"""hindsight doctor: check every integration, print fixes."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import HindsightConfig

CHECKS: list = []


@dataclass
class CheckResult:
    name: str
    ok: bool
    critical: bool
    detail: str = ""


def check(name: str, critical: bool = True):
    def deco(fn):
        CHECKS.append((name, critical, fn))
        return fn

    return deco


@check("opencode installed", critical=False)
def _opencode(cfg: HindsightConfig) -> tuple[bool, str]:
    path = shutil.which("opencode")
    if not path:
        return False, "opencode CLI not found; Hindsight watches OpenCode sessions, so install it"
    return True, path


@check("plugin installed")
def _plugin(cfg: HindsightConfig) -> tuple[bool, str]:
    p = cfg.project_dir / ".opencode" / "plugins" / "hindsight" / "index.ts"
    if not p.exists():
        return False, "run `hindsight init` to install the plugin"
    gui = (Path.home() / ".config" / "opencode" / "plugins" / "hindsight"
           / "gui-experimental.cjs")
    if not gui.exists():
        return False, (f"server part ok at {p}, but the GUI part (desktop "
                       "nudge toasts) is missing from "
                       "~/.config/opencode/plugins/hindsight/ — re-run "
                       "`hindsight init`")
    return True, f"{p} + {gui}"


@check("git available")
def _git(cfg: HindsightConfig) -> tuple[bool, str]:
    path = shutil.which("git")
    return (path is not None), f"git is required for snapshots: {path or 'missing'}"


@check("sail api key")
def _sail_key(cfg: HindsightConfig) -> tuple[bool, str]:
    if not os.environ.get("SAIL_API_KEY"):
        return False, "set SAIL_API_KEY, which runs the boxes (https://docs.sailresearch.com)"
    return True, "SAIL_API_KEY is set"


@check("model api key")
def _model_key(cfg: HindsightConfig) -> tuple[bool, str]:
    name = cfg.models.api_key_env
    if not os.environ.get(name):
        return False, f"set {name}, the key for {cfg.models.base_url}"
    return True, f"{name} is set, for {cfg.models.base_url}"


@check("sail reachable", critical=True)
def _sail_reachable(cfg: HindsightConfig) -> tuple[bool, str]:
    if not os.environ.get("SAIL_API_KEY"):
        return False, "cannot check without SAIL_API_KEY"
    try:
        from .sail_client import SailClient

        boxes = SailClient(cfg.app_name).list_boxes()
        return True, f"{len(boxes)} boxes visible in org"
    except Exception as e:
        return False, f"sail list_boxes failed: {e}"


@check("box key policy", critical=True)
def _box_policy(cfg: HindsightConfig) -> tuple[bool, str]:
    """Without this policy no worker box can start, because boxes never hold the key."""
    if not os.environ.get("SAIL_API_KEY"):
        return False, "cannot check without SAIL_API_KEY"
    from sail import EgressPolicy

    name = cfg.models.box_policy
    if any(policy.name == name for policy in EgressPolicy.list()):
        return True, f"egress policy {name} exists"
    return False, (f"no egress policy named {name}, so workers can't start. "
                   "Run `hindsight box-key`")


@check("snapshot repo writable")
def _snapdir(cfg: HindsightConfig) -> tuple[bool, str]:
    try:
        cfg.hindsight_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.hindsight_dir / ".probe"
        probe.write_text("ok")
        probe.unlink()
        return True, str(cfg.hindsight_dir)
    except OSError as e:
        return False, f"cannot write {cfg.hindsight_dir}: {e}"


@check("test command runs locally", critical=False)
def _tests(cfg: HindsightConfig) -> tuple[bool, str]:
    import shutil as sh
    import subprocess

    if not sh.which("python3"):
        return False, "python3 not found"
    r = subprocess.run(["python3", "-c", "import pytest"], capture_output=True)
    if r.returncode != 0:
        return False, "pytest not importable locally (box builds install it themselves)"
    return True, "pytest available"


def run_doctor(cfg: HindsightConfig) -> bool:
    print(f"hindsight doctor — project {cfg.project_id} ({cfg.project_dir})")
    print()
    all_ok = True
    for name, critical, fn in CHECKS:
        try:
            ok, detail = fn(cfg)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"check crashed: {e}"
        mark = "PASS" if ok else ("FAIL" if critical else "warn")
        print(f"[{mark:<4}] {name}: {detail}")
        if not ok and critical:
            all_ok = False
    print()
    print("all critical checks passed" if all_ok else "critical checks failed — see above")
    return all_ok
