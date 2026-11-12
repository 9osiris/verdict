import io
import json
import os
import sys
import tempfile
import threading
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))

from verdict.trace import (Turn, Trace, parse_jsonl, parse_mule_log,
                           parse_council_md, load_trace)
from verdict.checks import (CheckResult, load_checks, run_checks, summarize)
from verdict.judge import (judge, parse_judgement, build_messages,
                           JudgeError)
from verdict.report import (score_table, bench_table, history_table,
                            diff_table, as_json)
from verdict.diff import diff_traces
from verdict.history import make_record, record, load, default_path
from verdict.cli import main

passed = failed = 0


def check(name, cond):
    global passed, failed
    print(("ok   " if cond else "FAIL ") + name)
    if cond:
        passed += 1
    else:
        failed += 1


def raises(name, fn, exc=Exception):
    try:
        fn()
    except exc:
        check(name, True)
    except Exception as e:
        check(name + " (wrong exc: %r)" % e, False)
    else:
        check(name, False)


def tmpfile(content, suffix=".jsonl"):
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
EX = os.path.join(ROOT, "examples")
RUN_JSONL = os.path.join(EX, "run.jsonl")
COUNCIL_MD = os.path.join(EX, "council.md")

MULE_LOG = (
    "mule 0.9.0 \u00b7 gpt-4o-mini \u00b7 /tmp/proj\n"
    "\u23fa read_file path=deploy.sh\n"
    "\u23bf #!/bin/sh ... +4 lines\n"
    "  step 1 \u00b7 1,200 in / 400 out \u00b7 $0.0123\n"
    "\u23fa edit_file path=deploy.sh, old=deploy() {\n"
    "\u23bf patched deploy.sh: added --retry flag\n"
    "  step 2 \u00b7 300 in / 100 out \u00b7 $0.0010\n"
    "done. the script now takes --retry N (default 3).\n"
    "cost: $0.0133\n"
)


def run1(specs, trace):
    return run_checks(specs, trace)[0]


# trace model

raises("turn rejects bad kind", lambda: Turn("nonsense", "x"),
       ValueError)
t = Trace("s", [Turn("tool", "x", name="calc")])
check("final_answer none without assistant", t.final_answer() is None)
t = Trace("s", [Turn("tool", "a", name="read_file"),
                Turn("tool", "b", name="result")])
check("tool_names skips result pseudo-turns",
      t.tool_names() == ["read_file"])
check("call_turns skips result pseudo-turns", len(t.call_turns()) == 1)
t = Trace("s", [Turn("tool", "x" * 9000, name="read_file")])
c = t.compact(max_chars=100)
check("compact truncates", "[tool:read_file]" in c and "[truncated]" in c
      and len(c) < 9000)

# jsonl parser

trace = parse_jsonl(RUN_JSONL)
check("jsonl 6 turns", len(trace.turns) == 6)
check("jsonl tool names",
      trace.tool_names() == ["read_file", "edit_file"])
check("jsonl final answer", "done" in trace.final_answer())
p = tmpfile('{"role": "system", "content": "be nice"}\n'
            '{"role": "user", "content": "hi"}\n')
trace = parse_jsonl(p)
check("jsonl skips system", len(trace.turns) == 1
      and trace.turns[0].kind == "user")
raises("jsonl bad kind",
       lambda: parse_jsonl(tmpfile('{"role": "wat", "content": "x"}\n')),
       ValueError)
raises("jsonl bad json", lambda: parse_jsonl(tmpfile("not json\n")),
       ValueError)

# council parser

trace = parse_council_md(COUNCIL_MD)
kinds = [(x.kind, x.name) for x in trace.turns]
check("council alice turn", ("assistant", "alice") in kinds)
check("council tool turn", ("tool", "calc") in kinds)
check("council final answer", ("assistant", "final") in kinds)
check("council duration", trace.duration_ms == 12500)
check("council notes mention no per-turn data",
      any("no per-turn tokens" in n for n in trace.notes))
check("council notes carry token totals",
      any("800 in / 200 out" in n for n in trace.notes))
p = tmpfile("# council run: vote\n\ntask: t\n\n## transcript\n\n"
            "### judge\n\nverdict here\n", suffix=".md")
trace = parse_council_md(p)
check("council judge section", trace.turns[0].kind == "assistant"
      and trace.turns[0].name == "judge")

# mule parser

trace = parse_mule_log(tmpfile(MULE_LOG, suffix=".log"))
check("mule tool names",
      trace.tool_names() == ["read_file", "edit_file"])
check("mule results merged into calls", len(trace.call_turns()) == 2)
check("mule result text kept",
      "patched deploy.sh" in trace.turns[1].content)
check("mule cost summed", abs(trace.cost - 0.0133) < 1e-9)
check("mule final answer", "done. the script" in trace.final_answer())
check("mule notes honest",
      any("terminal output" in n for n in trace.notes))
ascii_log = ("* read_file path=x\n| first line\n"
             "step 1 | 100 in / 50 out | $0.001\nall good\n")
trace = parse_mule_log(tmpfile(ascii_log, suffix=".log"))
check("mule ascii fallback", trace.tool_names() == ["read_file"]
      and abs(trace.cost - 0.001) < 1e-9
      and "all good" in trace.final_answer())
trace = parse_mule_log(tmpfile("mule 0.9.0\nhello\n", suffix=".log"))
check("mule banner trimmed", trace.final_answer() == "hello")

# load_trace

t = load_trace(RUN_JSONL)
check("auto jsonl", len(t.turns) == 6)
t = load_trace(COUNCIL_MD)
check("auto council", t.duration_ms == 12500)
t = load_trace(tmpfile(MULE_LOG, suffix=".log"), fmt="mule")
check("explicit mule", t.tool_names() == ["read_file", "edit_file"])
raises("unknown format", lambda: load_trace("x.log", fmt="bogus"),
       ValueError)

# checks

trace = parse_jsonl(RUN_JSONL)
check("max_steps pass",
      run1([{"type": "max_steps", "max": 20}], trace).status == "pass")
check("max_steps fail",
      run1([{"type": "max_steps", "max": 1}], trace).status == "fail")
check("required_tools pass",
      run1([{"type": "required_tools",
             "tools": ["read_file", "edit_file"]}], trace).status == "pass")
r = run1([{"type": "required_tools", "tools": ["run_shell"]}], trace)
check("required_tools fail", r.status == "fail"
      and "run_shell" in r.detail)
check("forbidden_tools pass",
      run1([{"type": "forbidden_tools",
             "tools": ["run_shell"]}], trace).status == "pass")
check("forbidden_tools fail",
      run1([{"type": "forbidden_tools",
             "tools": ["read_file"]}], trace).status == "fail")
check("milestones pass",
      run1([{"type": "milestones",
             "tools": ["read_file", "edit_file"]}], trace).status == "pass")
check("milestones fail",
      run1([{"type": "milestones",
             "tools": ["edit_file", "read_file"]}], trace).status == "fail")
check("final_contains pass",
      run1([{"type": "final_contains", "text": "done."}],
           trace).status == "pass")
check("final_contains fail",
      run1([{"type": "final_contains", "text": "nope"}],
           trace).status == "fail")
check("final_regex pass",
      run1([{"type": "final_regex", "pattern": r"--retry N"}],
           trace).status == "pass")
check("final_regex fail",
      run1([{"type": "final_regex", "pattern": r"zzz\d+"}],
           trace).status == "fail")
t = Trace("s", [Turn("tool", "x", name="calc")])
check("final check skips without assistant",
      run1([{"type": "final_contains", "text": "x"}], t).status == "skip")
check("no_errors pass",
      run1([{"type": "no_errors"}], trace).status == "pass")
t = Trace("s", [Turn("tool", "error: file not found", name="read_file")])
r = run1([{"type": "no_errors"}], t)
check("no_errors fail", r.status == "fail" and "read_file" in r.detail)
t = parse_council_md(COUNCIL_MD)
check("max_cost skips without data",
      run1([{"type": "max_cost", "max": 1.0}], t).status == "skip")
t = parse_mule_log(tmpfile(MULE_LOG, suffix=".log"))
check("max_cost pass",
      run1([{"type": "max_cost", "max": 1.0}], t).status == "pass")
check("max_cost fail",
      run1([{"type": "max_cost", "max": 0.0001}], t).status == "fail")
t = parse_council_md(COUNCIL_MD)
check("max_duration pass",
      run1([{"type": "max_duration", "max_ms": 60000}],
           t).status == "pass")
check("max_duration fail",
      run1([{"type": "max_duration", "max_ms": 100}],
           t).status == "fail")
check("max_duration skip",
      run1([{"type": "max_duration", "max_ms": 100}],
           trace).status == "skip")
r = run1([{"type": "no_errors", "name": "clean run"}], trace)
check("custom check name", r.name == "clean run")
s = summarize([CheckResult("a", "pass"), CheckResult("b", "fail"),
               CheckResult("c", "skip")])
check("summarize", s == {"pass": 1, "fail": 1, "skip": 1})

# load_checks validation

d = tempfile.mkdtemp()
p = os.path.join(d, "c.json")
with open(p, "w") as f:
    f.write('[{"type": "no_errors"}]')
check("load_checks ok", bool(load_checks(p)))
for name, content in [
        ("unknown type", '[{"type": "nope"}]'),
        ("missing param", '[{"type": "max_steps"}]'),
        ("bad regex", '[{"type": "final_regex", "pattern": "(bad"}]'),
        ("not a list", '{"type": "no_errors"}'),
        ("non-number max", '[{"type": "max_steps", "max": "x"}]')]:
    q = os.path.join(d, "b.json")
    with open(q, "w") as f:
        f.write(content)
    raises("load_checks " + name, lambda q=q: load_checks(q), ValueError)
raises("load_checks bad json",
       lambda: load_checks(tmpfile("{nope", suffix=".json")),
       ValueError)

# reports

out = score_table("t.jsonl", [CheckResult("a", "pass", "fine"),
                               CheckResult("b", "fail", "bad"),
                               CheckResult("c", "skip")], ["a note"])
check("score_table", "ok" in out and "FAIL" in out and "skip" in out
      and "1 passed, 1 failed, 1 skipped" in out
      and "note: a note" in out)
rows = [("a.jsonl", [CheckResult("x", "pass")]),
        ("b.jsonl", [CheckResult("x", "fail")])]
out = bench_table(rows)
check("bench_table", "ok" in out and "FAIL" in out)
dd = json.loads(as_json("score", {"a": 1}))
check("as_json", dd["verdict"] == "score" and dd["data"]["a"] == 1)

# cli

def run_cli(argv, cwd=None):
    old = os.getcwd()
    if cwd:
        os.chdir(cwd)
    buf = io.StringIO()
    code = None
    try:
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            try:
                main(argv)
            except SystemExit as e:
                code = e.code
    finally:
        os.chdir(old)
    return code, buf.getvalue()


code, _ = run_cli(["init"], cwd=d)
check("cli init writes files",
      code is None and os.path.exists(os.path.join(d, "checks.json"))
      and os.path.exists(os.path.join(d, "rubric.md")))
code, out = run_cli(["init"], cwd=d)
check("cli init refuses overwrite", "exists" in out)
code, out = run_cli(["init", "--force"], cwd=d)
check("cli init --force", "wrote checks.json" in out)

checks = os.path.join(d, "ok.json")
with open(checks, "w") as f:
    json.dump([{"type": "max_steps", "max": 20},
               {"type": "required_tools",
                "tools": ["read_file", "edit_file"]},
               {"type": "no_errors"}], f)
code, _ = run_cli(["score", RUN_JSONL, "-c", checks])
check("cli score exit 0", code == 0)
bad = os.path.join(d, "bad.json")
with open(bad, "w") as f:
    json.dump([{"type": "required_tools", "tools": ["run_shell"]}], f)
code, _ = run_cli(["score", RUN_JSONL, "-c", bad])
check("cli score exit 1", code == 1)
code, out = run_cli(["score", RUN_JSONL, "-c", checks, "--json"])
dd = json.loads(out)
check("cli score --json", dd["verdict"] == "score"
      and dd["data"]["results"][0]["status"] == "pass")
code, _ = run_cli(["score", "/nope/missing.jsonl", "-c", checks])
check("cli score missing trace exit 2", code == 2)
code, _ = run_cli(["score", RUN_JSONL, "-c", "/nope/c.json"])
check("cli score bad checks exit 2", code == 2)

td = os.path.join(d, "traces")
os.mkdir(td)
with open(os.path.join(td, "good.jsonl"), "w") as f:
    f.write('{"role": "tool", "name": "calc", "content": "2"}\n')
with open(os.path.join(td, "bad.jsonl"), "w") as f:
    f.write('{"role": "tool", "name": "calc", "content": "error: boom"}\n')
noerr = os.path.join(d, "noerr.json")
with open(noerr, "w") as f:
    json.dump([{"type": "no_errors"}], f)
code, out = run_cli(["bench", td, "-c", noerr])
check("cli bench exit 1", code == 1)
check("cli bench lists both",
      "good.jsonl" in out and "bad.jsonl" in out)
empty = os.path.join(d, "empty")
os.mkdir(empty)
code, _ = run_cli(["bench", empty, "-c", noerr])
check("cli bench empty dir exit 2", code == 2)

# bench history

rec = make_record("checks.json",
                  [("a.jsonl", [CheckResult("x", "pass"),
                                CheckResult("y", "fail")]),
                   ("b.jsonl", [CheckResult("x", "pass")])])
check("history record totals",
      rec["pass"] == 2 and rec["fail"] == 1 and rec["skip"] == 0
      and rec["checks"] == "checks.json"
      and rec["traces"][0]["source"] == "a.jsonl"
      and rec["traces"][0]["status"] == "fail"
      and rec["traces"][1]["status"] == "ok"
      and "T" in rec["ts"])
hpath = os.path.join(d, "hist.jsonl")
record(hpath, rec)
record(hpath, rec)
check("history roundtrip", len(load(hpath)) == 2)
with open(hpath, "a") as f:
    f.write("not json\n")
    f.write('{"nope": true}\n')
check("history skips corrupt lines", len(load(hpath)) == 2)
check("history missing file", load(os.path.join(d, "nope.jsonl")) == [])
check("history default path",
      default_path().endswith(os.path.join(".verdict", "history.jsonl")))
recs = [dict(rec, ts="2026-11-07T10:00:00Z", **{"pass": 2, "fail": 2,
                                                 "skip": 0}),
        dict(rec, ts="2026-11-08T10:00:00Z", **{"pass": 4, "fail": 0,
                                                 "skip": 0})]
out = history_table(recs)
check("history table newest first",
      out.index("2026-11-08") < out.index("2026-11-07")
      and "+50%" in out and "4 passed, 0 failed" in out)
out = history_table(recs, limit=1)
check("history table limit", "2026-11-08" in out
      and "2026-11-07" not in out)

code, out = run_cli(["bench", td, "-c", noerr, "--record",
                     "--history-file", hpath])
check("cli bench --record writes history",
      code == 1 and len(load(hpath)) == 3)
code, out = run_cli(["history", "--history-file", hpath])
check("cli history shows runs", "traces" in out and "passed" in out)
code, out = run_cli(["history", "--history-file", hpath, "--json"])
dd = json.loads(out)
check("cli history --json",
      dd["verdict"] == "history" and len(dd["data"]) == 3)
code, out = run_cli(["history", "--history-file",
                     os.path.join(d, "empty.jsonl")])
check("cli history empty", "no bench runs" in out and code is None)

# judge

s, v, r = parse_judgement(
    '{"score": 0.5, "verdict": "fail", "rationale": "looped"}')
check("judgement ok", (s, v, r) == (0.5, "fail", "looped"))
s, v, r = parse_judgement(
    '```json\n{"score": 1, "verdict": "pass", "rationale": "ok"}\n```')
check("judgement strips fences", (s, v) == (1.0, "pass"))
for name, bad in [("not json", "not json"),
                  ("missing key",
                   '{"score": 0.5, "verdict": "pass"}'),
                  ("score out of range",
                   '{"score": 2, "verdict": "pass", "rationale": "x"}'),
                  ("score not a number",
                   '{"score": "high", "verdict": "pass", "rationale": "x"}'),
                  ("bad verdict",
                   '{"score": 0.5, "verdict": "maybe", "rationale": "x"}'),
                  ("rationale not a string",
                   '{"score": 0.5, "verdict": "pass", "rationale": 3}'),
                  ("not an object", "[1, 2]")]:
    raises("judgement rejects " + name, lambda b=bad: parse_judgement(b),
           JudgeError)
msgs = build_messages(parse_jsonl(RUN_JSONL),
                      "pass when the agent finishes.")
check("judge prompt", msgs[0]["role"] == "system"
      and "pass when the agent finishes." in msgs[1]["content"]
      and "[tool:read_file]" in msgs[1]["content"])


class Handler(BaseHTTPRequestHandler):
    reply = ('{"score": 0.85, "verdict": "pass", '
             '"rationale": "did the thing"}')
    status = 200
    seen_auth = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        Handler.seen_auth = self.headers.get("Authorization")
        body = json.dumps(
            {"choices": [{"message": {"content": Handler.reply}}]}).encode()
        self.send_response(Handler.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % srv.server_port
TRACE = parse_jsonl(RUN_JSONL)

out = judge(TRACE, "rubric", model="fake", base_url=BASE,
            api_key="test-key", timeout=10)
check("judge success", out == {"score": 0.85, "verdict": "pass",
                               "rationale": "did the thing",
                               "model": "fake"})
check("judge sends bearer key", Handler.seen_auth == "Bearer test-key")
Handler.reply = ('{"score": 0.2, "verdict": "fail", '
                 '"rationale": "looped forever"}')
out = judge(TRACE, "rubric", model="fake", base_url=BASE,
            api_key="k", timeout=10)
check("judge fail verdict", out["verdict"] == "fail")
Handler.reply = "i refuse to use json"
raises("judge rejects non-json", lambda: judge(
    TRACE, "rubric", base_url=BASE, api_key="k", timeout=10), JudgeError)
Handler.reply = ('{"score": 0.85, "verdict": "pass", '
                 '"rationale": "did the thing"}')
Handler.status = 500
raises("judge http error", lambda: judge(
    TRACE, "rubric", base_url=BASE, api_key="k", timeout=10), JudgeError)
Handler.status = 200
old_key = os.environ.pop("OPENAI_API_KEY", None)
raises("judge needs api key",
       lambda: judge(TRACE, "rubric", base_url=BASE, api_key=None),
       JudgeError)
if old_key is not None:
    os.environ["OPENAI_API_KEY"] = old_key
raises("judge unreachable",
       lambda: judge(TRACE, "rubric",
                     base_url="http://127.0.0.1:1", api_key="k", timeout=2),
       JudgeError)

code, out = run_cli(["judge", RUN_JSONL, "-r",
                     os.path.join(EX, "rubric.md"),
                     "--base-url", BASE, "--api-key", "k",
                     "--model", "fake"])
check("cli judge exit 0", code == 0 and "0.85" in out)
Handler.reply = ('{"score": 0.1, "verdict": "fail", "rationale": "nope"}')
code, out = run_cli(["judge", RUN_JSONL, "-r",
                     os.path.join(EX, "rubric.md"),
                     "--base-url", BASE, "--api-key", "k",
                     "--model", "fake", "--json"])
dd = json.loads(out)
check("cli judge exit 1 + json",
      code == 1 and dd["data"]["verdict"] == "fail")
srv.shutdown()

# trace diff

RUN2_JSONL = os.path.join(EX, "run2.jsonl")


def _mk_trace(turns, cost=None, duration_ms=None):
    return Trace("t", turns, cost=cost, duration_ms=duration_ms)


def _rows(d):
    return {r["metric"]: r for r in d["rows"]}


base = _mk_trace([
    Turn("user", "do the thing"),
    Turn("assistant", "looking"),
    Turn("tool", "ok", name="read_file"),
    Turn("tool", "ok", name="edit_file"),
    Turn("assistant", "line one\nline two\nline three")],
    cost=0.02, duration_ms=1200)
same = _mk_trace([
    Turn("user", "do the thing"),
    Turn("assistant", "looking"),
    Turn("tool", "ok", name="read_file"),
    Turn("tool", "ok", name="edit_file"),
    Turn("assistant", "line one\nline two\nline three")],
    cost=0.02, duration_ms=1200)
d = diff_traces(base, same)
check("diff identical traces are same",
      d["same"] and all(r["same"] for r in d["rows"]))

fewer = _mk_trace([
    Turn("user", "do the thing"),
    Turn("tool", "ok", name="read_file"),
    Turn("assistant", "line one\nline two\nline three")],
    cost=0.01, duration_ms=800)
d = diff_traces(base, fewer)
r = _rows(d)
check("diff counts tool calls and turns",
      not d["same"] and r["turns"]["a"] == 5 and r["turns"]["b"] == 3
      and r["tool calls"]["a"] == 2 and r["tool calls"]["b"] == 1)
check("diff tool detail names the split",
      "shared prefix: read_file" in r["tools"]["detail"]
      and "a continues with edit_file" in r["tools"]["detail"])
check("diff cost detail signs the gap",
      r["cost"]["a"] == "$0.0200" and r["cost"]["b"] == "$0.0100"
      and "more" in r["cost"]["detail"])
check("diff duration detail signs the gap",
      r["duration"]["a"] == 1200 and "slower" in r["duration"]["detail"])

nocost = _mk_trace(base.turns)
d = diff_traces(base, nocost)
r = _rows(d)
check("diff missing cost is unknown not different",
      r["cost"]["same"] is None and "no cost data in b" in r["cost"]["detail"]
      and d["same"])
d = diff_traces(nocost, nocost)
check("diff both missing duration is unknown",
      _rows(d)["duration"]["same"] is None)

other_answer = _mk_trace(base.turns[:-1] + [
    Turn("assistant", "line one\nline TWO\nline three")],
    cost=0.02, duration_ms=1200)
d = diff_traces(base, other_answer)
r = _rows(d)
check("diff final answer finds the first differing line",
      not r["final answer"]["same"]
      and "line 2" in r["final answer"]["detail"]
      and "line two" in r["final answer"]["detail"]
      and "line TWO" in r["final answer"]["detail"])

no_answer = _mk_trace(base.turns[:-1], cost=0.02, duration_ms=1200)
d = diff_traces(base, no_answer)
check("diff missing final answer is a difference",
      not _rows(d)["final answer"]["same"])

table = diff_table(diff_traces(base, fewer))
check("diff table marks differing rows and counts them",
      "<>" in table and "5 differences" in table)
check("diff table shows cost as money and duration in ms",
      "$0.0200" in table and "1200ms" in table)
check("diff table marks unknown rows with ?",
      "?" in diff_table(diff_traces(base, nocost)))
table = diff_table(diff_traces(base, same))
check("diff table on identical traces says 0 differences",
      "0 differences" in table and "<>" not in table)

code, out = run_cli(["diff", RUN_JSONL, RUN_JSONL])
check("cli diff same trace exit 0", code == 0 and "0 differences" in out)
code, out = run_cli(["diff", RUN_JSONL, RUN2_JSONL])
check("cli diff different traces exit 1",
      code == 1 and "final answer: differs" in out
      and "read_file -> edit_file" in out)
code, out = run_cli(["diff", RUN_JSONL, RUN2_JSONL, "--json"])
dd = json.loads(out)
check("cli diff --json shape",
      code == 1 and dd["verdict"] == "diff" and dd["data"]["same"] is False
      and len(dd["data"]["rows"]) == 6)
code, out = run_cli(["diff", RUN_JSONL, "/tmp/no-such-trace.jsonl"])
check("cli diff missing file exit 2", code == 2)

print()
print("%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
