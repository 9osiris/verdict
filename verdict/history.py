"""bench history: keep past bench runs so trends show up.

each bench run recorded with --record appends one json line to
~/.verdict/history.jsonl. the file is plain jsonl so you can read it
with anything; corrupt lines are skipped on load, never fatal.
"""

import json
import os
from datetime import datetime, timezone

from .checks import summarize


def default_path():
    return os.path.join(os.path.expanduser("~"), ".verdict", "history.jsonl")


def make_record(checks_path, rows):
    # rows: list of (source, results), the same shape bench passes around
    traces = []
    total = {"pass": 0, "fail": 0, "skip": 0}
    for source, results in rows:
        counts = summarize(results)
        for k in total:
            total[k] += counts[k]
        traces.append({
            "source": os.path.basename(source),
            "pass": counts["pass"],
            "fail": counts["fail"],
            "skip": counts["skip"],
            "status": "fail" if counts["fail"] else "ok",
        })
    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checks": os.path.basename(checks_path),
        "traces": traces,
        "pass": total["pass"],
        "fail": total["fail"],
        "skip": total["skip"],
    }


def record(path, rec):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def load(path):
    records = []
    if not os.path.exists(path):
        return records
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and "ts" in rec:
                records.append(rec)
    return records
