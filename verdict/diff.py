"""trace diff: two runs of the same task, side by side.

compares turns, tool calls, the tool sequence, cost, duration, and the
final answer. rows where a trace has no data (council transcripts carry
no cost, mule logs carry no duration) come back as same=None, unknown
instead of different, because a missing number is not a difference.
"""

from .trace import Trace  # noqa: F401  (re-exported for callers)


def _first_diff_line(a, b):
    # returns (line_no, a_line, b_line) for the first differing line,
    # or None when the texts are identical. a shorter text that is a
    # prefix of the longer one differs at the first extra line.
    alines = (a or "").splitlines()
    blines = (b or "").splitlines()
    for i in range(max(len(alines), len(blines))):
        la = alines[i] if i < len(alines) else ""
        lb = blines[i] if i < len(blines) else ""
        if la != lb:
            return (i + 1, la, lb)
    return None


def _tool_detail(a_names, b_names):
    # shared prefix of the two tool sequences, then what each side
    # does after the split.
    i = 0
    while (i < len(a_names) and i < len(b_names)
           and a_names[i] == b_names[i]):
        i += 1
    parts = []
    prefix = a_names[:i]
    parts.append("shared prefix: %s" % (
        " -> ".join(prefix) if prefix else "none"))
    if a_names[i:]:
        parts.append("a continues with %s" % " -> ".join(a_names[i:]))
    if b_names[i:]:
        parts.append("b continues with %s" % " -> ".join(b_names[i:]))
    return "; ".join(parts)


def _money(v):
    return "$%.4f" % v if v is not None else None


def diff_traces(a, b):
    rows = []

    rows.append({"metric": "turns", "a": len(a.turns), "b": len(b.turns),
                 "same": len(a.turns) == len(b.turns), "detail": ""})

    na = len(a.call_turns())
    nb = len(b.call_turns())
    rows.append({"metric": "tool calls", "a": na, "b": nb,
                 "same": na == nb, "detail": ""})

    a_names = a.tool_names()
    b_names = b.tool_names()
    rows.append({"metric": "tools",
                 "a": " -> ".join(a_names) if a_names else "(none)",
                 "b": " -> ".join(b_names) if b_names else "(none)",
                 "same": a_names == b_names,
                 "detail": "" if a_names == b_names
                           else _tool_detail(a_names, b_names)})

    if a.cost is None or b.cost is None:
        missing = "a" if a.cost is None else "b"
        if a.cost is None and b.cost is None:
            missing = "both"
        rows.append({"metric": "cost", "a": _money(a.cost),
                     "b": _money(b.cost), "same": None,
                     "detail": "no cost data in %s" % missing})
    else:
        d = a.cost - b.cost
        rows.append({"metric": "cost", "a": _money(a.cost),
                     "b": _money(b.cost), "same": a.cost == b.cost,
                     "detail": "" if d == 0 else
                               "a costs %s%.4f %s" % (
                                   "+" if d > 0 else "-",
                                   abs(d), "more" if d > 0 else "less")})

    if a.duration_ms is None or b.duration_ms is None:
        missing = "a" if a.duration_ms is None else "b"
        if a.duration_ms is None and b.duration_ms is None:
            missing = "both"
        rows.append({"metric": "duration", "a": a.duration_ms,
                     "b": b.duration_ms, "same": None,
                     "detail": "no duration data in %s" % missing})
    else:
        d = a.duration_ms - b.duration_ms
        rows.append({"metric": "duration", "a": a.duration_ms,
                     "b": b.duration_ms, "same": a.duration_ms == b.duration_ms,
                     "detail": "" if d == 0 else
                               "a is %dms %s" % (
                                   abs(d), "slower" if d > 0 else "faster")})

    fa = a.final_answer()
    fb = b.final_answer()
    if fa == fb:
        rows.append({"metric": "final answer", "a": fa, "b": fb,
                     "same": True, "detail": ""})
    else:
        first = _first_diff_line(fa, fb)
        if first is None:
            detail = "one side has no final answer"
        else:
            n, la, lb = first
            detail = ("first difference at line %d:\n"
                      "  a: %s\n  b: %s" % (
                          n, la[:100], lb[:100]))
        rows.append({"metric": "final answer", "a": fa, "b": fb,
                     "same": False, "detail": detail})

    same = not any(r["same"] is False for r in rows)
    return {"a": a.source, "b": b.source, "same": same, "rows": rows}
