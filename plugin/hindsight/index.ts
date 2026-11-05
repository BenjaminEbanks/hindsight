/**
 * Hindsight plugin for OpenCode v2.
 *
 * Server part. Sends events out, registers the in-chat tools, and hands
 * finished designs to the main agent with ctx.session.prompt (verified live on
 * OpenCode 2.2.5: an idle session starts a turn; a busy one is steered mid-turn,
 * so this waits for the turn to end). The GUI part (gui-experimental.cjs, desktop
 * app) shows the same nudges to the user as toasts + system notifications.
 *
 * Rules honored here:
 *  - Event handling never blocks the session: sends get a 200ms budget and
 *    run detached; on failure they spool to
 *    ~/.hindsight/spool/<project>.jsonl and drain on the next load.
 *  - Events from child (sub-agent) sessions are ignored.
 *  - Duplicate idle events are deduplicated on (session, message ids) — the
 *    orchestrator deduplicates again.
 *  - The orchestrator being down changes nothing for the main session.
 *
 * The v2 plugin format
 * (export default {id, setup}), ctx.event.subscribe, ctx.session.context,
 * ctx.session.get, and ctx.tool.transform were verified against the
 * installed OpenCode 2.2.5 (see docs/VERIFY.md); every access stays defensive so
 * a mismatched surface degrades, never crashes.
 */

import * as fs from "node:fs"
import * as os from "node:os"
import * as path from "node:path"

const ORCHESTRATOR_URL =
  process.env.HINDSIGHT_URL ?? "http://127.0.0.1:8787"

// Must match hindsight/delivery.py. A delivery arrives as a user message, and
// must never count as a request from the user or as permission to adopt.
const DELIVERY_MARKER = "[Hindsight delivery]"
const DELIVERY_POLL_MS = 5000

const debugLog = path.join(os.homedir(), ".hindsight", "plugin.log")
function debug(...args: unknown[]): void {
  if (process.env.HINDSIGHT_DEBUG !== "1") return
  try {
    fs.mkdirSync(path.dirname(debugLog), { recursive: true })
    fs.appendFileSync(debugLog,
      `${new Date().toISOString()} ${args.map(String).join(" ")}\n`)
  } catch {}
}

/** Role across message shapes: v2 SessionMessageInfo uses .type, older
 * surfaces used .role. */
function roleOf(m: any): string {
  return m?.role ?? m?.type ?? ""
}

/** v2 wraps run-CLI prompts in literal quotes; strip one symmetric pair. */
function stripOuterQuotes(t: string): string {
  if (typeof t !== "string" || t.length < 2) return t
  const a = t[0], b = t[t.length - 1]
  if ((a === '"' && b === '"') || (a === "'" && b === "'")) return t.slice(1, -1)
  return t
}

/** Defensive text extraction across message shapes (plain .text, parts
 * arrays of strings or {type:"text", text} objects). Reasoning parts are
 * excluded: they are not user-visible transcript content. */
function messageText(info: any): string {
  if (!info) return ""
  if (typeof info.text === "string") return stripOuterQuotes(info.text)
  const parts = info.parts ?? info.content ?? []
  if (Array.isArray(parts)) {
    return parts
      .filter((p: any) =>
        typeof p === "string" ||
        (p?.type ? p.type === "text" : typeof p?.text === "string"))
      .map((p: any) => (typeof p === "string" ? p : p?.text ?? ""))
      .join("")
  }
  return ""
}

async function api(pathName: string, init?: RequestInit): Promise<any> {
  const res = await fetch(`${ORCHESTRATOR_URL}${pathName}`, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  })
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.json()
}

const UNDO_PHRASES = [
  "undo the hindsight",
  "undo hindsight",
  "hindsight undo",
  "undo the adopt",
]

/** Tools are guarded: adopt/undo/dismiss run only when the most recent user
 * message names the proposal ID or an explicit undo phrase. */
function userAuthorized(text: string, proposalId?: string): boolean {
  const t = (text ?? "").toLowerCase()
  if (proposalId && t.includes(proposalId.toLowerCase())) return true
  if (!proposalId && UNDO_PHRASES.some((p) => t.includes(p))) return true
  return false
}

export default {
  id: "hindsight",
  setup(ctx: any) {
    const project =
      ctx?.location?.directory ?? process.cwd()
    const spoolDir = path.join(os.homedir(), ".hindsight", "spool")
    const spoolFile = path.join(
      spoolDir,
      `project-${Buffer.from(project).toString("base64url").slice(0, 16)}.jsonl`)

    // seen user-message ids: first sighting of a user message = pre-turn
    const seenUserMessages = new Set<string>()
    // sessionID -> is a child (sub-agent) session
    const childSessions = new Map<string, boolean>()
    // most recent user text (tool authorization)
    let lastUserText = ""

    function send(event: Record<string, unknown>): void {
      void (async () => {
        const body = JSON.stringify({ ...event, ts: Date.now() / 1000 })
        try {
          const ac = new AbortController()
          const timer = setTimeout(() => ac.abort(), 200)
          const res = await fetch(`${ORCHESTRATOR_URL}/events`, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body,
            signal: ac.signal,
          })
          clearTimeout(timer)
          if (!res.ok) throw new Error(`status ${res.status}`)
        } catch {
          try {
            fs.mkdirSync(spoolDir, { recursive: true })
            fs.appendFileSync(spoolFile, body + "\n")
          } catch {
            // spool itself failed; drop the event. The session must not suffer.
          }
        }
      })()
    }

    function drainSpool(): void {
      void (async () => {
        if (!fs.existsSync(spoolFile)) return
        const lines =
          fs.readFileSync(spoolFile, "utf8").split("\n").filter(Boolean)
        if (lines.length === 0) return
        const kept: string[] = []
        for (const line of lines) {
          try {
            const ac = new AbortController()
            const timer = setTimeout(() => ac.abort(), 500)
            const res = await fetch(`${ORCHESTRATOR_URL}/events`, {
              method: "POST",
              headers: { "content-type": "application/json" },
              body: line,
              signal: ac.signal,
            })
            clearTimeout(timer)
            if (!res.ok) kept.push(line)
          } catch {
            kept.push(line)
          }
        }
        try {
          if (kept.length === 0) fs.rmSync(spoolFile)
          else fs.writeFileSync(spoolFile, kept.join("\n") + "\n")
        } catch {}
      })()
    }

    async function isChildSession(sessionID: string): Promise<boolean> {
      if (!sessionID) return false
      if (childSessions.has(sessionID)) return childSessions.get(sessionID)!
      try {
        const s = await ctx.session.get({ sessionID })
        const child = Boolean(s?.parentID)
        childSessions.set(sessionID, child)
        return child
      } catch {
        return false
      }
    }

    // ------------------------------------------------------ event handling

    /** Event payload fields may be top-level or nested; both have been seen
     * across OpenCode builds. */
    function props(ev: any): any {
      return ev?.properties ?? ev?.data ?? ev ?? {}
    }
    function sessionIDOf(ev: any): string {
      const p = props(ev)
      return (
        ev?.sessionID ?? p?.sessionID ?? ev?.durable?.aggregateID ??
        p?.session ?? p?.info?.sessionID ?? "")
    }
    function messageOf(ev: any): any {
      const p = props(ev)
      return ev?.message ?? p?.info ?? p?.message ?? p ?? null
    }
    function filePathOf(ev: any): string {
      const p = props(ev)
      return (
        p?.file ?? p?.path ?? ev?.file?.path ?? p?.info?.file ?? "")
    }

    const knownSessions = new Set<string>()
    const busySessions = new Set<string>()
    const deliveryTurns = new Set<string>()
    let pushing = false

    /** Hand any finished design to its session's agent, one at a time, and only
     * between turns. Acked after the send, so a crash resends, never loses it. */
    async function pushDeliveries(): Promise<void> {
      if (pushing) return
      pushing = true
      try {
        for (const sid of knownSessions) {
          if (busySessions.has(sid)) continue
          const res = await fetch(`${ORCHESTRATOR_URL}/deliveries/pending?` +
            new URLSearchParams({ session_id: sid }))
          if (!res.ok) continue
          const [next] = (await res.json()) as { id: string; text: string }[]
          if (!next) continue
          busySessions.add(sid)
          deliveryTurns.add(sid)
          await ctx.session.prompt({ sessionID: sid, text: next.text })
          await fetch(`${ORCHESTRATOR_URL}/deliveries/${next.id}/sent`,
            { method: "POST" })
        }
      } catch (e) {
        debug(`delivery push failed: ${String(e)}`)
      } finally {
        pushing = false
      }
    }

    /** A user prompt was admitted to a session's inbox (v2:
     * session.inbox.enqueued with item.type "user" and the prompt text).
     * Fires before execution starts, so the orchestrator's pre-turn
     * snapshot lands before the agent edits anything. */
    async function handleInboxEnqueued(ev: any): Promise<void> {
      const p = props(ev)
      const item = p?.item
      if (item?.type !== "user") return
      const sid = sessionIDOf(ev)
      if (!sid) return
      if (await isChildSession(sid)) return
      knownSessions.add(sid)
      busySessions.add(sid) // a turn is coming: nothing is released before it ends
      const text = messageText(item?.payload)
      if (stripOuterQuotes(text).startsWith(DELIVERY_MARKER)) {
        deliveryTurns.add(sid)
        return
      }
      deliveryTurns.delete(sid)
      const id = p?.inboxID ?? ""
      if (id && seenUserMessages.has(id)) return
      if (id) seenUserMessages.add(id)
      lastUserText = text || lastUserText
      send({
        kind: "user_message",
        session_id: sid,
        turn_id: id || null,
        message_ids: id ? [id] : [],
      })
    }

    /** v1 surface (message.created/updated with role info): kept for
     * builds and API paths that still emit it. */
    async function handleMessageEvent(ev: any): Promise<void> {
      const m = messageOf(ev)
      const id = m?.id
      if (roleOf(m) !== "user" || !id) return
      const sid = sessionIDOf(ev)
      if (await isChildSession(sid)) return
      if (messageText(m).startsWith(DELIVERY_MARKER)) return
      if (seenUserMessages.has(id)) return
      seenUserMessages.add(id)
      lastUserText = messageText(m)
      send({
        kind: "user_message",
        session_id: sid,
        turn_id: id,
        message_ids: [id],
      })
    }

    /** The session finished working (v2: session.execution.succeeded; v1:
     * session.idle). Fetch the transcript and emit turn_completed. */
    async function handleTurnEnd(ev: any): Promise<void> {
      const sid = sessionIDOf(ev)
      if (!sid) return
      if (await isChildSession(sid)) return
      knownSessions.add(sid)
      // The agent's review of a delivery is not a turn the user asked for.
      if (deliveryTurns.delete(sid)) return
      let msgs: readonly any[] = []
      try {
        // V2 message fetch — ctx.session.context({sessionID}).
        msgs = (await ctx.session.context({ sessionID: sid })) ?? []
      } catch {
        return
      }
      if (process.env.HINDSIGHT_DEBUG === "1") {
        try {
          debug("context fetch:",
            Array.isArray(msgs)
              ? `${msgs.length} messages: ` +
                (msgs ?? []).map((m: any) =>
                  `${roleOf(m)}(${(m?.content ?? m?.parts ?? [])
                    .map((p: any) => p?.type).join("+")})`).join(", ")
              : `non-array: ${JSON.stringify(msgs).slice(0, 800)}`)
        } catch {}
      }
      const ids = (msgs ?? []).map((m: any) => m.id)
      // NOTE: send the recent transcript window, not "new since last event".
      // The orchestrator stores these as the turn's messages (add_messages)
      // and dedupes on message ids, so re-sending is harmless while an
      // empty window would silently drop the turn's transcript.
      const messages = (msgs ?? [])
        .filter((m: any) =>
          roleOf(m) === "user" || roleOf(m) === "assistant")
        .map((m: any) => ({
          id: m.id,
          role: roleOf(m),
          text: messageText(m),
        }))
        .slice(-12)
      send({
        kind: "turn_completed",
        session_id: sid,
        message_ids: ids,
        messages,
      })
    }

    async function handleEvent(ev: any): Promise<void> {
      const type = ev?.type
      if (!type) return
      switch (type) {
        case "session.inbox.enqueued":
          await handleInboxEnqueued(ev)
          break
        case "message.updated":
          await handleMessageEvent(ev)
          break
        case "session.execution.started":
          busySessions.add(sessionIDOf(ev))
          break
        case "session.execution.succeeded":
        case "session.idle":
          busySessions.delete(sessionIDOf(ev))
          await handleTurnEnd(ev)
          void pushDeliveries()
          break
        case "session.execution.failed":
        case "session.error":
          busySessions.delete(sessionIDOf(ev))
          send({ kind: "session_error", session_id: sessionIDOf(ev) })
          void pushDeliveries()
          break
        case "message.removed":
          send({
            kind: "message_removed",
            session_id: sessionIDOf(ev),
            turn_id: messageOf(ev)?.id ?? null,
          })
          break
        // Event name against the running OpenCode version; the box
        // TTL (experiments.winner_box_ttl_hours) is the backstop if it never fires.
        case "session.deleted":
          send({ kind: "session_ended", session_id: sessionIDOf(ev) })
          break
        case "session.compaction.ended":
        case "session.compacted":
          send({ kind: "session_compacted", session_id: sessionIDOf(ev) })
          break
        case "file.edited":
          // batched at idle by the orchestrator; forwarded here
          send({
            kind: "files_edited",
            session_id: sessionIDOf(ev),
            paths: [filePathOf(ev)].filter(Boolean),
          })
          break
      }
    }

    // ------------------------------------------------------- in-chat tools

    async function authorizationText(
      sessionID: string | undefined,
    ): Promise<string> {
      if (lastUserText) return lastUserText
      // Fallback: the tool may run before any message event was seen (e.g.
      // a session resumed from history). Read the transcript directly.
      if (!sessionID) return ""
      try {
        const msgs = (await ctx.session.context({ sessionID: sessionID })) ?? []
        let text = ""
        for (const m of msgs ?? []) {
          if (roleOf(m) === "user" && !messageText(m).startsWith(DELIVERY_MARKER)) {
            text = messageText(m) || text
          }
        }
        lastUserText = text
        return text
      } catch {
        return ""
      }
    }

    void (async () => {
      try {
        await ctx.tool.transform((editor: any) => {
          editor.add({
            name: "hindsight_status",
            description:
              "Show Hindsight background-agent status and spend.",
            input: {
              type: "object",
              properties: {},
              additionalProperties: false,
            },
            execute: async () => ({
              content: JSON.stringify(await api("/status")),
            }),
          })
          editor.add({
            name: "hindsight_show",
            description:
              "Show a Hindsight proposal summary, evidence, and diff.",
            input: {
              type: "object",
              properties: { id: { type: "string" } },
              required: ["id"],
              additionalProperties: false,
            },
            execute: async (input: any) => ({
              content: JSON.stringify(
                await api(`/proposals/${input.id}`)),
            }),
          })
          editor.add({
            name: "hindsight_ask",
            description:
              "Ask about a Hindsight proposal: read-only facts (summary, assumptions, tests, diff), or with a question its finished worker is resumed to answer.",
            input: {
              type: "object",
              properties: {
                id: { type: "string" },
                question: { type: "string" },
              },
              required: ["id"],
              additionalProperties: false,
            },
            execute: async (input: any) => ({
              content: JSON.stringify(await api(`/proposals/${input.id}/ask`, {
                method: "POST",
                body: JSON.stringify({ question: input.question ?? null }),
              })),
            }),
          })
          editor.add({
            name: "hindsight_adopt",
            description:
              "Adopt a Hindsight proposal into the user's working tree (user must confirm by naming its ID).",
            input: {
              type: "object",
              properties: { id: { type: "string" } },
              required: ["id"],
              additionalProperties: false,
            },
            execute: async (input: any, context: any) => {
              const text = await authorizationText(context?.sessionID)
              if (!userAuthorized(text, input.id)) {
                return {
                  content:
                    "Not confirmed: ask the user to confirm by naming the proposal ID (e.g. 'adopt H7').",
                }
              }
              return {
                content: JSON.stringify(
                  await api(`/proposals/${input.id}/adopt`, {
                    method: "POST",
                    body: JSON.stringify({ via: "plugin" }),
                  })),
              }
            },
          })
          editor.add({
            name: "hindsight_undo",
            description:
              "Undo the last adopted Hindsight change (user must ask).",
            input: {
              type: "object",
              properties: {},
              additionalProperties: false,
            },
            execute: async (_input: any, context: any) => {
              const text = await authorizationText(context?.sessionID)
              if (!userAuthorized(text)) {
                return {
                  content:
                    "Not confirmed: ask the user to say 'undo the Hindsight change'.",
                }
              }
              return {
                content: JSON.stringify(
                  await api("/adoptions/last/undo", {
                    method: "POST",
                    body: JSON.stringify({ via: "plugin" }),
                  })),
              }
            },
          })
          editor.add({
            name: "hindsight_dismiss",
            description:
              "Dismiss a Hindsight proposal with an optional reason.",
            input: {
              type: "object",
              properties: {
                id: { type: "string" },
                reason: { type: "string" },
              },
              required: ["id"],
              additionalProperties: false,
            },
            execute: async (input: any, context: any) => {
              const text = await authorizationText(context?.sessionID)
              if (!userAuthorized(text, input.id)) {
                return {
                  content:
                    "Not confirmed: ask the user to name the proposal ID.",
                }
              }
              return {
                content: JSON.stringify(
                  await api(
                    `/proposals/${input.id}/dismiss` +
                    `?reason=${encodeURIComponent(input.reason ?? "")}`,
                    { method: "POST" })),
              }
            },
          })
          editor.add({
            name: "hindsight_explore",
            description:
              "Ask Hindsight to run background explorations for this session now.",
            input: {
              type: "object",
              properties: {
                strategies: {
                  type: "string",
                  description:
                    "Comma-separated workers, such as open_redesign or direction_1 (optional)",
                },
              },
              additionalProperties: false,
            },
            execute: async (input: any, context: any) => ({
              content: JSON.stringify(await api("/explorations", {
                method: "POST",
                body: JSON.stringify({
                  session_id: context?.sessionID ?? "",
                  strategies: input.strategies
                    ?.split(",")
                    .map((s: string) => s.trim()),
                }),
              })),
            }),
          })
        })
      } catch (e) {
        debug(`tool registration failed: ${String(e)}`)
      }
    })()

    // ------------------------------------------------------------- startup

    drainSpool()
    debug(`server part loaded (project ${project})`)

    const controller = new AbortController()
    const debugTypes = new Set([
      "session.inbox.enqueued", "session.execution.succeeded",
      "session.execution.failed", "session.idle", "message.updated",
      "message.removed", "session.compaction.ended", "file.edited",
    ])
    void (async () => {
      try {
        for await (const ev of ctx.event.subscribe({
          signal: controller.signal,
        })) {
          try {
            if (process.env.HINDSIGHT_DEBUG === "1" &&
                ev?.type && debugTypes.has(ev.type)) {
              debug(`event ${ev.type}`,
                JSON.stringify(ev).slice(0, 600))
            }
            await handleEvent(ev)
          } catch {
            // never let hindsight break the user's session
          }
        }
      } catch {
        // stream aborted (shutdown)
      }
    })()

    const deliveryTimer = setInterval(() => void pushDeliveries(), DELIVERY_POLL_MS)

    return () => {
      clearInterval(deliveryTimer)
      controller.abort()
    }
  },
}
