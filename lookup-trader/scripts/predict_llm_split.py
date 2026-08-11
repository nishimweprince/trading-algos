#!/usr/bin/env python3
"""Run a split through the fine-tuned deployment and write predictions for scoring.

    ./scripts/predict_llm_split.py --split validation

Configuration comes from `server/.env` (the same file the app reads), or the
environment if you would rather export it:

    LOOKUP_AZURE_OPENAI_ENDPOINT     https://<resource>.cognitiveservices.azure.com
    LOOKUP_AZURE_OPENAI_KEY          deployment key
    LOOKUP_AZURE_OPENAI_DEPLOYMENT   deployment name, not the model name
    LOOKUP_AZURE_OPENAI_API_VERSION  optional
    LOOKUP_AZURE_OPENAI_REQUESTS_PER_MINUTE  optional, paced client-side

Then score what it wrote:

    ./scripts/eval_llm_predictions.py \
        --predictions data/exports/llm/preds-validation.jsonl --split validation

Offline batch only. Nothing here runs on the live path — `/compare` and the
meta-shadow worker never call an external model, and this script exists solely to
produce a file for `eval_llm_predictions.py`.

Two things it does that matter more than the API call:

**Probability, not just a decision.** The completion is `{"take":true}` with the
boolean last, so `top_logprobs` at that position gives P(true) directly. Without
it the evaluator can only report take rate and lift; with it you also get log
loss, AUC, calibration and the threshold sweep — which is what separates a model
that is *better* from one that is merely *more aggressive*.

**Resumability.** A split is a couple of thousand requests. Output is appended
and already-scored `event_id`s are skipped on restart, so a rate limit or a
dropped connection costs the remaining rows rather than all of them.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.config import settings  # noqa: E402

DEFAULT_API_VERSION = "2024-12-01-preview"
MAX_ATTEMPTS = 6
# The completion is a fixed 8-token shape; anything longer means the model has
# started explaining itself, which is not what it was trained to do.
MAX_TOKENS = 16


def split_dir() -> Path:
    return settings.data_dir / "exports" / "llm"


def _dotenv() -> dict[str, str]:
    """Values from `server/.env`, so this matches how the app is configured."""
    path = _REPO_ROOT / "server" / ".env"
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _config() -> dict[str, str]:
    """Environment wins over `.env`, so a one-off export can override the file."""
    merged = {**_dotenv(), **os.environ}
    required = (
        "LOOKUP_AZURE_OPENAI_ENDPOINT",
        "LOOKUP_AZURE_OPENAI_KEY",
        "LOOKUP_AZURE_OPENAI_DEPLOYMENT",
    )
    missing = sorted(name for name in required if not merged.get(name))
    if missing:
        raise SystemExit(
            "Missing configuration: "
            + ", ".join(missing)
            + "\nSet them in server/.env or export them."
        )
    return {
        "endpoint": merged["LOOKUP_AZURE_OPENAI_ENDPOINT"].rstrip("/"),
        "key": merged["LOOKUP_AZURE_OPENAI_KEY"],
        "deployment": merged["LOOKUP_AZURE_OPENAI_DEPLOYMENT"],
        "api_version": merged.get("LOOKUP_AZURE_OPENAI_API_VERSION") or DEFAULT_API_VERSION,
        "rpm": merged.get("LOOKUP_AZURE_OPENAI_REQUESTS_PER_MINUTE") or "0",
    }


def _load(split: str) -> list[dict[str, Any]]:
    """Prompts joined to their event ids by line order, as the exporter wrote them."""
    prompts = split_dir() / f"meta-events-{split}.jsonl"
    sidecar = split_dir() / f"meta-events-{split}.metadata.jsonl"
    for path in (prompts, sidecar):
        if not path.exists():
            raise SystemExit(f"Missing {path} — run scripts/export_llm_dataset.py first.")

    rows = [json.loads(line) for line in prompts.open(encoding="utf-8")]
    meta = [json.loads(line) for line in sidecar.open(encoding="utf-8")]
    if len(rows) != len(meta):
        raise SystemExit(
            f"{prompts.name} has {len(rows)} lines but {sidecar.name} has {len(meta)}; "
            "re-export so the sidecar matches."
        )
    return [
        {
            "event_id": m["event_id"],
            # Drop the assistant turn: that is the answer we are asking for.
            "messages": [msg for msg in row["messages"] if msg["role"] != "assistant"],
        }
        for row, m in zip(rows, meta, strict=True)
    ]


def _probability_of_true(logprobs: dict[str, Any] | None) -> tuple[float | None, str | None]:
    """P(true) at the position where the model actually committed to a boolean.

    Returns `(probability, emitted)` so the caller can check the two agree.

    An earlier version scanned for the first position whose *alternatives*
    mentioned a boolean. That is wrong whenever the model answers with a bare
    `true` instead of `{"take":true}`, because then `true` and `false` are both
    plausible opening tokens and appear in `top_logprobs` at position 0. It read
    P(true-vs-false as an opening token) and reported it as the decision, which
    produced identical probabilities on opposite answers.

    Match on the token the model emitted instead. That position is the decision
    by construction, whatever the surrounding format.
    """
    import math

    if not logprobs or not logprobs.get("content"):
        return None, None
    for position in logprobs["content"]:
        emitted = position.get("token", "").strip().strip('",:{}[] ')
        if emitted not in {"true", "false"}:
            continue
        # Keep the BEST logprob per boolean, not the last one seen. `top_logprobs`
        # returns variants that strip to the same key — `false`, `":false`,
        # ` false` — and a dict comprehension keeps whichever comes last. Since
        # the list is ordered by descending probability, that silently replaced
        # the emitted token's real mass with its least likely variant and
        # inverted the result: logit +7.4 reported for `take:false`.
        alternatives: dict[str, float] = {}
        for entry in position.get("top_logprobs", []):
            key = entry["token"].strip().strip('",:{}[] ')
            if key in {"true", "false"}:
                alternatives[key] = max(alternatives.get(key, -math.inf), entry["logprob"])
        true_p = math.exp(alternatives["true"]) if "true" in alternatives else 0.0
        false_p = math.exp(alternatives["false"]) if "false" in alternatives else 0.0
        total = true_p + false_p
        if total <= 0:
            # The emitted token is known even when the counterpart is outside the
            # top-k, so fall back to a hard decision rather than discarding it.
            return (1.0 if emitted == "true" else 0.0), emitted
        # Renormalise over the two legal answers; other tokens at this position
        # are not decisions the model was trained to make.
        return float(true_p / total), emitted
    return None, None


class RateLimiter:
    """Client-side pacing so the deployment's quota is respected, not discovered.

    Backoff alone would work but wastes the whole budget learning where the wall
    is: at 50 RPM and eight workers, most of the first minute is 429s. Spacing
    requests keeps the run smooth and predictable.
    """

    def __init__(self, per_minute: int) -> None:
        self.interval = 60.0 / per_minute if per_minute > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if self.interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            due = max(now, self._next)
            self._next = due + self.interval
        delay = due - now
        if delay > 0:
            time.sleep(delay)


class Caller:
    """One deployment, paced, with backoff that respects Retry-After."""

    def __init__(
        self,
        endpoint: str,
        key: str,
        deployment: str,
        timeout: float,
        api_version: str,
        limiter: RateLimiter,
    ) -> None:
        self.url = (
            f"{endpoint}/openai/deployments/{deployment}"
            f"/chat/completions?api-version={api_version}"
        )
        self.key = key
        self.timeout = timeout
        self.limiter = limiter
        self.debug = False
        self._lock = threading.Lock()
        self.audit: dict[str, int] = {
            "throttled": 0,
            "errors": 0,
            "no_logprobs": 0,
            "probability_decision_mismatch": 0,
        }

    def _count(self, name: str) -> None:
        with self._lock:
            self.audit[name] = self.audit.get(name, 0) + 1

    def __call__(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "messages": row["messages"],
            "max_tokens": MAX_TOKENS,
            # Deterministic: this is a measurement, and it should reproduce.
            "temperature": 0.0,
            "logprobs": True,
            "top_logprobs": 5,
        }
        body = json.dumps(payload).encode("utf-8")
        for attempt in range(MAX_ATTEMPTS):
            self.limiter.wait()
            request = urllib.request.Request(
                self.url,
                data=body,
                headers={"Content-Type": "application/json", "api-key": self.key},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                    parsed = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 or exc.code >= 500:
                    self._count("throttled" if exc.code == 429 else "errors")
                    # Honour the server's own backoff when it gives one.
                    wait = float(exc.headers.get("Retry-After") or 0) or min(
                        2**attempt + random.random(), 60
                    )
                    time.sleep(wait)
                    continue
                self._count("errors")
                return {"event_id": row["event_id"], "error": f"http_{exc.code}"}
            except Exception as exc:
                self._count("errors")
                if attempt == MAX_ATTEMPTS - 1:
                    return {"event_id": row["event_id"], "error": type(exc).__name__}
                time.sleep(min(2**attempt + random.random(), 60))
        else:
            return {"event_id": row["event_id"], "error": "exhausted_retries"}

        choice = parsed["choices"][0]
        content = (choice["message"].get("content") or "").strip()
        probability, emitted = _probability_of_true(choice.get("logprobs"))
        if probability is None:
            self._count("no_logprobs")

        take: bool | None = None
        try:
            parsed_content = json.loads(content)
            take = bool(
                parsed_content["take"]
                if isinstance(parsed_content, dict)
                else parsed_content  # a bare `true` / `false`
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            # The model drifted off the trained format. The emitted boolean token
            # is still authoritative; the probability is only a tiebreak.
            if emitted is not None:
                take = emitted == "true"
            elif probability is not None:
                take = probability >= 0.5

        # A probability that disagrees with the decision means the wrong token
        # position was read. That bug shipped once; it must never be silent.
        if take is not None and probability is not None and (probability >= 0.5) != take:
            self._count("probability_decision_mismatch")

        out: dict[str, Any] = {"event_id": row["event_id"], "raw": content}
        # Attach the payload whenever the two disagree, so a mismatch is
        # self-documenting rather than something to reverse-engineer from
        # probabilities later.
        if self.debug or (
            take is not None and probability is not None and (probability >= 0.5) != take
        ):
            out["logprobs"] = choice.get("logprobs")
        if take is not None:
            out["take"] = take
        if probability is not None:
            out["probability"] = round(probability, 6)
        if take is None and probability is None:
            out["error"] = "unparseable_completion"
        return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("train", "validation", "test"), default="validation")
    parser.add_argument("--output", type=Path, default=None)
    # Kept low on purpose: this deployment allows 50 requests per minute, so
    # extra workers only produce 429s that the limiter then has to absorb.
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--requests-per-minute",
        type=int,
        default=None,
        help="override the pacing from .env (0 disables client-side pacing)",
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--limit", type=int, default=None, help="first N rows, for a smoke test")
    parser.add_argument(
        "--debug-logprobs",
        action="store_true",
        help="store the raw logprob payload on every row, to inspect tokenisation",
    )
    args = parser.parse_args()

    if args.split == "test":
        print("NOTE: the test split should be scored once, after everything is frozen.\n")

    config = _config()
    rows = _load(args.split)
    if args.limit:
        rows = rows[: args.limit]

    output = args.output or split_dir() / f"preds-{args.split}.jsonl"
    done: set[str] = set()
    if output.exists():
        for line in output.open(encoding="utf-8"):
            try:
                done.add(json.loads(line)["event_id"])
            except (json.JSONDecodeError, KeyError):
                continue
        print(f"resuming: {len(done):,} already scored in {output.name}")

    pending = [row for row in rows if row["event_id"] not in done]
    rpm = args.requests_per_minute if args.requests_per_minute is not None else int(config["rpm"])
    limiter = RateLimiter(rpm)
    eta = f", ~{len(pending) / rpm:.0f} min at {rpm} rpm" if rpm > 0 else ""
    print(
        f"split {args.split}: {len(rows):,} rows, {len(pending):,} to score "
        f"via {config['deployment']}{eta}"
    )
    if not pending:
        print("nothing to do.")
        return 0

    caller = Caller(
        config["endpoint"],
        config["key"],
        config["deployment"],
        args.timeout,
        config["api_version"],
        limiter,
    )
    caller.debug = args.debug_logprobs
    started = time.monotonic()
    written = 0
    lock = threading.Lock()
    with output.open("a", encoding="utf-8") as handle, ThreadPoolExecutor(
        max_workers=args.concurrency
    ) as pool:
        for result in pool.map(caller, pending):
            with lock:
                handle.write(json.dumps(result, separators=(",", ":")) + "\n")
                handle.flush()
                written += 1
                if written % 200 == 0:
                    rate = written / max(time.monotonic() - started, 1e-9)
                    print(f"  {written:,}/{len(pending):,}  {rate:.1f}/s")

    failed = sum(1 for line in output.open(encoding="utf-8") if '"error"' in line)
    print(f"\nwrote {output} ({written:,} new rows)")
    print(f"call audit: {caller.audit}")
    if caller.audit.get("probability_decision_mismatch"):
        print(
            "  WARNING: probability disagrees with the decision on "
            f"{caller.audit['probability_decision_mismatch']:,} rows — the logprob "
            "position is wrong and the probabilities must not be scored."
        )
    if failed:
        print(f"{failed:,} rows carry an error — re-run to retry only those")
    print(f"\nNext:\n  ./scripts/eval_llm_predictions.py --predictions {output} "
          f"--split {args.split}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
