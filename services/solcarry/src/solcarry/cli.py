"""solcarry CLI.

Exit codes: 0 ok, 1 unexpected error, 2 validation failed, 3 lock missing/mismatch.
"""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from . import pipeline
from .config import Config, Paths, load_config
from .lock import write_lock
from .validate import ValidationResult

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)
console = Console()

EXIT_OK, EXIT_ERR, EXIT_VALIDATION, EXIT_LOCK = 0, 1, 2, 3

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="Path to gate1.yaml")]
DataOpt = Annotated[
    Path | None, typer.Option("--data-dir", help="Data root (default <project>/data)")
]


def _default_config() -> Path:
    for base in (Path.cwd(), Path(__file__).resolve().parents[2]):
        p = base / "config" / "gate1.yaml"
        if p.exists():
            return p
    return Path("config/gate1.yaml")


class Ctx:
    cfg: Config
    paths: Paths


_ctx = Ctx()


@app.callback()
def main(
    config: ConfigOpt = _default_config(),  # noqa: B008
    data_dir: DataOpt = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _ctx.cfg = load_config(config)
    _ctx.paths = Paths(config.resolve().parent.parent, data_dir)


def _apply_range(start: date | None, end: date | None) -> None:
    if start:
        _ctx.cfg.run.start = start
    if end:
        _ctx.cfg.run.end = end


@app.command()
def fetch(
    source: Annotated[
        str, typer.Option(help="all|velocity|binance|defillama|fred|jupiter|okx")
    ] = "all",
    start: Annotated[str | None, typer.Option(help="YYYY-MM-DD")] = None,
    end: Annotated[str | None, typer.Option(help="YYYY-MM-DD")] = None,
) -> None:
    """Download raw data into data/raw (idempotent)."""
    _apply_range(
        date.fromisoformat(start) if start else None, date.fromisoformat(end) if end else None
    )
    out = pipeline.fetch(_ctx.cfg, _ctx.paths, source)
    console.print(out)


@app.command()
def normalize() -> None:
    """Build hourly UTC parquet tables in data/normalized."""
    t = pipeline.normalize(_ctx.cfg, _ctx.paths)
    tab = Table("table", "rows", "start", "end")
    for name in (
        "spot",
        "funding_velocity",
        "funding_binance",
        "index",
        "staking",
        "hurdle",
        "lending",
    ):
        df = getattr(t, name)
        tab.add_row(
            name,
            str(len(df)),
            str(df.index.min()) if len(df) else "-",
            str(df.index.max()) if len(df) else "-",
        )
    tab.add_row("quarterly", str(len(t.quarterly)), "", "")
    console.print(tab)


def _print_validation(res: ValidationResult) -> None:
    tab = Table("check", "level", "ok", "detail")
    for c in res.checks:
        mark = "[green]ok" if c.ok else ("[red]FAIL" if c.level == "hard" else "[yellow]warn")
        tab.add_row(c.name, c.level, mark, c.detail)
    console.print(tab)


@app.command()
def validate(run_id: Annotated[str | None, typer.Option()] = None) -> None:
    """Run data sanity checks; exit 2 on any hard failure."""
    run_dir = (_ctx.paths.results / run_id) if run_id else pipeline.new_run(_ctx.cfg, _ctx.paths)
    run_dir.mkdir(parents=True, exist_ok=True)
    res = pipeline.validate(_ctx.cfg, _ctx.paths, run_dir)
    _print_validation(res)
    if not res.ok:
        raise typer.Exit(EXIT_VALIDATION)


@app.command()
def lock() -> None:
    """Pre-register thresholds + variants + alpha (writes config/gate1.lock)."""
    rec = write_lock(_ctx.cfg, _ctx.paths.lock_file)
    console.print(f"locked {rec['hash'][:16]} at {rec['locked_at']} -> {_ctx.paths.lock_file}")


@app.command()
def analyze(
    variant: Annotated[list[str] | None, typer.Option("--variant", help="repeatable")] = None,
    run_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Compute run-ups, reserves, P and D variants into a new results/<run_id>/."""
    run_dir = (_ctx.paths.results / run_id) if run_id else pipeline.new_run(_ctx.cfg, _ctx.paths)
    run_dir.mkdir(parents=True, exist_ok=True)
    out = pipeline.analyze(_ctx.cfg, _ctx.paths, run_dir, variant)
    console.print(f"analysis written to {run_dir} ({len(out['summaries'])} variants)")


def _gate(run_dir: Path, unlocked: bool) -> dict[str, Any]:
    st = pipeline.lock_status(_ctx.cfg, _ctx.paths)
    if not st.ok and not unlocked:
        console.print(f"[red]lock {st.state}: {st.detail}. Run `solcarry lock` or pass --unlocked.")
        raise typer.Exit(EXIT_LOCK)
    g = pipeline.gate(_ctx.cfg, _ctx.paths, run_dir, st)
    tab = Table("variant", "status", "failing", "flags", "note")
    for s in g["scores"]:
        color = {"PASS": "green", "FAIL": "red"}.get(s["status"], "yellow")
        tab.add_row(
            s["variant"],
            f"[{color}]{s['status']}",
            ", ".join(s["failing"]),
            ", ".join(s["flags"]),
            s["note"],
        )
    console.print(tab)
    console.print(
        f"[bold]GATE 1: {g['result']}[/bold]" + ("  [red](UNLOCKED)" if g["unlocked"] else "")
    )
    return g


@app.command()
def gate(
    unlocked: Annotated[bool, typer.Option("--unlocked")] = False,
    run_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Score variants against the locked thresholds (exit 3 if the lock is missing/mismatched)."""
    _gate(pipeline.resolve_run(_ctx.paths, run_id), unlocked)


@app.command()
def report(run_id: Annotated[str | None, typer.Option("--run-id")] = None) -> None:
    """Write report.md, charts and ui.json for a run (default: latest)."""
    from .gate.report import build_report

    run_dir = pipeline.resolve_run(_ctx.paths, run_id)
    p = build_report(_ctx.cfg, _ctx.paths, run_dir)
    console.print(f"report: {p}")


@app.command()
def depth() -> None:
    """One Jupiter depth snapshot, appended to depth_history.parquet (cron-friendly)."""
    snap = pipeline.depth_snapshot(_ctx.cfg, _ctx.paths)
    tab = Table("lst", "size", "out SOL", "impact", "routes")
    for _, r in snap.iterrows():
        tab.add_row(
            r["lst"],
            f"{r['size']:g}",
            f"{r['out_sol']:.4f}",
            f"{r['impact']:.4%}",
            str(r["routes"])[:60],
        )
    console.print(tab)


@app.command("run-all")
def run_all(
    unlocked: Annotated[bool, typer.Option("--unlocked")] = False,
    skip_fetch: Annotated[bool, typer.Option("--skip-fetch", help="reuse the raw cache")] = False,
) -> None:
    """fetch -> normalize -> validate -> analyze -> gate -> report."""
    from .gate.report import build_report

    st = pipeline.lock_status(_ctx.cfg, _ctx.paths)
    if not st.ok and not unlocked:
        console.print(f"[red]lock {st.state}: {st.detail}. Run `solcarry lock` or pass --unlocked.")
        raise typer.Exit(EXIT_LOCK)
    if not skip_fetch:
        console.print(pipeline.fetch(_ctx.cfg, _ctx.paths, "all"))
    else:
        pipeline.depth_snapshot(_ctx.cfg, _ctx.paths)
    pipeline.normalize(_ctx.cfg, _ctx.paths)
    run_dir = pipeline.new_run(_ctx.cfg, _ctx.paths)
    res = pipeline.validate(_ctx.cfg, _ctx.paths, run_dir)
    _print_validation(res)
    if not res.ok:
        console.print(f"[red]validation failed; see {run_dir / 'validation.json'}")
        raise typer.Exit(EXIT_VALIDATION)
    pipeline.analyze(_ctx.cfg, _ctx.paths, run_dir)
    _gate(run_dir, unlocked)
    console.print(f"report: {build_report(_ctx.cfg, _ctx.paths, run_dir)}")


@app.command()
def serve(
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8765,
) -> None:
    """Serve the local dashboard over data/results (read-only)."""
    from .ui.server import serve as _serve

    console.print(f"solcarry dashboard on http://{host}:{port}")
    _serve(_ctx.paths.results, host, port)


def entry() -> None:  # pragma: no cover
    try:
        app()
    except SystemExit:
        raise
    except Exception as e:
        console.print_exception()
        console.print(f"[red]error: {e}")
        sys.exit(EXIT_ERR)


if __name__ == "__main__":  # pragma: no cover
    entry()
