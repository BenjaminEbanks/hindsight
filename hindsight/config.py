"""Project configuration: .hindsight.toml loading and derived paths."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlparse

import tomllib
from pydantic import BaseModel, Field

DEFAULT_APP_NAME = "hindsight"

# Defaults for the snapshotter's private exclude list.
DEFAULT_SNAPSHOT_EXCLUDES = [
    ".env*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "*.p12",
    "*.pfx",
    "credentials*",
    ".aws/",
    ".ssh/",
    ".gnupg/",
]


class ProjectConfig(BaseModel):
    test_command: str = "python -m pytest -q"
    install_command: Optional[str] = None
    # Build worker boxes from this Dockerfile instead of the default image, for
    # projects whose toolchain it installs. The tools workers need are added.
    dockerfile: Optional[str] = None
    # Where the project expects to live, if not /repo. Linked to /repo in boxes.
    repo_alias: Optional[str] = None
    exclude: list[str] = Field(default_factory=list)
    max_file_mb: float = 5.0


class BudgetConfig(BaseModel):
    per_session_usd: float = 15.0
    per_day_usd: float = 40.0


class InterruptConfig(BaseModel):
    min_minutes_between: float = 30.0
    min_turns_between: int = 5
    max_per_session: int = 3


class AdoptConfig(BaseModel):
    mode: str = "working-tree"  # working-tree | commit | branch
    post_adopt_test: bool = True


class ModelsConfig(BaseModel):
    """Which models fill each role, and the provider that serves them.

    Any provider with an OpenAI-compatible chat-completions endpoint and bearer-token
    auth works. The defaults use Sail inference. The judge deliberately differs from
    the worker's model family, to reduce self-preference."""

    worker: str = "zai-org/GLM-5.3"  # workers and the final worker
    judge: str = "moonshotai/Kimi-K3"  # critics and chairman
    reviewer: str = "moonshotai/Kimi-K3"  # detector and auditor
    nudge: str = "zai-org/GLM-5.3-Flash"  # the sentence summarising a delivery
    # The OpenCode provider name for workers, which address models as <provider>/<model>.
    provider: str = "sail-research"
    base_url: str = "https://api.sailresearch.com/v1"
    # The environment variable holding the model key on this machine.
    api_key_env: str = "SAIL_API_KEY"
    # Some providers' newer models want max_completion_tokens instead.
    max_tokens_param: str = "max_tokens"
    # Model-name substring -> [input, output] USD per million tokens, for the budget.
    # A model matching nothing here or in budget.MODEL_PRICES is priced at the default.
    prices: dict[str, tuple[float, float]] = Field(default_factory=dict)
    # Boxes never hold the real key. This saved egress policy adds it to model calls and
    # nothing else, and a box that can't get the policy is closed. `hindsight box-key`
    # creates both the policy and the secret it reads.
    box_policy: str = "hindsight-inference-only"
    box_secret: str = "HINDSIGHT_MODEL_KEY"
    box_key_placeholder: str = "not-a-real-key"

    @property
    def uses_sail_inference(self) -> bool:
        return urlparse(self.base_url).hostname == "api.sailresearch.com"



class ExploreConfig(BaseModel):
    samples_per_strategy: int = 2
    worker_wall_clock_minutes: float = 45.0
    worker_max_tokens: int = 2_000_000
    worker_max_agent_turns: int = 200
    max_concurrent_boxes: int = 8
    poll_interval_seconds: float = 10.0


class ExperimentsConfig(BaseModel):
    """Settings treated as experiments: each is a guess,
    built as a setting so the evaluation can compare values.

    A turn the design review says yes to starts a run as soon as the turn has
    settled, and a finished design goes to the agent at the next gap between
    turns.

    winner_box_ttl_hours: how long the winning worker's box stays asleep after
    delivery, waiting for questions from the main agent.
    """

    # A cap on workers per run, including the open worker. The auditor's directions
    # set the actual number.
    workers_max: int = 8
    review_max_code_chars: int = 400_000
    settle_seconds: float = 3.0
    report_bugs: bool = False
    winner_box_ttl_hours: float = 24.0
    # A finished design waiting to be sent when the user says something new.
    # list_missed: send it at the next gap and list what it has not seen.
    # retry_final: send it back to the final worker to build those in first.
    late_messages: Literal["list_missed", "retry_final"] = "list_missed"


class CouncilConfig(BaseModel):
    """How a run's designs become one result. Workers propose; if more than one
    qualifies, critics review them anonymously and the chairman combines them
    into a plan; one final worker builds it on the latest code.

    - reviewers            critique calls, one per review lens; 0 skips critics
    - one_run_at_a_time    per session: no detection or launch while a run for
                           that session is ongoing, until its result is sent
                           or dropped. Other sessions are unaffected.
    - stream_worker_updates
                           push post-launch USER messages into worker boxes as
                           /work/updates/u-<n>.json (workers are instructed to
                           read them before finalizing)
    """

    reviewers: int = 3
    one_run_at_a_time: bool = True
    stream_worker_updates: bool = True


class HindsightConfig(BaseModel):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    interrupts: InterruptConfig = Field(default_factory=InterruptConfig)
    adopt: AdoptConfig = Field(default_factory=AdoptConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    explore: ExploreConfig = Field(default_factory=ExploreConfig)
    experiments: ExperimentsConfig = Field(default_factory=ExperimentsConfig)
    council: CouncilConfig = Field(default_factory=CouncilConfig)
    app_name: str = DEFAULT_APP_NAME

    # Derived at load time.
    project_dir: Path = Field(default_factory=Path.cwd, exclude=True)
    config_path: Optional[Path] = Field(default=None, exclude=True)
    # Override for where hindsight keeps its data (tests, eval episodes).
    hindsight_home: Optional[Path] = None

    @property
    def project_id(self) -> str:
        """Stable id for this project directory (used under ~/.hindsight/projects/)."""
        digest = hashlib.sha256(str(self.project_dir.resolve()).encode()).hexdigest()[:12]
        slug = re.sub(r"[^a-z0-9]+", "-", self.project_dir.resolve().name.lower()).strip("-")
        return f"{slug}-{digest}"

    @property
    def hindsight_dir(self) -> Path:
        if self.hindsight_home is not None:
            return self.hindsight_home
        return Path.home() / ".hindsight" / "projects" / self.project_id

    @property
    def snapshot_repo(self) -> Path:
        return self.hindsight_dir / "snap.git"

    @property
    def db_path(self) -> Path:
        return self.hindsight_dir / "events.sqlite3"

    @property
    def spool_path(self) -> Path:
        return Path.home() / ".hindsight" / "spool" / f"{self.project_id}.jsonl"

    def all_snapshot_excludes(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for pattern in DEFAULT_SNAPSHOT_EXCLUDES + list(self.project.exclude):
            if pattern not in seen:
                seen.add(pattern)
                out.append(pattern)
        return out


def load_config(project_dir: Path | str = ".", config_path: Path | None = None) -> HindsightConfig:
    """Load .hindsight.toml from project_dir (or an explicit path), with defaults."""
    project_dir = Path(project_dir).resolve()
    if config_path is None:
        config_path = project_dir / ".hindsight.toml"
    data: dict = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    cfg = HindsightConfig(**data)
    cfg.project_dir = project_dir
    cfg.config_path = config_path if config_path.exists() else None
    return cfg
