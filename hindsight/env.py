"""Environment manager: checkpointed Sailboxes with deps installed.

Environment key = hash of dependency manifests + base image version.
Workers start from a checkpoint instead of reinstalling every time.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import HindsightConfig
from .sail_client import SailClient, SailClientError
from .snapshot import Snapshotter
from .store import Store

MANIFEST_FILES = [
    "pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.cfg",
    "setup.py", "poetry.lock", "uv.lock", "package.json", "package-lock.json",
    "Pipfile", "Pipfile.lock", "go.mod", "Cargo.toml", "Gemfile",
]

BASE_IMAGE_VERSION = "hs-base-4"  # bump when base image contents change
# hs-base-4: installs the worker agent CLI (headless
# opencode, pinned v2.2.5) into every env checkpoint via HARNESS_INSTALL.
# hs-base-3 shipped NO agent CLI, so the default worker harness died at
# Popen and every swarm worker produced an empty "proposal" (the
# dead-workforce bug).
# hs-base-3: install_python=False. The SDK default builds a
# uv-pinned python matching the DRIVER's version; in-box drivers run
# Debian's stock 3.11.2 and uv's registry no longer carries it —
# "No download found for cpython-3.11.2-linux-*-gnu" killed every
# envbuild/worker/edge-search box in two trials (misread first as an
# arch failure). The base's stock python is the same 3.11.2 anyway, and we
# never use @sail.function (the feature the pinning exists for).
# hs-base-2: arch pin arm64 -> amd64 (all runtime boxes observed x86_64;
# debian()'s default is amd64). Old arm64 checkpoints are never reused.


@dataclass
class EnvRecord:
    key: str
    checkpoint_id: str
    expires_at: float
    setup: dict
    flaky_tests: list


def env_key(project_dir: Path, dockerfile: str | None = None) -> str:
    h = hashlib.sha256()
    h.update(BASE_IMAGE_VERSION.encode())
    if dockerfile:
        h.update(Path(dockerfile).read_bytes())
    for name in MANIFEST_FILES:
        p = project_dir / name
        if p.is_file():
            h.update(name.encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:24]


# What a worker box needs on top of a project's own Dockerfile.
DOCKERFILE_TOOLS = """
RUN apt-get update && apt-get install -y --no-install-recommends python3 git ripgrep curl tar gzip ca-certificates && rm -rf /var/lib/apt/lists/*
"""


def project_image(dockerfile: str):
    import sail

    path = Path(dockerfile)
    return sail.Image.from_dockerfile(
        contents=path.read_text() + DOCKERFILE_TOOLS, context_dir=str(path.parent),
        architecture="amd64")


def base_image():
    """Debian amd64 with the toolchain; harness CLIs are installed by
    `harness_install_cmds` (config) because their pinned install channels
    are Items (see docs/VERIFY.md).

    Image API verified against sail-sdk 0.17.2: the entry point is
    `sail.Image.debian(arch)` (the ImageNamespace), not `sail.image.debian`;
    accepted arch values are "amd64" (default) and "arm64".
    """
    import sail

    return (
        sail.Image.debian("amd64", install_python=False)
        .apt_install("git", "ripgrep", "curl", "tar", "gzip")
        .pip_install("pytest")
    )


# The worker agent CLI: the default worker harness is
# headless opencode — the SAME binary the eval's main agents run
# (pinned v2.2.5, probe-verified in-box). The original design pointed
# workers at a `pi` CLI whose install channel was never verified
# (the harness choice was still open), so every swarm worker died at
# Popen(["pi", ...]) and the pipeline laundered the empty patches
# into "delivered redesigns" (found: 28/28 proposals
# tests-only, 100% of workers exit_reason=error).
_OC_URL = ("https://github.com/anomalyco/opencode/releases/download/"
           "v2.2.5/opencode-linux-x64.tar.gz")

HARNESS_INSTALL = (
    "curl -fsSL " + _OC_URL + " -o /tmp/oc.tar.gz "
    "&& mkdir -p /opt/oc && tar -xzf /tmp/oc.tar.gz -C /opt/oc "
    "&& ln -sf /opt/oc/opencode /usr/local/bin/opencode "
    "&& opencode --version"
)


class EnvManager:
    def __init__(self, cfg: HindsightConfig, store: Store, sail: SailClient, snapshotter: Snapshotter):
        self.cfg = cfg
        self.store = store
        self.sail = sail
        self.snapshotter = snapshotter
        self._building: dict[str, str] = {}  # key -> box_id (in-flight builds)

    def ensure_env(self, commit: str) -> EnvRecord:
        """Return a fresh-or-cached env for the dependency state at `commit`.

        Since we cannot read the manifests out of a tarball without exporting,
        the key is computed from the live project dir (dev-local mode: the
        orchestrator runs on the user's machine, so the dir is authoritative).
        """
        key = env_key(self.cfg.project_dir, self.cfg.project.dockerfile)
        row = self.store.get_env(key)
        if row is not None and row["expires_at"] > time.time():
            return EnvRecord(key, row["checkpoint_id"], row["expires_at"],
                             json.loads(row["setup_json"]), json.loads(row["flaky_tests_json"]))
        return self._build(key, commit)

    def _build(self, key: str, commit: str) -> EnvRecord:
        import tempfile

        from .snapshot import SecretsFound

        t0 = time.time()
        project = self.cfg.project
        image = project_image(project.dockerfile) if project.dockerfile else base_image()
        box = self.sail.create_box(f"hs-envbuild-{key[:10]}", image=image)
        self._building[key] = box.id
        try:
            # upload snapshot content
            with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tf:
                tarball = Path(tf.name)
            try:
                self.snapshotter.export_tarball(commit, tarball)
            except SecretsFound as e:
                self.sail.terminate(box.id)
                raise
            self.sail.upload_file(box.id, str(tarball), "/work/repo.tar.gz")
            tarball.unlink(missing_ok=True)

            self.sail.run(box.id, [
                "bash", "-c",
                "mkdir -p /repo /work && tar -xzf /work/repo.tar.gz -C /repo "
                "&& cd /repo && git init -q && git add -A "
                "&& git -c user.name=hindsight -c user.email=h@localhost commit -qm baseline",
            ], timeout=600)

            if project.repo_alias:
                self.sail.run(box.id, ["bash", "-c",
                                       f"rm -rf {project.repo_alias} "
                                       f"&& ln -s /repo {project.repo_alias}"], timeout=60)
            install = self.cfg.project.install_command
            setup: dict = {"install_command": install or "(none)"}
            if install:
                r = self.sail.run(box.id, ["bash", "-c", f"cd /repo && {install}"], timeout=1800)
                if r.exit_code != 0:
                    # Not built: setup agent fallback
                    self.sail.terminate(box.id)
                    raise SailClientError(f"install_command failed: {r.stderr[-500:]}")

            # Worker agent CLI: installed INTO THE CHECKPOINT
            # so every worker/matrix box boots with it. Without this the
            # default worker harness cannot start (see HARNESS_INSTALL
            # note) — and the failure is otherwise silent. The install
            # command ends with `opencode --version`, so a missing/failing
            # CLI is a nonzero exit.
            r = self.sail.run(box.id, ["bash", "-c", HARNESS_INSTALL],
                              timeout=900)
            if r.exit_code != 0:
                self.sail.terminate(box.id)
                raise SailClientError(
                    f"worker harness install failed: "
                    f"{(r.stdout or '')[-200:]} {(r.stderr or '')[-200:]}")

            flaky = self._verify_tests(box.id)
            checkpoint_id = self.sail.checkpoint(
                box.id, ttl_seconds=30 * 24 * 3600, name=f"env-{key[:10]}")
        finally:
            self.sail.terminate(box.id)
            self._building.pop(key, None)

        record = EnvRecord(key, checkpoint_id, time.time() + 30 * 24 * 3600, setup, flaky)
        self.store.save_env(key, checkpoint_id, record.expires_at, setup, flaky)
        from .store import CostEntry
        self.store.record_cost(CostEntry(ref=f"envbuild-{key[:10]}", kind="box",
                                         box_seconds=time.time() - t0, usd=0.002 * (time.time() - t0) / 60))
        return record

    def _verify_tests(self, box_id: str) -> list[str]:
        """Run the test command 3 times; tests with inconsistent results are
        marked flaky and excluded from gate decisions."""
        cmd = self.cfg.project.test_command
        if not cmd:
            return []
        if "pytest" in cmd and "--junitxml" not in cmd:
            cmd = f"{cmd} --junitxml=/work/junit.xml"
        results: list[set[str]] = []
        for i in range(3):
            r = self.sail.run(box_id, ["bash", "-c", f"cd /repo && {cmd}"], timeout=1800)
            tests = self._parse_junit(r.stdout)
            if not tests:
                # junit file lives in the box; fetch it
                raw = self.sail.read_file(box_id, "/work/junit.xml")
                tests = self._parse_junit_bytes(raw)
            results.append(tests)
            if r.exit_code != 0 and i == 0:
                # suite doesn't run at all; no flaky detection possible
                return []
        if len(results) < 2:
            return []
        flaky: set[str] = set()
        for name in results[0]:
            outcomes = [name in r for r in results]
            if len(set(outcomes)) > 1:
                flaky.add(name)
        return sorted(flaky)

    def _parse_junit(self, _stdout: str) -> set[str]:
        return set()

    @staticmethod
    def _parse_junit_bytes(raw: bytes) -> set[str]:
        import xml.etree.ElementTree as ET

        try:
            tree = ET.fromstring(raw)
        except ET.ParseError:
            return set()
        out = set()
        for tc in tree.iter("testcase"):
            out.add((tc.get("classname") or "") + "::" + (tc.get("name") or ""))
        return out
