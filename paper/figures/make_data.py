"""Extracts plot data from saved run files into figures/data/*.csv. Standard library only.

Usage: python3 -I paper/figures/make_data.py [data folder, default ~/hindsight-paper-data]
Every value comes from a run file: nothing is typed by hand, and no transcript text is copied.
All runs used here are debug or stopped runs, not results.
"""
import csv
import glob
import json
import os
import shutil
import sqlite3
import sys
import tempfile

DATA = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/hindsight-paper-data")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
R1 = os.path.join(DATA, "slop", "redesign1")


def open_store(folder):
    """Closed stores keep their data in WAL files, so copy all of them, then read the copy."""
    tmp = tempfile.mkdtemp()
    for path in glob.glob(os.path.join(folder, "events.sqlite3*")):
        shutil.copy(path, tmp)
    db = sqlite3.connect(os.path.join(tmp, "events.sqlite3"))
    db.row_factory = sqlite3.Row
    return db


def write(name, header, rows):
    with open(os.path.join(OUT, name), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"{name}: {len(rows)} rows")


def is_final(row):
    return bool(json.loads(row["budget_json"] or "{}").get("synthesis"))


def timeline():
    """Figure 1: one session, execution_server. Seconds since its first round ended."""
    db = open_store(os.path.join(R1, "hs", "execution_server"))
    result = json.load(open(os.path.join(R1, "execution_server.json")))
    turns = db.execute("select n, completed_at from turns where completed_at is not null "
                       "order by n").fetchall()
    zero = turns[0]["completed_at"] - result["rounds"][0]["agent_seconds"]
    rows = []
    for turn, rnd in zip(turns, result["rounds"]):
        end = turn["completed_at"] - zero
        rows.append(["main", f"Round {rnd['round']}", round(end - rnd["agent_seconds"]), round(end), ""])
    run = db.execute("select * from runs").fetchone()
    for e in db.execute("select * from explorations order by created_at"):
        kind = "final" if is_final(e) else "worker"
        # finished_at is when a losing worker was discarded, so use the end of its own work.
        end = db.execute("select ts from exploration_events where exploration_id=? and to_state in "
                         "('GATING','FAILED','SHOWN','READY') order by ts limit 1", (e["id"],)).fetchone()
        stop = end["ts"] if end else e["finished_at"]
        rows.append([kind, e["strategy"], round(e["created_at"] - zero), round(stop - zero),
                     e["exit_reason"] or ""])
    for m in db.execute("select step, ts, seconds from model_calls where run_id=? or step in "
                        "('design_review','design_directions') order by ts", (run["id"],)):
        if m["step"] in ("design_directions", "council_critique", "council_chairman"):
            # ts is when the call started: each critic's start plus its seconds meets the next.
            rows.append(["review", m["step"], round(m["ts"] - zero),
                         round(m["ts"] + (m["seconds"] or 0) - zero), ""])
    nudge = db.execute("select shown_at from nudges where shown_at is not null").fetchone()
    if nudge:
        rows.append(["delivery", "nudge", round(nudge["shown_at"] - zero), round(nudge["shown_at"] - zero), ""])
    write("timeline.csv", ["lane", "label", "start_s", "end_s", "state"], rows)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    timeline()
