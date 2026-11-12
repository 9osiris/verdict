"""reports: human tables, markdown, and json for score/judge/bench."""

import json

from .checks import summarize


def score_table(source, results, notes):
    lines = ["verdict: %s" % source, ""]
    width = max([len(r.name) for r in results] + [5])
    for r in results:
        mark = {"pass": "ok", "fail": "FAIL", "skip": "skip"}[r.status]
        line = "  %-*s  %s" % (width, r.name, mark)
        if r.detail:
            line += "  (%s)" % r.detail
        lines.append(line)
    counts = summarize(results)
    lines += ["",
              "%d passed, %d failed, %d skipped" %
              (counts["pass"], counts["fail"], counts["skip"])]
    for n in notes:
        lines.append("note: %s" % n)
    return "\n".join(lines)


def judge_text(source, out):
    lines = ["verdict: %s" % source, "",
             "score: %.2f  (%s)" % (out["score"], out["verdict"]),
             "model: %s" % out["model"],
             "rationale: %s" % out["rationale"]]
    return "\n".join(lines)


def bench_table(rows):
    # rows: list of (source, results)
    lines = []
    width = max([len(s) for s, _ in rows] + [6])
    for source, results in rows:
        counts = summarize(results)
        status = "FAIL" if counts["fail"] else "ok"
        lines.append("  %-*s  %s  (%d/%d passed%s)" %
                     (width, source, status, counts["pass"],
                      counts["pass"] + counts["fail"],
                      ", %d skipped" % counts["skip"] if counts["skip"] else ""))
    return "\n".join(lines)


def history_table(records, limit=10):
    # records are oldest first; show newest first, with the pass-rate
    # delta against the previous run (the first run has nothing to
    # compare to, so it shows "-")
    rows = []
    prev_rate = None
    for rec in records:
        total = rec["pass"] + rec["fail"] + rec["skip"]
        rate = rec["pass"] / total if total else 1.0
        if prev_rate is None:
            delta = "-"
        else:
            delta = "%+d%%" % round((rate - prev_rate) * 100)
        prev_rate = rate
        rows.append((rec, total, delta))
    lines = []
    for rec, total, delta in rows[-limit:][::-1]:
        lines.append("  %s  %d traces  %d passed, %d failed, %d skipped  %s" %
                     (rec["ts"][:10], len(rec["traces"]), rec["pass"],
                      rec["fail"], rec["skip"], delta))
    return "\n".join(lines)


def _fmt(v):
    if v is None:
        return "-"
    return str(v)


def diff_table(result):
    # result is the dict from diff.diff_traces
    lines = ["verdict diff: %s vs %s" % (result["a"], result["b"]), ""]
    def _disp(r, side):
        v = _fmt(r[side])
        if r["metric"] == "duration" and r[side] is not None:
            v += "ms"
        return v

    rows = [r for r in result["rows"] if r["metric"] != "final answer"]
    width = max([len(r["metric"]) for r in rows] + [5])
    aw = max([len(_disp(r, "a")) for r in rows] + [1])
    for r in rows:
        mark = ""
        if r["same"] is False:
            mark = "  <>"
        elif r["same"] is None:
            mark = "  ?"
        lines.append("  %-*s  %-*s  %s%s" %
                     (width, r["metric"], aw, _disp(r, "a"), _disp(r, "b"), mark))
        if r["detail"]:
            lines.append("    %s" % r["detail"])
    fa = [r for r in result["rows"] if r["metric"] == "final answer"][0]
    lines.append("")
    if fa["same"]:
        lines.append("  final answer: same")
    else:
        lines.append("  final answer: differs")
        if fa["detail"]:
            lines.append("  %s" % fa["detail"].replace("\n", "\n  "))
    diffs = sum(1 for r in result["rows"] if r["same"] is False)
    lines.append("")
    lines.append("%d difference%s" % (diffs, "" if diffs == 1 else "s"))
    return "\n".join(lines)


def as_json(kind, payload):
    return json.dumps({"verdict": kind, "data": payload}, indent=2)
