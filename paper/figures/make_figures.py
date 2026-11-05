"""Draws the paper's figures as SVG, then renders PDF and PNG with a local Chrome.

Usage: python3 -I paper/figures/make_figures.py
Data figures read figures/data/*.csv, which make_data.py extracts from the saved runs.
Standard library only, so the figures can be rebuilt without installing anything.
"""
import csv
import html
import os
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

INK, INK2, MUTED = "#1f2328", "#57606a", "#8b949e"
GRID, SURFACE, TINT = "#e4e6e8", "#ffffff", "#f3f4f6"
BLUE, ORANGE, AQUA, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
FONT = "Helvetica, Arial, sans-serif"
SHARED_FONT = "'Avenir Next', 'Segoe UI', Helvetica, Arial, sans-serif"


class Svg:
    def __init__(self, width, height):
        self.w, self.h, self.parts = width, height, []
        self.font = FONT

    def rect(self, x, y, w, h, fill=SURFACE, stroke="none", sw=1, r=3, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(w, .5):.1f}" height="{h:.1f}" '
                          f'rx="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>')

    def line(self, x1, y1, x2, y2, stroke=GRID, sw=1, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                          f'stroke="{stroke}" stroke-width="{sw}"{d}/>')

    def circle(self, x, y, r, fill=BLUE, stroke=SURFACE, sw=1):
        self.parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{fill}" '
                          f'stroke="{stroke}" stroke-width="{sw}"/>')

    def diamond(self, x, y, r, fill=INK):
        self.parts.append(f'<path d="M{x} {y - r}L{x + r} {y}L{x} {y + r}L{x - r} {y}Z" fill="{fill}" '
                          f'stroke="{SURFACE}" stroke-width="1.5"/>')

    def text(self, x, y, s, size=12, fill=INK, anchor="start", weight="normal"):
        size = size * 1.3
        self.parts.append(f'<text x="{x:.1f}" y="{y:.1f}" font-family="{self.font}" font-size="{size}" '
                          f'fill="{fill}" text-anchor="{anchor}" font-weight="{weight}">'
                          f'{html.escape(s)}</text>')

    def lines(self, x, y, rows, size=11, fill=INK, anchor="middle", gap=None, first_bold=False):
        gap = gap or size * 1.6
        for i, row in enumerate(rows):
            self.text(x, y + i * gap, row, size, fill, anchor,
                      "bold" if first_bold and i == 0 else "normal")

    def arrow(self, points, stroke=INK2, sw=1.4, dash=None):
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            self.line(x1, y1, x2, y2, stroke, sw, dash)
        (x1, y1), (x2, y2) = points[-2], points[-1]
        dx, dy = x2 - x1, y2 - y1
        n = max((dx * dx + dy * dy) ** .5, 1)
        ux, uy = dx / n, dy / n
        self.parts.append(
            f'<path d="M{x2} {y2}L{x2 - 7 * ux - 3.5 * uy:.1f} {y2 - 7 * uy + 3.5 * ux:.1f}'
            f'L{x2 - 7 * ux + 3.5 * uy:.1f} {y2 - 7 * uy - 3.5 * ux:.1f}Z" fill="{stroke}"/>')

    def svg(self):
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}"><rect width="{self.w}" height="{self.h}" '
                f'fill="{SURFACE}"/>{"".join(self.parts)}</svg>')


def render(name, fig):
    """SVG to vector PDF for LaTeX, and PNG for checking by eye."""
    svg = fig.svg()
    with open(os.path.join(HERE, f"{name}.svg"), "w") as f:
        f.write(svg)
    page = (f'<!doctype html><html><head><style>@page{{size:{fig.w}px {fig.h}px;margin:0}}'
            f'html,body{{margin:0;padding:0;background:#fff}}svg{{display:block}}</style></head>'
            f'<body>{svg}</body></html>')
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "f.html")
        with open(path, "w") as f:
            f.write(page)
        base = [CHROME, "--headless=new", "--disable-gpu", f"--user-data-dir={tmp}/profile",
                "--no-first-run"]
        subprocess.run(base + ["--no-pdf-header-footer",
                               f"--print-to-pdf={HERE}/{name}.pdf", f"file://{path}"],
                       capture_output=True, timeout=120, check=True)
        subprocess.run(base + [f"--window-size={fig.w},{fig.h}", "--force-device-scale-factor=2",
                               f"--screenshot={HERE}/{name}.png", f"file://{path}"],
                       capture_output=True, timeout=120, check=True)
    print(name)


def read(name):
    with open(os.path.join(DATA, name)) as f:
        return list(csv.DictReader(f))


def timeline():
    rows = read("timeline.csv")
    fig = Svg(1000, 400)
    fig.font = SHARED_FONT
    left, right, top = 190, 975, 44
    last = max(int(r["end_s"]) for r in rows)
    span = (last // 600 + 1) * 600
    X = lambda s: left + (right - left) * s / span  # noqa: E731
    lanes = [("Main agent rounds", "main", BLUE), ("Auditor", "aud", AQUA), ("Workers", "worker", ORANGE),
             ("Critics & chairman", "rev", AQUA), ("Final worker", "final", YELLOW), ("Delivery", "delivery", INK)]
    heights = {"worker": 4}
    y, spot = top, {}
    for name, key, _ in lanes:
        spot[key] = y
        y += 26 * heights.get(key, 1) + 12 + (8 if key == "main" else 0)
    bottom = y - 4
    for tick in range(0, span + 1, 600):
        x = X(tick)
        fig.line(x, top - 8, x, bottom, GRID)
        fig.text(x, bottom + 21, f"{tick // 60}", 11, INK2, "middle")
    fig.text((left + right) / 2, bottom + 45, "Minutes since the session began", 12, INK2, "middle")
    end_round5 = max(int(r["end_s"]) for r in rows if r["lane"] == "main")
    fig.line(X(end_round5), top - 8, X(end_round5), bottom, BLUE, 1.2, "4 3")
    fig.text(X(end_round5) + 5, top - 8, "last request handled", 11, INK2)
    for name, key, _ in lanes:
        fig.text(left - 12, spot[key] + 16, name, 12, INK, "end")
    for r in rows:
        s, e = int(r["start_s"]), int(r["end_s"])
        if r["lane"] == "main":
            fig.rect(X(s), spot["main"], X(e) - X(s) - 1, 22, BLUE)
            fig.text((X(s) + X(e)) / 2, spot["main"] + 15.5, r["label"].replace("Round ", "R"), 11,
                     "#ffffff", "middle")
        elif r["lane"] == "review":
            key = "aud" if r["label"] == "design_directions" else "rev"
            fig.rect(X(s), spot[key], X(e) - X(s) - 2, 22, AQUA)
        elif r["lane"] == "final":
            fig.rect(X(s), spot["final"], X(e) - X(s), 22, YELLOW)
    workers = [r for r in rows if r["lane"] == "worker" and r["state"] == "done"]
    for i, r in enumerate(workers):
        s, e = int(r["start_s"]), int(r["end_s"])
        yy = spot["worker"] + i * 26
        fig.rect(X(s), yy, X(e) - X(s), 22, ORANGE)
        label = r["label"].replace("direction_", "Design direction ")
        fig.text(X(e) + 6, yy + 15.5, label, 11, INK2)
    d = [r for r in rows if r["lane"] == "delivery"][0]
    fig.diamond(X(int(d["end_s"])), spot["delivery"] + 11, 8)
    fig.text(X(int(d["end_s"])) - 13, spot["delivery"] + 15.5, "Design delivered to the main agent", 11, INK2, "end")
    fig.h = bottom + 56
    render("fig_timeline", fig)





def box(fig, x, y, w, h, rows, kind="code"):
    """kind: code (outline), model (tinted), sail (a Sailbox, heavy border), person."""
    styles = {"code": (SURFACE, INK2, 1, None), "model": ("#dcebfa", BLUE, 1.2, None),
              "sail": ("#fdeadf", ORANGE, 2.2, None), "person": (TINT, INK2, 1, None)}
    fill, stroke, sw, dash = styles[kind]
    fig.rect(x, y, w, h, fill, stroke, sw, 5, dash)
    fig.lines(x + w / 2, y + h / 2 - (len(rows) - 1) * 9.5 + 5, rows, 11.5, INK, first_bold=True, gap=19)


def flow():
    fig = Svg(1000, 900)
    fig.font = SHARED_FONT
    w, h = 222, 74
    four = [24, 266, 508, 750]
    three = [24, 387, 750]
    rows = [("Stage 1: monitoring and detection", 10, four,
             [(["User", "sends requests"], "person"), (["Main agent", "works on each request"], "model"),
              (["Plugin & orchestrator", "record each turn"], "code"), (["Detector", "did requirements change?"], "model")],
             [(3, "no: wait for the next turn")]),
            ("Stage 2: exploration", 162, three,
             [(["Auditor", "names 2-5 directions"], "model"), (["Workers", "one per direction + one open"], "sail"),
              (["Validity checks", "tests intact & real code change"], "code")],
             [(1, "new requests reach workers as they arrive"), (2, "none pass: stop")]),
            ("Stage 3: review", 314, four,
             [(["Critics", "three anonymous reviews"], "model"), (["Chairman", "picks one & lists changes"], "model"),
              (["Final worker", "continues that design"], "sail"), (["Final check", "tests on its starting code"], "sail")],
             [(1, "nothing worth sending: stop"), (2, "new requests still arrive"), (3, "failures listed; can't run: stop")]),
            ("Stage 4: delivery and decision", 466, four,
             [(["Delivery", "nudge at a turn gap"], "code"), (["Main agent", "reviews & recommends"], "model"),
              (["User", "agrees or declines"], "person"), (["Applied", "all, part, ideas or none"], "model")],
             [])]
    ys = []
    for title, y, xs, content, exits in rows:
        fig.rect(10, y, 980, 144, TINT, "none", r=6)
        fig.text(24, y + 24, title, 12, INK, weight="bold")
        by = y + 36
        ys.append((by, xs))
        for x, (text, kind) in zip(xs, content):
            box(fig, x, by, w, h, text, kind)
        for a, b in zip(xs, xs[1:]):
            fig.arrow([(a + w, by + h / 2), (b, by + h / 2)])
        for col, label in exits:
            # Keep the long label clear of the arrow leaving this box.
            shift = 14 if label.startswith("failures") else 0
            fig.text(xs[col] + w / 2 - shift, by + h + 20, label, 10.5, INK2, "middle")
    for i, (by, xs) in enumerate(ys[:-1]):
        nxt, nxs = ys[i + 1]
        mid = rows[i + 1][1] - 4
        fig.arrow([(xs[-1] + w - 24, by + h), (xs[-1] + w - 24, mid), (15, mid), (15, nxt + h / 2), (nxs[0], nxt + h / 2)])
    ly = 626
    x = 24
    for kind, label, step in [("model", "Model/agent call", 260), ("sail", "Sailbox", 180),
                              ("code", "User's machine", 250), ("person", "User", 150)]:
        box(fig, x, ly, 28, 20, [], kind)
        fig.text(x + 36, ly + 16, label, 11, INK2)
        x += step
    fig.h = ly + 30
    render("fig_flow", fig)


def wrap(s, n):
    out, cur = [], ""
    for word in s.split():
        if len(cur) + len(word) + 1 > n and cur:
            out.append(cur)
            cur = word
        else:
            cur = (cur + " " + word).strip()
    return out + [cur] if cur else out


def bubble(fig, x, y, width, who, body, fill=SURFACE, stroke=GRID, chars=None, sw=1):
    """A chat message. Returns the y just below it."""
    rows = []
    for para in body:
        rows += wrap(para, chars or int(width / 8.3))
    height = 34 + len(rows) * 20 + 8
    fig.rect(x, y, width, height, fill, stroke, sw, 8)
    fig.text(x + 12, y + 22, who, 10.5, INK2, weight="bold")
    for i, row in enumerate(rows):
        fig.text(x + 12, y + 44 + i * 20, row, 11, INK)
    return y + height


def request():
    fig = Svg(1000, 480)
    fig.font = SHARED_FONT
    fig.rect(10, 10, 500, 450, TINT, "none", r=6)
    fig.text(24, 36, "The user's session", 12, INK, weight="bold")
    y = bubble(fig, 24, 50, 472, "User, first request",
               ["You're building a tiny \"local runner\" web API... \"files\": optional object<string,string>"])
    y = bubble(fig, 24, y + 8, 472, "Main agent", ["Builds the server around files as plain text."], "#dcebfa", BLUE)
    y = bubble(fig, 24, y + 8, 472, "User, third request",
               ["You're extending the API so the input files map can carry structured data, not just raw text strings."],
               "#fdeadf", ORANGE, sw=2.2)
    hit = y - 50
    y = bubble(fig, 24, y + 8, 472, "Main agent", ["Adds structured files to the code it already has, and replies."], "#dcebfa", BLUE)
    y = bubble(fig, 24, y + 8, 472, "User, keeps working", ["The next request arrives while the background runs."])
    fig.rect(560, 10, 430, 450, TINT, "none", r=6)
    fig.text(574, 36, "In the background", 12, INK, weight="bold")
    box(fig, 574, 50, 402, 66, ["Detector", "did the requirements change? yes"], "model")
    box(fig, 574, 140, 402, 66, ["Auditor", "if you were starting over, what would you try?"], "model")
    fig.arrow([(775, 116), (775, 140)])
    dirs = ["File formats as a lookup table", "A Run object owns each run", "HTTP layer on a framework",
            "Stats that use constant memory", "Split into modules, thin main", "Open: no direction given"]
    fig.arrow([(775, 206), (775, 226)])
    for i, d in enumerate(dirs):
        fig.rect(574, 226 + i * 38, 402, 32, "#fdeadf", ORANGE, 1.6, 5)
        fig.text(590, 226 + i * 38 + 22, "Worker " + str(i + 1) + ": " + d, 11, INK)
    fig.arrow([(496, hit), (528, hit), (528, 83), (574, 83)], ORANGE, 1.8)
    fig.h = 470
    render("fig_request", fig)


def delivery():
    fig = Svg(1000, 660)
    fig.rect(10, 10, 980, 640, TINT, "none", r=6)
    fig.text(24, 36, "The user's session, when a design is ready", 12, INK, weight="bold")
    y = bubble(fig, 24, 50, 620, "Hindsight, to the main agent",
               ["[Hindsight delivery] This came from Hindsight, the background system, not from the user. "
                "It finished a design and is handing it to you.",
                "A redesign (+1105/-147 across 9 files), based on your code as of message #3, which may have moved since.",
                "This is a suggestion, not a patch you have to apply. You can use all of it, part of it, or only its ideas.",
                "THE DIFF: ..."], "#fdeadf", ORANGE, sw=1.6)
    y = bubble(fig, 24, y + 8, 620, "Main agent, to the user",
               ["I've reviewed the redesign diff against my current code... A Run class owns one execution's lifecycle... "
                "My recommendation: adopt all of it. Shall I go ahead and apply it?"], "#dcebfa", BLUE)
    y = bubble(fig, 24, y + 8, 620, "User", ["Go ahead and apply what you recommend."])
    y = bubble(fig, 24, y + 8, 620, "Main agent",
               ["Applying the redesign now... All 73 redesign tests pass. Now reconciling my own suite with the new rule."],
               "#dcebfa", BLUE)
    fig.rect(672, 50, 304, 268, SURFACE, INK2, 1.4, 8)
    fig.text(686, 76, "Toast, to the user", 10.5, INK2, weight="bold")
    fig.lines(686, 104, ["Hindsight: a redesign", "is ready"], 12, INK, "start", gap=21)
    for i, row in enumerate(wrap("Your background redesign is finished: an alternative version is now available...", 34)):
        fig.text(686, 160 + i * 20, row, 11, INK2)
    for i, (label, wd) in enumerate([("Review", 86), ("Adopt", 78), ("Dismiss", 92)]):
        bx = 686 + [0, 94, 180][i]
        fig.rect(bx, 262, wd, 36, TINT, INK2, 1, 5)
        fig.text(bx + wd / 2, 286, label, 11, INK, "middle")
    fig.lines(672, 352, ["The final worker's box sleeps,", "so the main agent can wake it",
                         "later with questions about", "the design."], 11, INK2, "start", gap=21)
    fig.h = max(y + 22, 460)
    fig.parts[0] = fig.parts[0].replace('height="640.0"', f'height="{fig.h - 20:.1f}"')
    render("fig_delivery", fig)



if __name__ == "__main__":
    timeline()
    flow()
    request()
    delivery()
