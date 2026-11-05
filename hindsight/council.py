"""The design council (patterned on karpathy/llm-council).

  Stage 1  the run's workers each build a redesign.
  Stage 2  up to `council.reviewers` critique calls, one per review LENS, each
           evaluating every design against the current code, anonymized
           ("Proposal A" ...). Each critique ends with a "FINAL RANKING:" list.
  Stage 3  a chairman reads the user's verbatim messages, the designs and the
           critiques, picks one design and writes the plan for finishing it.

Council texts (critiques, chairman raw) are stored per run for audit.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from .config import HindsightConfig
from .models import call_context
from .prompts import load, render
from .selection import product_patch
from .store import Store

log = logging.getLogger("hindsight")

# Enough for a whole redesign's product code, within what a model can read at once.
_DESIGN_CHARS = 100_000
_ALL_DESIGNS_CHARS = 400_000
_DIRECTIVE_HEADER = "###DIRECTIVE"
_BASE = re.compile(r"^###BASE:?[ \t]*(?:Proposal[ \t]+)?([A-Z])\b.*$", re.M)
_NO_CHANGE = re.compile(r"^###NO_CHANGE[ \t]*$(.*)", re.M | re.S)

# Review lenses (stage 2): distinct evaluation mandates, so the N critique
# calls are genuinely independent judgments rather than N samples of one.
REVIEW_LENSES = (
    ("requirements auditor",
     "verify the design does EVERYTHING the user asked for; hunt for requests "
     "met only narrowly or in the summary's claims but not in the patch, and "
     "for behaviors the current code gets right that this design might "
     "regress"),
    ("simplicity advocate",
     "judge structural quality: the clearest structure for everything the user "
     "asked wins; penalize incidental churn, dead abstraction and patches bolted "
     "onto a design that needed rethinking. Old code moved into new files is not "
     "a better structure"),
    ("scope examiner",
     "judge whether each design does only what the user's words ask for; hunt "
     "for invented requirements, unrequested behavior changes, and strictness "
     "or validation nobody asked for"),
)


def label_for(index: int) -> str:
    return chr(65 + index)  # A, B, C, ...


def parse_ranking_from_text(text: str, n_labels: int) -> list[str]:
    """Karpathy-style parse of the FINAL RANKING section into proposal labels.

    Numbered lines ("1. Proposal A") win; fallback is label-mention order in
    the ranking section, then in the whole text. Labels beyond the known set
    are dropped; duplicates collapse (first mention keeps the position)."""
    labels = [label_for(i) for i in range(n_labels)]
    valid = set(labels)

    def scan(section: str) -> list[str]:
        numbered = re.findall(r"\d+\.\s*(?:Proposal|Response)\s+([A-Z])\b",
                              section)
        found = numbered or re.findall(r"(?:Proposal|Response)\s+([A-Z])\b",
                                       section)
        out: list[str] = []
        for lab in found:
            if lab in valid and lab not in out:
                out.append(lab)
        return out

    section = text.split("FINAL RANKING:", 1)[1] if "FINAL RANKING:" in text \
        else text
    out = scan(section)
    if not out and section is not text:
        out = scan(text)
    return out


def calculate_aggregate_rankings(
    critiques: list[dict],
    label_to_proposal: dict[str, str],
) -> list[dict]:
    """Average position per proposal across critics, best first — the
    llm-council aggregate (rankings may be partial)."""
    positions: dict[str, list[int]] = {pid: [] for pid in
                                       label_to_proposal.values()}
    for c in critiques:
        for pos, label in enumerate(c.get("ranking") or [], start=1):
            pid = label_to_proposal.get(label)
            if pid is not None:
                positions.setdefault(pid, []).append(pos)
    aggregate = [
        {"proposal_id": pid,
         "average_rank": round(sum(ps) / len(ps), 2),
         "rankings_count": len(ps)}
        for pid, ps in positions.items() if ps
    ]
    aggregate.sort(key=lambda x: x["average_rank"])
    return aggregate


def proposal_brief(label: str, p: dict, max_chars: int = _DESIGN_CHARS) -> str:
    """Anonymized design shown to critics and the chairman, with its product code
    and without its tests."""
    summary = p.get("summary") or {}
    if isinstance(summary, dict):
        # A worker writes this summary itself, so its shape isn't guaranteed: a list
        # entry has arrived as a dict, which crashed the review (2026-11-05).
        raw = summary.get("addresses") or []
        if not isinstance(raw, list):
            raw = [raw]
        addresses = ", ".join(
            str(a.get("id") or a.get("requirement") or a) if isinstance(a, dict)
            else str(a) for a in raw) or "(unstated)"
        summary_text = str(summary.get("summary") or "")
        risks = str(summary.get("risks") or "")
        assumptions = "; ".join(
            str(a.get("assumption") or "") for a in
            (summary.get("assumptions") or []) if isinstance(a, dict))
    else:  # legacy: plain string summary
        addresses, summary_text, risks, assumptions = "", str(summary), "", ""
    stat = p.get("diff_stat") or {}
    whole = product_patch(p.get("patch") or "")
    patch = whole[:max_chars]
    cut = (f"\n... ({len(whole) - max_chars} more characters not shown)"
           if len(whole) > max_chars else "")
    tests = p.get("worker_tests") or {}
    return (
        f"### Proposal {label}\n"
        f"Author's claimed addresses: {addresses}\n"
        f"Author's summary: {summary_text}\n"
        f"Author's risks: {risks or '(none stated)'}\n"
        f"Author's assumptions: {assumptions or '(none stated)'}\n"
        f"Diff: +{stat.get('added', '?')}/-{stat.get('removed', '?')} lines, "
        f"{stat.get('files', '?')} file(s)\n"
        f"Tests when it finished: {tests.get('passed', '?')} passed, "
        f"{tests.get('failed', '?')} failed\n"
        f"Patch, without tests:\n```diff\n{patch}{cut}\n```"
    )


def _chosen(raw: str, label_to_proposal: dict[str, str],
            aggregate: list[dict]) -> Optional[str]:
    """The design the chairman named, or else the critics' first choice."""
    named = _BASE.search(raw or "")
    if named and named.group(1) in label_to_proposal:
        return label_to_proposal[named.group(1)]
    log.warning("chairman named no design; using the critics' first choice")
    return aggregate[0]["proposal_id"] if aggregate else None


def _briefs(labelled: list[tuple[str, dict]]) -> str:
    share = min(_DESIGN_CHARS, _ALL_DESIGNS_CHARS // max(len(labelled), 1))
    return "\n\n".join(proposal_brief(label, p, share) for label, p in labelled)


class Council:
    """Stages 2+3. Never raises: every model call degrades to a recorded
    empty result, and without a chairman the run sends nothing."""

    def __init__(
        self,
        cfg: HindsightConfig,
        store: Store,
        model_fn: Callable[[str], str],
        model_name: str = "",
    ):
        self.cfg = cfg
        self.store = store
        self.model_fn = model_fn
        self.model_name = model_name

    # ------------------------------------------------------------ stage 2

    def critique_round(
        self,
        run_id: str,
        user_messages: str,
        qualified: list[dict],
        current_code: str = "(not available)",
    ) -> tuple[list[dict], dict[str, str]]:
        """One critique call per review lens (capped by `reviewers`), each
        evaluating ALL qualified designs, anonymized. Returns (saved
        critiques, label -> proposal_id)."""
        label_to_proposal = {
            label_for(i): p["id"] for i, p in enumerate(qualified)}
        candidates = _briefs([(label_for(i), p) for i, p in enumerate(qualified)])
        lenses = REVIEW_LENSES[: max(0, self.cfg.council.reviewers)]

        def critique(lens: tuple[str, str]) -> Optional[str]:
            lens_name, lens_instruction = lens
            prompt = render(
                load("council_critique"),
                lens_name=lens_name,
                lens_instruction=lens_instruction,
                user_messages=user_messages,
                current_code=current_code,
                candidates=candidates,
            )
            try:
                with call_context(step="council_critique", run_id=run_id):
                    return self.model_fn(prompt)
            except Exception:  # noqa: BLE001 - one critic down is not fatal
                log.exception("council critique failed (lens %s)", lens_name)
                return None

        # Together: one at a time, three 20-minute critiques would cost an hour.
        with ThreadPoolExecutor(max_workers=max(1, len(lenses))) as pool:
            replies = list(pool.map(critique, lenses))

        critiques: list[dict] = []
        for (lens_name, _), raw in zip(lenses, replies):
            if raw is None:
                continue
            ranking = parse_ranking_from_text(raw, len(qualified))
            self.store.save_critique(
                run_id, lens_name, label_to_proposal, ranking, raw,
                model=self.model_name)
            critiques.append({
                "reviewer": lens_name,
                "labels": label_to_proposal,
                "ranking": ranking, "raw": raw,
            })
        return critiques, label_to_proposal

    # ------------------------------------------------------------ stage 3

    def chairman_directive(
        self,
        run_id: str,
        user_messages: str,
        qualified: list[dict],
        critiques: list[dict],
        label_to_proposal: dict[str, str],
        current_code: str = "(not available)",
    ) -> Optional[dict]:
        """The chairman reads designs + critiques, picks a design and writes the plan
        for finishing it. None when the model call yields no usable plan."""
        aggregate = calculate_aggregate_rankings(critiques, label_to_proposal)
        candidates = _briefs(list(zip(label_to_proposal, qualified)))
        critique_text = "\n\n---\n\n".join(
            f"Critique by the {c['reviewer']}:\n{c['raw']}"
            for c in critiques) or "(no critiques returned)"
        pid_to_label = {pid: lab for lab, pid in label_to_proposal.items()}
        aggregate_text = "\n".join(
            f"{i}. Proposal {pid_to_label[a['proposal_id']]} "
            f"(avg rank {a['average_rank']}, {a['rankings_count']} ranking(s))"
            for i, a in enumerate(aggregate, start=1)
        ) or "(no rankings — critiques did not parse)"
        prompt = render(
            load("council_chairman"),
            n_candidates=str(len(qualified)),
            user_messages=user_messages,
            current_code=current_code,
            candidates=candidates,
            critiques=critique_text,
            aggregate=aggregate_text,
            test_command=self.cfg.project.test_command,
        )
        try:
            with call_context(step="council_chairman", run_id=run_id):
                raw = self.model_fn(prompt)
        except Exception:  # noqa: BLE001
            log.exception("council chairman failed for run %s", run_id)
            return None
        parsed = self._parse_chairman(raw, aggregate)
        if parsed is not None and not parsed.get("no_change"):
            parsed["chosen_proposal_id"] = _chosen(raw, label_to_proposal, aggregate)
        return parsed

    @staticmethod
    def _parse_chairman(raw: str, aggregate: list[dict]) -> Optional[dict]:
        raw = raw or ""
        none = _NO_CHANGE.search(raw)
        if none:
            return {"rationale": none.group(1).strip(), "directive": "", "no_change": True,
                    "aggregate": aggregate, "raw": raw}
        idx = raw.find(_DIRECTIVE_HEADER)
        if idx < 0:
            log.warning("chairman answer lacked %s", _DIRECTIVE_HEADER)
            return None
        rationale = _BASE.sub("", raw[:idx]).strip()
        directive = _BASE.sub("", raw[idx + len(_DIRECTIVE_HEADER):]).strip()
        if not directive:
            return None
        return {"rationale": rationale, "directive": directive,
                "aggregate": aggregate, "raw": raw}
