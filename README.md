verdict - score whole agent runs, not just replies [ai eval tool]

disclaimer:
this is a scratch eval tool i wrote for my own agent work, not a serious
benchmarking framework. it scores one run at a time against checks i wrote
myself. if you want real evals, use inspect or something maintained.

why i made this:
probe scores single replies, which is fine for prompts, but my agents fail
in ways a single reply never shows: they loop, they call the wrong tools,
they ignore errors and then write a confident final answer anyway. i kept
re-reading mule logs by hand to check runs, so i wrote the checks down
instead. verdict reads a trace (a mule --log-file, a council transcript,
or plain jsonl) and runs scripted checks over it: step budgets, required
and forbidden tools, milestone ordering, final answer contains/regex, no
tool errors, cost and duration ceilings. checks the trace has no data for
come back as skip, not fail, because a council transcript has no cost data
and pretending otherwise would be lying. there is also a judge mode that
sends the run plus a rubric to any openai-compatible chat api and parses
back a score. i avoided requests and click and all of that, stdlib only,
because every one of my agent tools already works that way and i like that
they install with nothing.

the guide:
python3 -m verdict works from the repo root, or pip install . for the
verdict command. needs python 3.9+.

verdict init
  writes checks.json and rubric.md in the current dir so you have
  something to edit. will not overwrite unless you pass --force.

verdict score run.jsonl -c checks.json
  runs the scripted checks on one trace. prints a table, exits 0 when
  everything passes or skips, 1 on any fail, 2 on broken input.
  --format jsonl|mule|council overrides the auto-detect. --json prints
  machine-readable output instead of the table.

verdict judge run.jsonl -r rubric.md --model gpt-4o-mini
  scores one trace with a judge model. reads the key from OPENAI_API_KEY
  or --api-key, base url from OPENAI_BASE_URL or --base-url. exits 0 on
  pass, 1 on fail, 2 when the judge call itself breaks.

verdict bench ./traces -c checks.json
  scores every trace file in a directory, one row each. good for a folder
  of recorded runs.

trace formats:
  jsonl: one json object per line, {"role": "user"|"assistant"|"tool",
  "content": ..., "name": ...} (verdict's own {"kind": ...} works too).
  mule: a --log-file tee of the terminal transcript. tool args are the
  one-line summaries and results are truncated, so checks see what you
  saw, not the full api payloads. final answer detection is a last-block
  heuristic, it is documented as one.
  council: the markdown from council's transcript export. carries run
  totals only, so max_cost and other data checks skip honestly.

checks.json is a list of {"type": ..., "name": optional, ...params}:
  max_steps {"max": n}, required_tools {"tools": [...]},
  forbidden_tools {"tools": [...]}, milestones {"tools": [...]} (must
  appear in order), final_contains {"text": ...},
  final_regex {"pattern": ...}, no_errors {}, max_cost {"max": usd},
  max_duration {"max_ms": n}.

tests: python3 tests/test_verdict.py, 93 checks, no deps. judge tests run
against a fake local openai-compatible server, no key needed.
