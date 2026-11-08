"""cli: init, score, judge, bench."""

import argparse
import glob
import json
import os
import sys

from . import __version__
from .trace import load_trace
from .checks import load_checks, run_checks, summarize
from .judge import judge, JudgeError
from .report import score_table, judge_text, bench_table, history_table, as_json
from .history import default_path, make_record, record, load as load_history


CHECKS_TEMPLATE = """[
  {"name": "stays in budget", "type": "max_steps", "max": 20},
  {"name": "reads before editing", "type": "milestones",
   "tools": ["read_file", "edit_file"]},
  {"name": "no forbidden tools", "type": "forbidden_tools",
   "tools": ["run_shell"]},
  {"name": "no tool errors", "type": "no_errors"},
  {"name": "cheap run", "type": "max_cost", "max": 0.10}
]
"""

RUBRIC_TEMPLATE = """# rubric

score the run 0.0 to 1.0. pass at 0.7 or above.

what matters:
- the agent made progress on the task instead of looping
- tool calls had a reason; no shotgun debugging
- the final answer actually answers the task
- no ignored errors or silent failures

be strict about loops and ignored errors. be generous about style.
"""


def _read_rubric(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def cmd_init(args):
    for path, content in (("checks.json", CHECKS_TEMPLATE),
                          ("rubric.md", RUBRIC_TEMPLATE)):
        if os.path.exists(path) and not args.force:
            print("%s exists, use --force to overwrite" % path)
            continue
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print("wrote %s" % path)


def _load_trace_or_die(path, fmt):
    try:
        return load_trace(path, fmt)
    except (OSError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        sys.exit(2)


def cmd_score(args):
    trace = _load_trace_or_die(args.trace, args.format)
    try:
        specs = load_checks(args.checks)
    except (OSError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        sys.exit(2)
    results = run_checks(specs, trace)
    if args.json:
        print(as_json("score", {
            "source": trace.source,
            "notes": trace.notes,
            "results": [r.to_dict() for r in results],
            "summary": summarize(results)}))
    else:
        print(score_table(trace.source, results, trace.notes))
    sys.exit(1 if summarize(results)["fail"] else 0)


def _model_flags(p):
    p.add_argument("--model", default="gpt-4o-mini")
    p.add_argument("--base-url", default=None)
    p.add_argument("--api-key", default=None)
    p.add_argument("--timeout", type=int, default=60)


def cmd_judge(args):
    trace = _load_trace_or_die(args.trace, args.format)
    try:
        rubric = _read_rubric(args.rubric)
    except OSError as e:
        print("error: %s" % e, file=sys.stderr)
        sys.exit(2)
    try:
        out = judge(trace, rubric, model=args.model, base_url=args.base_url,
                    api_key=args.api_key, timeout=args.timeout)
    except JudgeError as e:
        print("error: %s" % e, file=sys.stderr)
        sys.exit(2)
    if args.json:
        print(as_json("judge", {"source": trace.source, **out}))
    else:
        print(judge_text(trace.source, out))
    sys.exit(1 if out["verdict"] == "fail" else 0)


def cmd_bench(args):
    paths = sorted(glob.glob(os.path.join(args.dir, "*")))
    paths = [p for p in paths if os.path.isfile(p)]
    if not paths:
        print("error: no files in %s" % args.dir, file=sys.stderr)
        sys.exit(2)
    try:
        specs = load_checks(args.checks)
    except (OSError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        sys.exit(2)
    rows = []
    failed_any = False
    for p in paths:
        try:
            trace = load_trace(p, args.format)
        except (OSError, ValueError) as e:
            print("error: %s: %s" % (p, e), file=sys.stderr)
            sys.exit(2)
        results = run_checks(specs, trace)
        if summarize(results)["fail"]:
            failed_any = True
        rows.append((p, results))
    if args.json:
        print(as_json("bench", [
            {"source": s, "results": [r.to_dict() for r in res],
             "summary": summarize(res)} for s, res in rows]))
    else:
        print(bench_table(rows))
    if args.record:
        path = args.history_file or default_path()
        record(path, make_record(args.checks, rows))
        print("recorded bench run to %s" % path, file=sys.stderr)
    sys.exit(1 if failed_any else 0)


def cmd_history(args):
    path = args.history_file or default_path()
    records = load_history(path)
    if not records:
        print("no bench runs recorded yet (use bench --record)")
        return
    if args.json:
        print(as_json("history", records[-args.limit:]))
    else:
        print(history_table(records, args.limit))


def build_parser():
    p = argparse.ArgumentParser(
        prog="verdict",
        description="score whole agent runs, not just replies")
    p.add_argument("--version", action="version", version="verdict " + __version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="scaffold checks.json and rubric.md")
    pi.add_argument("--force", action="store_true")
    pi.set_defaults(func=cmd_init)

    ps = sub.add_parser("score", help="run scripted checks on one trace")
    ps.add_argument("trace")
    ps.add_argument("-c", "--checks", default="checks.json")
    ps.add_argument("--format", default="auto",
                    choices=["auto", "jsonl", "mule", "council"])
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(func=cmd_score)

    pj = sub.add_parser("judge", help="score one trace with a judge model")
    pj.add_argument("trace")
    pj.add_argument("-r", "--rubric", default="rubric.md")
    pj.add_argument("--format", default="auto",
                    choices=["auto", "jsonl", "mule", "council"])
    pj.add_argument("--json", action="store_true")
    _model_flags(pj)
    pj.set_defaults(func=cmd_judge)

    pb = sub.add_parser("bench", help="score every trace in a directory")
    pb.add_argument("dir")
    pb.add_argument("-c", "--checks", default="checks.json")
    pb.add_argument("--format", default="auto",
                    choices=["auto", "jsonl", "mule", "council"])
    pb.add_argument("--json", action="store_true")
    pb.add_argument("--record", action="store_true",
                    help="save this run to the bench history")
    pb.add_argument("--history-file", default=None,
                    help="history file (default ~/.verdict/history.jsonl)")
    pb.set_defaults(func=cmd_bench)

    ph = sub.add_parser("history", help="show past bench runs")
    ph.add_argument("--limit", type=int, default=10)
    ph.add_argument("--json", action="store_true")
    ph.add_argument("--history-file", default=None,
                    help="history file (default ~/.verdict/history.jsonl)")
    ph.set_defaults(func=cmd_history)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)
