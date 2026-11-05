"""hindsight CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import typer

from .config import load_config

app = typer.Typer(no_args_is_help=True, help="Hindsight: background redesign agents.")


def _cfg(project_dir: str | None):
    return load_config(project_dir or ".")


@app.command()
def init(project_dir: str = typer.Option(".", help="Project directory to initialize")):
    """Configure project, write .hindsight.toml, install plugin, run checks."""
    cfg = _cfg(project_dir)
    config_path = cfg.project_dir / ".hindsight.toml"
    if not config_path.exists():
        example = Path(__file__).parent.parent / ".hindsight.example.toml"
        config_path.write_text(example.read_text() if example.exists() else
                               "# hindsight configuration\n")
        typer.echo(f"wrote {config_path}")
    # install plugin into .opencode/plugins/hindsight/ (a directory: the
    # v2 plugin format loads the server part from index.ts at that root).
    # The GUI part (nudge toasts) goes to the GLOBAL plugins dir instead:
    # the desktop app's window-side plugin host lists only the sidecar
    # server's plugins (~/.config/opencode/plugins), not per-project ones
    # . Installing the server part globally too
    # would double-emit session events, so only gui-experimental.cjs goes
    # there.
    plugin_dir = cfg.project_dir / ".opencode" / "plugins"
    src = Path(__file__).parent.parent / "plugin" / "hindsight"
    if src.is_dir():
        dst = plugin_dir / "hindsight"
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        typer.echo(f"installed plugin into {dst}")
        gui_src = src / "gui-experimental.cjs"
        if gui_src.exists():
            gui_dst = (Path.home() / ".config" / "opencode" / "plugins"
                       / "hindsight")
            gui_dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(gui_src, gui_dst / "gui-experimental.cjs")
            typer.echo(f"installed GUI part into {gui_dst} (desktop toasts;"
                       " restart OpenCode Desktop to pick it up)")
    cfg.hindsight_dir.mkdir(parents=True, exist_ok=True)
    (cfg.hindsight_dir / "snap.git").mkdir(parents=True, exist_ok=True)
    typer.echo(f"hindsight data dir: {cfg.hindsight_dir}")
    typer.echo("next: choose your models in .hindsight.toml, run `hindsight box-key` and `hindsight doctor`, then `hindsight up` while you work.")


@app.command("box-key")
def box_key(project_dir: str = typer.Option(".")):
    """Set up model access for worker boxes, without putting the key in them."""
    from .box_key import set_up

    try:
        for line in set_up(_cfg(project_dir).models):
            typer.echo(line)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)


@app.command()
def doctor(project_dir: str = typer.Option(".")):
    """Check every integration and print fixes."""
    from .doctor import run_doctor
    ok = run_doctor(_cfg(project_dir))
    sys.exit(0 if ok else 1)


@app.command()
def up(project_dir: str = typer.Option(".")):
    """Start the local orchestrator (HTTP API) for this project."""
    import uvicorn

    from .api import create_app
    from .orchestrator import Orchestrator

    cfg = _cfg(project_dir)
    orch = Orchestrator(cfg)
    from .api import create_app as _ca  # noqa: F811
    api = create_app(orch)
    typer.echo(f"hindsight orchestrator on http://127.0.0.1:8787 (project {cfg.project_id})")
    uvicorn.run(api, host="127.0.0.1", port=8787, log_level="warning")


@app.command()
def trace(session_id: str = typer.Argument(..., help="OpenCode session id"),
          project_dir: str = typer.Option("."),
          call: int = typer.Option(0, help="Show this model call's prompt and reply"),
          worker: str = typer.Option("", help="Show this worker's transcript"),
          json_out: bool = typer.Option(False, "--json")):
    """What happened in a session, in order, and why."""
    from .store import Store
    from .trace import build_timeline, render_call, render_timeline

    store = Store(_cfg(project_dir).db_path)
    try:
        if call:
            typer.echo(render_call(store, call))
        elif worker:
            typer.echo(store.worker_transcript(worker) or "(no transcript kept)")
        elif json_out:
            typer.echo(json.dumps(build_timeline(store, session_id), indent=1))
        else:
            typer.echo(render_timeline(build_timeline(store, session_id)))
    finally:
        store.close()


@app.command()
def status(project_dir: str = typer.Option("."), json_out: bool = typer.Option(False, "--json")):
    """Current activity, spend, inbox count."""
    import httpx

    cfg = _cfg(project_dir)
    try:
        r = httpx.get("http://127.0.0.1:8787/status", timeout=5)
        data = r.json()
    except Exception as e:
        typer.echo(f"orchestrator not reachable ({e}); is `hindsight up` running?")
        sys.exit(1)
    if json_out:
        typer.echo(json.dumps(data))
        return
    typer.echo(f"hindsight: {data['state']}")
    for ex in data["explorations"]:
        typer.echo(f"  {ex['id']} {ex['strategy']} {ex['state']}")
    typer.echo(f"spend 24h: ${data['spend_24h_usd']:.2f} / ${data['budget_per_day_usd']:.2f}")


@app.command()
def explore(session_id: str = typer.Argument(..., help="OpenCode session id"),
           strategies: str = typer.Option(None, "--strategies", help="comma-separated"),
           project_dir: str = typer.Option(".")):
    """User-requested exploration now (pull mode)."""
    import httpx

    cfg = _cfg(project_dir)
    body: dict = {"session_id": session_id}
    if strategies:
        body["strategies"] = [s.strip() for s in strategies.split(",")]
    r = httpx.post("http://127.0.0.1:8787/explorations", json=body, timeout=30)
    typer.echo(r.text if r.status_code == 200 else f"error: {r.text}")


@app.command()
def inbox(project_dir: str = typer.Option(".")):
    """List proposals below the nudge bar."""
    import httpx

    _cfg(project_dir)
    r = httpx.get("http://127.0.0.1:8787/inbox", timeout=10)
    for p in r.json():
        ds = p.get("diff_stat", {})
        typer.echo(f"{p['id']}  {p['exploration_id']}  "
                   f"{ds.get('files', '?')} files (+{ds.get('added', '?')} -{ds.get('removed', '?')})")


@app.command()
def show(proposal_id: str, project_dir: str = typer.Option(".")):
    """Summary, evidence, diff for one proposal."""
    import httpx

    _cfg(project_dir)
    r = httpx.get(f"http://127.0.0.1:8787/proposals/{proposal_id}", timeout=10)
    if r.status_code != 200:
        typer.echo(f"error: {r.text}")
        sys.exit(1)
    p = r.json()
    typer.echo(f"== {p['id']} (exploration {p['exploration_id']}) ==")
    typer.echo(json.dumps(p.get("summary"), indent=2)[:2000])
    typer.echo(p["patch"][:4000])


@app.command()
def budget(project_dir: str = typer.Option(".")):
    """Spend so far and remaining."""
    import httpx

    _cfg(project_dir)
    r = httpx.get("http://127.0.0.1:8787/budget", timeout=10)
    typer.echo(json.dumps(r.json(), indent=2))


@app.command()
def ask(proposal_id: str = typer.Argument(...),
        question: str = typer.Argument(None),
        project_dir: str = typer.Option(".")):
    """`hindsight_ask`: a proposal's checked facts; with a question, its
    finished worker is resumed to answer (can take minutes)."""
    from .orchestrator import Orchestrator

    cfg = _cfg(project_dir)
    orch = Orchestrator(cfg)
    try:
        out = orch.ask(proposal_id, question)
        if "error" in out:
            typer.echo(f"error: {out['error']}")
            sys.exit(1)
        typer.echo(json.dumps(out, indent=2)[:8000])
    finally:
        orch.shutdown()


@app.command()
def adopt(proposal_id: str = typer.Argument(...),
          project_dir: str = typer.Option(".")):
    """Adopt a proposal: backup -> apply patch -> run tests -> notify.
    The only command that writes your repo. Undo: `hindsight undo`."""
    from .adopt import AdoptionError
    from .orchestrator import Orchestrator

    cfg = _cfg(project_dir)
    orch = Orchestrator(cfg)
    try:
        out = orch.adopt.adopt(proposal_id, via="cli")
        typer.echo(out["message"])
        typer.echo(f"adoption: {out['adoption_id']} (backup: {out['backup_dir']})")
        if out["test_passed"] is False:
            sys.exit(2)  # applied, but tests failed — visible in exit code
    except AdoptionError as e:
        typer.echo(f"error: {e}")
        sys.exit(1)
    finally:
        orch.shutdown()


@app.command()
def undo(adoption_id: str = typer.Argument("last"),
         project_dir: str = typer.Option(".")):
    """Undo an adoption: restore pre-images, remove created files."""
    from .adopt import AdoptionError
    from .orchestrator import Orchestrator

    cfg = _cfg(project_dir)
    orch = Orchestrator(cfg)
    try:
        out = orch.adopt.undo(adoption_id, via="cli")
        if out["already_undone"]:
            typer.echo(f"{out['adoption_id']} was already undone")
            return
        typer.echo(f"undone: {out['adoption_id']} "
                   f"({len(out['restored'])} restored, "
                   f"{len(out['removed'])} removed)")
    except AdoptionError as e:
        typer.echo(f"error: {e}")
        sys.exit(1)
    finally:
        orch.shutdown()


@app.command()
def adoptions(project_dir: str = typer.Option(".")):
    """List adoptions for this project."""
    from .orchestrator import Orchestrator

    cfg = _cfg(project_dir)
    orch = Orchestrator(cfg)
    try:
        for r in orch.store.adoptions_list():
            state = "undone" if r["undone_at"] else "active"
            tests = ("?" if r["test_passed"] is None
                     else "pass" if r["test_passed"] else "FAIL")
            typer.echo(f"{r['id']}  {r['proposal_id']}  {state}  tests={tests}")
    finally:
        orch.shutdown()


if __name__ == "__main__":
    app()
