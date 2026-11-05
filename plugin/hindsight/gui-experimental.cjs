/**
 * Hindsight plugin for OpenCode v2 — GUI part (
 * "Recipient: the user, not the main agent. Plugin toast only.").
 *
 * Runs in the OpenCode Desktop window. The server part (index.ts) never
 * polls nudges, so a headless CLI instance cannot consume a nudge meant
 * for the user's screen; only this part fetches, shows, and acks them.
 *
 * Install location (verified): this file must live in the
 * GLOBAL plugins dir (~/.config/opencode/plugins/hindsight/) — the
 * desktop's window-side plugin host lists only the sidecar server's
 * plugins. The per-project .opencode/plugins copy is picked up by the
 * project server for its server part but the window never loads a GUI
 * part from there. The orchestrator also needs CORS enabled (it has it)
 * because window fetches are cross-origin. `hindsight init` installs
 * both copies.
 *
 * Environment rules (verified against the desktop app's GUI-bundle
 * loader, 2026-11-1): bundles are evaluated with a CommonJS shim
 * (module.exports works) but WITHOUT `process` or real Node builtins
 * (`require("node:*")` resolves to inert proxies; `process` is
 * undefined) — so this file uses browser globals only. fetch,
 * setInterval, AbortController, Promise and URL all exist in the window.
 *
 * Ctx.toast.show({title, description?, variant:
 * "default"|"success"|"error"|"loading", duration?, persistent?,
 * actions?: [{label, run}]}) and ctx.attention.notify({title, message?,
 * variant, session?: {server, id}, sound?, id?}) — surfaces verified
 * against the desktop app bundle + /v2/docs/build/plugins/gui; the
 * toast itself verified live. The system notification shows
 * only while no window has focus; `id` deduplicates across windows.
 * Every access stays defensive: a mismatched surface degrades to an
 * ack, never a crash.
 *
 * Actions on the nudge toast: Review (navigate to the session when a
 * surface offers it + a detail toast with the exact chat phrases),
 * Adopt (a confirm toast first — a toast click is the user's explicit
 * confirmation, but toasts are mis-clickable — then POST
 * /proposals/{id}/adopt via:"toast" and a result toast with the honest
 * test outcome + undo hint), Dismiss (POST /proposals/{id}/dismiss so
 * the direction is not re-proposed).
 */
"use strict"

// No environment override in this part (the desktop window has no
// `process`): the orchestrator is always localhost:8787. Debug lines go
// to POST /debug/gui -> ~/.hindsight/gui-plugin.log (fire-and-forget).
var ORCHESTRATOR_URL = "http://127.0.0.1:8787"
var POLL_SECONDS = 30

function log() {
  var parts = []
  for (var i = 0; i < arguments.length; i++) parts.push(String(arguments[i]))
  setTimeout(function () {
    try {
      var ac = new AbortController()
      setTimeout(function () { ac.abort() }, 800)
      fetch(ORCHESTRATOR_URL + "/debug/gui", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ lines: [parts.join(" ")] }),
        signal: ac.signal,
      }).catch(function () {})
    } catch (e) {}
  }, 0)
}

function withTimeout(ms) {
  var ac = new AbortController()
  var t = setTimeout(function () { ac.abort() }, ms)
  return { signal: ac.signal, done: function () { clearTimeout(t) } }
}

/** Resolve a nudge's session to {server, id} so the notification can
 * open it; undefined when it is not mounted in this window. */
function sessionRef(ctx, sessionID) {
  if (!sessionID) return undefined
  try {
    var refs = ctx.sessions && ctx.sessions.list ? ctx.sessions.list() : []
    for (var i = 0; i < refs.length; i++) {
      if (refs[i] && refs[i].id === sessionID) {
        return { server: refs[i].server && refs[i].server.id, id: sessionID }
      }
    }
  } catch (e) {}
  return undefined
}

function toast(ctx, opts) {
  try {
    var show = ctx && ctx.toast && ctx.toast.show
    if (typeof show === "function") { show(opts); return true }
  } catch (e) { log("gui toast.show failed: " + String(e)) }
  return false
}

function postJSON(path, body, timeoutMs) {
  var t = withTimeout(timeoutMs || 2000)
  return fetch(ORCHESTRATOR_URL + path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body || {}),
    signal: t.signal,
  }).then(function (res) {
    t.done()
    return res.json().then(function (j) {
      return { status: res.status, body: j }
    }, function () { return { status: res.status, body: {} } })
  })
}

/** Adopt from a toast click: a labeled button click IS the user's
 * explicit confirmation (same bar as naming the ID in chat), but toasts
 * are mis-clickable, so the first click only arms a confirm toast. */
function adoptFlow(ctx, n) {
  var pid = String((n && n.proposal_id) || "")
  toast(ctx, {
    title: "Adopt " + pid + "?",
    description: "Applies the patch to your working tree and runs the " +
      "project tests. Undo any time with `hindsight undo`.",
    variant: "default",
    duration: 15000,
    actions: [
      { label: "Confirm adopt", run: function () { doAdopt(ctx, n) } },
      { label: "Cancel", run: function () {} },
    ],
  })
}

function doAdopt(ctx, n) {
  var pid = String((n && n.proposal_id) || "")
  toast(ctx, {
    title: "Adopting " + pid + "…",
    description: "Applying the patch and running the project tests — " +
      "this can take a minute.",
    variant: "loading",
    persistent: true,
  })
  // adoption runs the project test suite: allow minutes, not seconds.
  postJSON("/proposals/" + encodeURIComponent(pid) + "/adopt",
    { via: "toast" }, 600000).then(function (r) {
      if (r.status === 200) {
        var b = r.body || {}
        var ok = b.test_passed
        toast(ctx, {
          title: ok === false ? "Adopted — tests FAILED" : "Adopted " + pid,
          description: (ok === false
            ? "The patch applied but the project tests did not pass. " +
              "Undo with `hindsight undo " + (b.adoption_id || "") + "`."
            : "Patch applied" +
              (ok ? " and tests pass" : "") + ". Undo: hindsight undo " +
              (b.adoption_id || "")).slice(0, 300),
          variant: ok === false ? "error" : "success",
          duration: 15000,
        })
        log("gui adopt " + pid + ": tests_passed=" + ok)
      } else {
        toast(ctx, {
          title: "Adopt refused",
          description: String((r.body && r.body.detail) ||
            ("status " + r.status)).slice(0, 300),
          variant: "error",
          duration: 12000,
        })
        log("gui adopt " + pid + " refused: status=" + r.status)
      }
    }).catch(function (e) {
      toast(ctx, {
        title: "Adopt failed",
        description: String(e).slice(0, 300),
        variant: "error",
        duration: 12000,
      })
      log("gui adopt " + pid + " failed: " + String(e))
    })
}

function dismissFlow(ctx, n) {
  var pid = String((n && n.proposal_id) || "")
  postJSON("/proposals/" + encodeURIComponent(pid) + "/dismiss",
    { reason: "dismissed from toast" }, 2000).then(function (r) {
      if (r.status === 200) {
        toast(ctx, {
          title: "Dismissed " + pid,
          description: "This direction won't be re-proposed in this " +
            "session.",
          variant: "default",
          duration: 8000,
        })
        log("gui dismiss " + pid + " ok")
      } else {
        log("gui dismiss " + pid + ": status=" + r.status)
      }
    }).catch(function (e) {
      log("gui dismiss " + pid + " failed: " + String(e))
    })
}

/** Review: navigate to the originating session if any surface offers
 * it (defensive — shapes unverified), and always leave a detail toast
 * with the exact chat phrases so the path works even with no nav. */
function reviewFlow(ctx, n) {
  var pid = String((n && n.proposal_id) || "")
  var sid = String((n && n.session_id) || "")
  var navigated = false
  var ref = sessionRef(ctx, sid)
  if (ref) {
    try {
      var ss = ctx && ctx.sessions
      if (ss && typeof ss.open === "function") {
        ss.open(ref); navigated = true
      } else if (ss && typeof ss.select === "function") {
        ss.select(ref); navigated = true
      }
    } catch (e) { log("gui session nav failed: " + String(e)) }
  }
  toast(ctx, {
    title: "Review " + pid,
    description: (navigated
      ? "Session opened. "
      : sid ? "Open session " + sid + ". " : "") +
      "Ask the agent: \u201chindsight show " + pid + "\u201d for the diff " +
      "and evidence, or \u201cwhy " + pid + "?\u201d for the reasoning.",
    variant: "default",
    duration: 20000,
  })
}

var pollInFlight = false

function pollNudges(ctx) {
  if (pollInFlight) return
  pollInFlight = true
  void (async function () {
    try {
      var t = withTimeout(2000)
      var res = await fetch(ORCHESTRATOR_URL + "/nudges/pending", {
        signal: t.signal,
      })
      t.done()
      if (!res.ok) { log("gui poll: status " + res.status); return }
      var nudges = await res.json()
      if (nudges && nudges.length) {
        log("gui poll: " + nudges.length + " pending nudge(s)")
      }
      for (var i = 0; i < (nudges || []).length; i++) {
        var n = nudges[i]
        var displayed = false
        // In-window toast, persistent with actions: Review (navigates +
        // chat phrases), Adopt (confirm toast -> real adoption + test
        // run), Dismiss (records the dismissal so the direction is not
        // re-proposed).
        if (toast(ctx, {
          title: "Hindsight: a redesign is ready",
          description: String((n && n.text) || "").slice(0, 300),
          variant: "default",
          persistent: true,
          actions: [
            { label: "Review", run: function () { reviewFlow(ctx, n) } },
            { label: "Adopt", run: function () { adoptFlow(ctx, n) } },
            { label: "Dismiss", run: function () { dismissFlow(ctx, n) } },
          ],
        })) {
          displayed = true
          log("gui toast.show v2 ok for " + (n && n.id) + " (with actions)")
        } else {
          log("gui toast surface absent: " + typeof (ctx && ctx.toast))
        }
        // System notification + sound while the user looks elsewhere. The
        // nudge id deduplicates across windows; clicking opens the session.
        try {
          var notify = ctx && ctx.attention && ctx.attention.notify
          if (typeof notify === "function") {
            var result = await notify({
              title: "Hindsight: a redesign is ready",
              message: String((n && n.text) || "").slice(0, 300),
              id: "hindsight-nudge-" + ((n && n.id) || ""),
              session: sessionRef(ctx, n && n.session_id),
            })
            log("gui notify: " + JSON.stringify(result || null))
            if (result && result.notification) displayed = true
          }
        } catch (e) {
          log("gui attention.notify failed: " + String(e))
        }
        // Ack every fetched nudge regardless of display success: a missing
        // surface must never redeliver forever (displayed records the
        // truth for delivery metrics).
        try {
          var t2 = withTimeout(500)
          var r2 = await fetch(
            ORCHESTRATOR_URL + "/nudges/" +
              encodeURIComponent(String((n && n.id) || "")) + "/ack",
            {
              method: "POST",
              headers: { "content-type": "application/json" },
              body: JSON.stringify({ displayed: displayed }),
              signal: t2.signal,
            })
          t2.done()
          log("gui ack " + (n && n.id) + ": displayed=" + displayed +
            " status=" + r2.status)
        } catch (e) {
          log("gui ack " + (n && n.id) + " failed: " + String(e))
        }
      }
    } catch (e) {
      log("gui poll failed: " + String(e))
      // orchestrator down / slow: retry on the next tick. Never throws out.
    } finally {
      pollInFlight = false
    }
  })()
}

function setup(ctx) {
  log("gui part loaded in window")
  // Nudges are released when the session has been quiet; poll on a slow
  // timer (the ack makes consumption exact, so re-polls are harmless).
  var timer = setInterval(function () { pollNudges(ctx) },
    POLL_SECONDS * 1000)
  pollNudges(ctx)
  return function () { clearInterval(timer) }
}

var definition = { id: "hindsight", setup: setup }
module.exports = definition
module.exports.default = definition
