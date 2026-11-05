"""Local HTTP API: the plugin and CLI talk to this."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .orchestrator import Orchestrator
from .sources.base import SessionEvent
import time
from pathlib import Path

# Module level (not inside create_app): with `from __future__ import
# annotations`, FastAPI resolves parameter types via typing.get_type_hints,
# which cannot see closure locals — a model defined inside create_app would
# silently fail body parsing (req stays None).
class ExploreReq(BaseModel):
    session_id: str
    strategies: list[str] | None = None


class AskReq(BaseModel):
    question: str | None = None


class AdoptReq(BaseModel):
    via: str = "api"


class NudgeAckReq(BaseModel):
    displayed: bool = True


class DebugGuiReq(BaseModel):
    lines: list[str] = []


def create_app(orch: Orchestrator) -> FastAPI:
    app = FastAPI(title="hindsight", version="0.1.0")
    app.state.orch = orch
    # The desktop-app window fetches nudges cross-origin (its origin is
    # the window's own scheme/host, not 127.0.0.1:8787), so CORS must be
    # permissive or every GUI-part request fails silently.
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.post("/events")
    def events(event: SessionEvent):
        decision = orch.handle_event(event)
        return {"action": decision.action}

    @app.get("/status")
    def status():
        return orch.status()

    @app.post("/explorations")
    def explore(req: ExploreReq):
        ids = orch.explore_now(req.session_id, req.strategies)
        if not ids:
            raise HTTPException(409, "no explorations launched (budget, no base snapshot, or no plans)")
        return {"exploration_ids": ids}

    @app.get("/explorations")
    def explorations():
        rows = orch.store.live_explorations()
        return [{"id": r["id"], "strategy": r["strategy"], "state": r["state"],
                 "box_id": r["box_id"], "created_at": r["created_at"]} for r in rows]

    @app.get("/inbox")
    def inbox():
        return orch.store.proposals_in_states(["INBOX", "NUDGE_READY", "SHOWN"])

    @app.get("/nudges/pending")
    def nudges_pending():
        """Released-but-undisplayed nudges, for the plugin's toast poll
       . Read-only; the ack endpoint records the display attempt."""
        rows = orch.store.pending_toast_nudges()
        return [{"id": r["id"], "proposal_id": r["proposal_id"],
                 "session_id": r["session_id"], "tier": r["tier"],
                 "text": r["text"], "released_at": r["shown_at"]}
                for r in rows]

    @app.post("/nudges/{nudge_id}/ack")
    def nudge_ack(nudge_id: str, req: NudgeAckReq | None = None):
        displayed = req.displayed if req is not None else True
        if not orch.store.mark_nudge_acked(nudge_id, displayed=displayed):
            raise HTTPException(404, "unknown or already-acked nudge")
        return {"ok": True}

    @app.get("/deliveries/pending")
    def deliveries_pending(session_id: str):
        """Designs for the plugin to hand to this session's main agent."""
        return orch.designs_for_agent(session_id)

    @app.post("/deliveries/{nudge_id}/sent")
    def delivery_sent(nudge_id: str):
        if not orch.store.mark_agent_delivery_sent(nudge_id):
            raise HTTPException(404, "unknown or already-sent delivery")
        return {"ok": True}

    @app.post("/debug/gui")
    def debug_gui(req: DebugGuiReq):
        """Debug sink for the GUI plugin part: the desktop-app window has
        no filesystem or `process`, so the GUI part posts its log lines
        here and they land in ~/.hindsight/gui-plugin.log."""
        p = Path.home() / ".hindsight" / "gui-plugin.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with p.open("a") as f:
            for line in req.lines[:20]:
                f.write(f"{stamp} {line[:500]}\n")
        return {"ok": True}

    @app.get("/proposals/{proposal_id}")
    def proposal(proposal_id: str):
        p = orch.store.get_proposal(proposal_id)
        if p is None:
            raise HTTPException(404, "unknown proposal")
        return p

    @app.post("/proposals/{proposal_id}/dismiss")
    def dismiss(proposal_id: str, reason: str = ""):
        p = orch.store.get_proposal(proposal_id)
        if p is None:
            raise HTTPException(404, "unknown proposal")
        orch.store.record_user_action(proposal_id, "dismiss", reason, via="api")
        # Dismissal memory: the fingerprint blocks
        # re-delivery of the same direction in this session.
        orch.delivery.record_dismissal(proposal_id, reason or "unspecified")
        orch.resume.release_proposal(proposal_id)
        return {"ok": True}

    @app.post("/proposals/{proposal_id}/ask")
    def ask(proposal_id: str, req: AskReq | None = None):
        """`hindsight_ask`: read-only facts, or a deeper question answered
        by the winning worker (this can take minutes — the box wakes and runs
        one agent turn)."""
        p = orch.store.get_proposal(proposal_id)
        if p is None:
            raise HTTPException(404, "unknown proposal")
        return orch.ask(proposal_id, (req.question if req else None))

    @app.post("/proposals/{proposal_id}/adopt")
    def adopt(proposal_id: str, req: AdoptReq | None = None):
        """Adopt a proposal into the user's working tree (explicit command;
        the only endpoint that writes the user's repo)."""
        from .adopt import AdoptionError

        p = orch.store.get_proposal(proposal_id)
        if p is None:
            raise HTTPException(404, "unknown proposal")
        try:
            return orch.adopt.adopt(
                proposal_id, via=(req.via if req else "api"))
        except AdoptionError as e:
            raise HTTPException(409, str(e))

    @app.post("/adoptions/{adoption_id}/undo")
    def undo_adoption(adoption_id: str, req: AdoptReq | None = None):
        from .adopt import AdoptionError

        try:
            return orch.adopt.undo(
                adoption_id, via=(req.via if req else "api"))
        except AdoptionError as e:
            raise HTTPException(404, str(e))

    @app.get("/adoptions")
    def adoptions():
        rows = orch.store.adoptions_list()
        return [{"id": r["id"], "proposal_id": r["proposal_id"],
                 "applied_at": r["applied_at"], "test_passed": r["test_passed"],
                 "undone_at": r["undone_at"]} for r in rows]

    @app.get("/budget")
    def budget():
        import time as _t
        day_spend = orch.store.spend_since(_t.time() - 24 * 3600)
        return {"spend_24h_usd": round(day_spend, 4),
                "per_day_usd": orch.cfg.budget.per_day_usd,
                "per_session_usd": orch.cfg.budget.per_session_usd}

    return app
