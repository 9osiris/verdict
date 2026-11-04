"""scripted checks: the assertions a run has to satisfy.

checks.json is a list of {"type": ..., ...params, "name": optional}.
each check returns a CheckResult: pass, fail, or skip (skip means the
trace has no data for it, e.g. max_cost on a council transcript).
unknown types and bad params fail at load, loudly, not mid-run.
"""

import re


class CheckResult:
    def __init__(self, name, status, detail=""):
        # status is pass, fail, or skip
        self.name = name
        self.status = status
        self.detail = detail

    def to_dict(self):
        return {"name": self.name, "status": self.status,
                "detail": self.detail}

    def __repr__(self):
        return "CheckResult(%r, %r)" % (self.name, self.status)


def _check_max_steps(spec, trace):
    n = len(trace.call_turns())
    limit = spec["max"]
    if n <= limit:
        return ("pass", "%d tool calls, budget %d" % (n, limit))
    return ("fail", "%d tool calls, over budget %d" % (n, limit))


def _check_required_tools(spec, trace):
    names = set(trace.tool_names())
    missing = [t for t in spec["tools"] if t not in names]
    if not missing:
        return ("pass", "all present: %s" % ", ".join(spec["tools"]))
    return ("fail", "never called: %s" % ", ".join(missing))


def _check_forbidden_tools(spec, trace):
    names = trace.tool_names()
    hit = [t for t in spec["tools"] if t in names]
    if not hit:
        return ("pass", "none called")
    return ("fail", "called: %s" % ", ".join(sorted(set(hit))))


def _check_milestones(spec, trace):
    # the tools must appear in order (other calls allowed between)
    want = list(spec["tools"])
    names = trace.tool_names()
    i = 0
    for n in names:
        if i < len(want) and n == want[i]:
            i += 1
    if i == len(want):
        return ("pass", "in order: %s" % " -> ".join(want))
    return ("fail", "order broken at %r (want %s)" %
            (want[i], " -> ".join(want)))


def _check_final_contains(spec, trace):
    final = trace.final_answer()
    if final is None:
        return ("skip", "no assistant turn in trace")
    if spec["text"] in final:
        return ("pass", "final answer contains %r" % spec["text"])
    return ("fail", "final answer missing %r" % spec["text"])


def _check_final_regex(spec, trace):
    final = trace.final_answer()
    if final is None:
        return ("skip", "no assistant turn in trace")
    if re.search(spec["pattern"], final):
        return ("pass", "final answer matches /%s/" % spec["pattern"])
    return ("fail", "final answer does not match /%s/" % spec["pattern"])


def _check_no_errors(spec, trace):
    bad = []
    for t in trace.tool_turns():
        c = t.content.strip()
        if c.lower().startswith("error:") or "traceback" in c.lower():
            bad.append(t.name or "?")
    if not bad:
        return ("pass", "no tool errors")
    return ("fail", "errors in: %s" % ", ".join(sorted(set(bad))))


def _check_max_cost(spec, trace):
    if trace.cost is None:
        return ("skip", "trace has no cost data")
    if trace.cost <= spec["max"]:
        return ("pass", "$%.4f, budget $%.4f" % (trace.cost, spec["max"]))
    return ("fail", "$%.4f over budget $%.4f" % (trace.cost, spec["max"]))


def _check_max_duration(spec, trace):
    if trace.duration_ms is None:
        return ("skip", "trace has no duration data")
    if trace.duration_ms <= spec["max_ms"]:
        return ("pass", "%dms, budget %dms" %
                (trace.duration_ms, spec["max_ms"]))
    return ("fail", "%dms over budget %dms" %
            (trace.duration_ms, spec["max_ms"]))


# type -> (required params, function)
_REGISTRY = {
    "max_steps": (["max"], _check_max_steps),
    "required_tools": (["tools"], _check_required_tools),
    "forbidden_tools": (["tools"], _check_forbidden_tools),
    "milestones": (["tools"], _check_milestones),
    "final_contains": (["text"], _check_final_contains),
    "final_regex": (["pattern"], _check_final_regex),
    "no_errors": ([], _check_no_errors),
    "max_cost": (["max"], _check_max_cost),
    "max_duration": (["max_ms"], _check_max_duration),
}


def load_checks(path):
    # returns the validated spec list. raises ValueError on anything off.
    import json
    with open(path, encoding="utf-8") as f:
        try:
            specs = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError("%s: not json (%s)" % (path, e))
    if not isinstance(specs, list):
        raise ValueError("%s: top level must be a list of checks" % path)
    for i, spec in enumerate(specs):
        where = "%s check %d" % (path, i)
        if not isinstance(spec, dict):
            raise ValueError("%s: must be an object" % where)
        ctype = spec.get("type")
        if ctype not in _REGISTRY:
            raise ValueError("%s: unknown check type %r (known: %s)" %
                             (where, ctype, ", ".join(sorted(_REGISTRY))))
        params, _ = _REGISTRY[ctype]
        missing = [p for p in params if p not in spec]
        if missing:
            raise ValueError("%s (%s): missing params %s" %
                             (where, ctype, ", ".join(missing)))
        if ctype == "final_regex":
            try:
                re.compile(spec["pattern"])
            except re.error as e:
                raise ValueError("%s: bad regex %r (%s)" %
                                 (where, spec["pattern"], e))
        for num in ("max", "max_ms"):
            if num in params and not isinstance(spec[num], (int, float)):
                raise ValueError("%s: %r must be a number" % (where, num))
    return specs


def run_checks(specs, trace):
    results = []
    for spec in specs:
        ctype = spec["type"]
        name = spec.get("name") or ctype
        _, fn = _REGISTRY[ctype]
        try:
            status, detail = fn(spec, trace)
        except Exception as e:
            status, detail = "fail", "check crashed: %s" % e
        results.append(CheckResult(name, status, detail))
    return results


def summarize(results):
    counts = {"pass": 0, "fail": 0, "skip": 0}
    for r in results:
        counts[r.status] += 1
    return counts
