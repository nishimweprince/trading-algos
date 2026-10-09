"""The go-live switch for demo and shadow ("shallow") trading: one command, checked.

    ofi-go-live --profile dev status
    ofi-go-live --profile dev on                   # demo orders + shadow on the live feed
    ofi-go-live --profile dev on --shadow-only     # shadow on the live feed, nothing sent
    ofi-go-live --profile dev on --version v7      # a trained model instead of the rule
    ofi-go-live --profile dev off                  # back to demo controls only
    ... --restart                                  # then restart the unit and report

``on`` (demo + shadow) sets, in ``.env.<profile>``::

    OFI_EXECUTION_MODE=testnet    orders to execution-service on Binance demo trading
    OFI_SHADOW_MIRROR=true        and a shadow bridge on the mainnet feed beside it
    OFI_MODEL_VERSION=<version>   the rule model (rules.py) unless --version names another
    OFI_ALLOW_UNGATED=true        only when that model has no research gates pass

``rules-*`` versions that do not exist yet are written with the rule's defaults.
Before anything is kept, the edited file must load (``load_settings``) and the
model must load the way the service will load it; otherwise the backup is put
back. Live trading is not reachable from here: ``OFI_EXECUTION_MODE=live`` is
refused by the settings themselves.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from ta_core.settings import resolve_env_file

from .config import ExecutionMode, Settings, load_settings
from .rules import DEFAULT_POLICY, RuleParams

__all__ = ["KEYS", "apply_env", "main", "plan", "read_env"]

KEYS = ("OFI_EXECUTION_MODE", "OFI_MODEL_VERSION", "OFI_ALLOW_UNGATED", "OFI_SHADOW_MIRROR")
MARK = "# ofi-go-live"
# rules-v1 had no re-entry cooldown: on demo it re-entered every 100 ms after rejections.
# rules-v4 adds the 5 s entry timeout and hold (rules-v2/v3 exist on the VM with other
# parameters; model directories are hash-pinned, so a name is never reused).
# rules-v5 moves to the 300 s horizon and 20 bp barrier.
DEFAULT_RULES_VERSION = "rules-v5"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def apply_env(text: str, updates: dict[str, str | None]) -> str:
    """Set (or, for None, comment out) each key; other lines are kept as they are."""
    lines = text.splitlines()
    done: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if "=" in stripped else ""
        if key in updates and not stripped.startswith("#"):
            value = updates[key]
            if key in done:
                continue  # a later duplicate would override ours: drop it
            done.add(key)
            out.append(f"# {stripped}  {MARK} off" if value is None else f"{key}={value}")
            continue
        out.append(line)
    missing = [k for k, v in updates.items() if k not in done and v is not None]
    if missing:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{MARK} {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
        out += [f"{key}={updates[key]}" for key in missing]
    return "\n".join(out) + "\n"


def plan(
    action: str,
    *,
    version: str,
    shadow_only: bool,
    demo_only: bool,
    ungated: bool,
    current: dict[str, str],
) -> dict[str, str | None]:
    """The env values for an action."""
    if action == "off":
        mode = current.get("OFI_EXECUTION_MODE", "off")
        return {
            # Demo controls (kill, dead-man, flatten) stay; shadow alone means nothing to keep.
            "OFI_EXECUTION_MODE": "off" if mode == ExecutionMode.SHADOW.value else mode,
            "OFI_MODEL_VERSION": None,
            "OFI_ALLOW_UNGATED": "false",
            "OFI_SHADOW_MIRROR": "false",
        }
    return {
        "OFI_EXECUTION_MODE": "shadow" if shadow_only else "testnet",
        "OFI_MODEL_VERSION": version,
        "OFI_ALLOW_UNGATED": "true" if ungated else "false",
        "OFI_SHADOW_MIRROR": "false" if shadow_only or demo_only else "true",
    }


def _ensure_rules_model(model_dir: Path, version: str) -> bool:
    """Write a default rule model for a missing ``rules-*`` version. True if written."""
    from .model import write_rules_model

    if (model_dir / version).exists() or not version.startswith("rules"):
        return False
    write_rules_model(model_dir, version, rules=RuleParams().as_dict(), policy=dict(DEFAULT_POLICY))
    return True


def _model_is_ungated(model_dir: Path, version: str) -> bool:
    manifest = model_dir / version / "manifest.json"
    if not manifest.is_file():
        raise SystemExit(f"no model at {model_dir / version} (manifest.json missing)")
    files = json.loads(manifest.read_text()).get("files") or {}
    gates = model_dir / version / "gates.json"
    if "gates.json" not in files or not gates.is_file():
        return True
    return json.loads(gates.read_text()).get("passed") is not True


def _validate(profile: str | None) -> tuple[Settings, dict[str, Any] | None]:
    """Load the settings and the model exactly as the service will. Raises on failure."""
    from .app import load_pinned_model

    settings = load_settings(profile)
    model = load_pinned_model(settings)
    return settings, (model.summary() if model is not None else None)


async def _gateway_ready(settings: Settings) -> tuple[bool, str]:
    from ta_clients.execution import ExecutionClient

    from .app import _ClientSettings

    if settings.execution_api_key is None:
        return False, "no EXECUTION_API_KEY"
    async with httpx.AsyncClient() as http:
        client = ExecutionClient(
            _ClientSettings(
                ctrader_markets_url=settings.execution_url,
                execution_timeout_seconds=settings.execution_timeout_seconds,
                ctrader_api_key=settings.execution_api_key,
                execution_account=settings.execution_account,
            ),
            http,
        )
        return await client.trading_ready()


def _scalper_status(settings: Settings) -> dict[str, Any] | None:
    try:
        response = httpx.get(
            f"http://127.0.0.1:{settings.port}/v1/status",
            headers={"X-API-Key": settings.api_key.get_secret_value()},
            timeout=3.0,
        )
    except httpx.HTTPError:
        return None
    return response.json() if response.status_code == 200 else None


def _report(settings: Settings, model: dict[str, Any] | None) -> list[str]:
    lines = [
        f"mode {settings.execution_mode.value}"
        + (" + shadow mirror on the live feed" if settings.shadow_mirror else ""),
        f"model {model['version']} ({model['kind']}{', ungated' if model['ungated'] else ''})"
        if model
        else "model none (controls only)"
        if settings.execution_mode is ExecutionMode.TESTNET
        else "model none",
    ]
    if model:
        policy = model["policy"]
        lines.append(
            f"policy horizon {policy['horizon_s']:g}s, barrier {policy['barrier_bp']:g} bp, "
            f"threshold {policy['threshold']:g}, buffer {policy['buffer_bp']:g} bp; "
            f"size {settings.order_notional_usd:g} USD"
        )
    status = _scalper_status(settings)
    if status is None:
        lines.append("scalper not reachable on 127.0.0.1 (stopped, or another host)")
    else:
        risk = status.get("risk") or {}
        halt = risk.get("halt")
        running = status.get("execution_mode")
        lines.append(
            f"running now: mode {running}, model {(status.get('model') or {}).get('version')}, "
            + (f"HALTED ({(halt or {}).get('reason')})" if halt else "not halted")
        )
        if halt:
            lines.append(
                "  clear it once the cause is understood: "
                f"curl -s -X POST -H 'X-API-Key: ...' localhost:{settings.port}/v1/kill/ack"
            )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ofi-go-live", description=__doc__.split("\n")[0])
    parser.add_argument("action", choices=("status", "on", "off"))
    parser.add_argument("--profile", default=None)
    parser.add_argument("--version", default=DEFAULT_RULES_VERSION)
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--shadow-only", action="store_true", help="no demo orders")
    which.add_argument("--demo-only", action="store_true", help="no shadow mirror")
    parser.add_argument("--restart", action="store_true", help="restart the unit afterwards")
    parser.add_argument("--unit", default="ofi-scalper-service")
    args = parser.parse_args(argv)

    env_path = resolve_env_file(args.profile, settings_class=Settings).resolve()
    if not env_path.is_file():
        print(f"no {env_path}", file=sys.stderr)
        return 1
    if args.action == "status":
        settings, model = _validate(args.profile)
        print("\n".join(_report(settings, model)))
        return 0

    original = env_path.read_text()
    current = read_env(env_path)
    os.chdir(env_path.parent)  # relative paths in the env file resolve as the service sees them
    model_dir = Path(current.get("OFI_MODEL_DIR", "models")).expanduser()
    ungated = False
    if args.action == "on":
        if _ensure_rules_model(model_dir, args.version):
            print(f"wrote {model_dir / args.version} (rule model, default parameters)")
        ungated = _model_is_ungated(model_dir, args.version)
    updates = plan(
        args.action,
        version=args.version,
        shadow_only=args.shadow_only,
        demo_only=args.demo_only,
        ungated=ungated,
        current=current,
    )
    backup = env_path.with_name(f"{env_path.name}.bak-{time.strftime('%Y%m%dT%H%M%S')}")
    shutil.copy2(env_path, backup)
    env_path.write_text(apply_env(original, updates))
    try:
        settings, model = _validate(args.profile)
    except Exception as exc:  # noqa: BLE001 - any failure puts the old file back
        env_path.write_text(original)
        print(f"refused, {env_path.name} unchanged: {exc}", file=sys.stderr)
        return 1
    print(f"{env_path.name} updated (backup {backup.name})")
    for key in KEYS:
        print(f"  {key}={updates[key] if updates[key] is not None else '(unset)'}")
    if settings.execution_mode is ExecutionMode.TESTNET:
        ready, reason = asyncio.run(_gateway_ready(settings))
        print(f"execution-service: {'ready' if ready else 'NOT ready: ' + reason}")
    if args.restart:
        result = subprocess.run(["sudo", "systemctl", "restart", args.unit], check=False)
        if result.returncode != 0:
            print(f"restart failed ({result.returncode}); restart {args.unit} by hand")
            return 1
        time.sleep(5)
    else:
        print(f"restart to apply: sudo systemctl restart {args.unit}")
    print("\n".join(_report(settings, model)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
