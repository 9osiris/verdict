"""judge mode: score a run with a model instead of scripted checks.

sends the rubric plus a compacted trace to any openai-compatible chat
completions api and parses back {"score", "verdict", "rationale"}.
stdlib http only, the api key is never printed or logged.
"""

import json
import os
import urllib.request
import urllib.error


class JudgeError(Exception):
    pass


JUDGE_SYSTEM = (
    "you score agent runs against a rubric. reply with exactly one json "
    "object and nothing else: {\"score\": <0.0-1.0>, "
    "\"verdict\": \"pass\" or \"fail\", \"rationale\": \"<one or two "
    "sentences>\"}. no markdown fences, no commentary."
)


def build_messages(trace, rubric):
    user = ("rubric:\n%s\n\n---\ntrace of one agent run "
            "(%d turns, source: %s):\n%s\n\n---\nscore it." %
            (rubric.strip(), len(trace.turns), trace.source,
             trace.compact()))
    return [{"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": user}]


def _strip_fences(text):
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[: -3]
    return t.strip()


def parse_judgement(text):
    # the model's reply -> (score, verdict, rationale). raises JudgeError.
    try:
        obj = json.loads(_strip_fences(text))
    except json.JSONDecodeError as e:
        raise JudgeError("judge did not return json: %s" % e)
    if not isinstance(obj, dict):
        raise JudgeError("judge returned %s, not an object" % type(obj).__name__)
    for key in ("score", "verdict", "rationale"):
        if key not in obj:
            raise JudgeError("judge json missing %r" % key)
    score = obj["score"]
    if not isinstance(score, (int, float)) or not 0.0 <= score <= 1.0:
        raise JudgeError("judge score %r not a number in 0..1" % score)
    if obj["verdict"] not in ("pass", "fail"):
        raise JudgeError("judge verdict %r not pass/fail" % obj["verdict"])
    if not isinstance(obj["rationale"], str):
        raise JudgeError("judge rationale is not a string")
    return float(score), obj["verdict"], obj["rationale"]


def judge(trace, rubric, model="gpt-4o-mini", base_url=None,
          api_key=None, timeout=60):
    base_url = (base_url or os.environ.get("OPENAI_BASE_URL")
                or "https://api.openai.com/v1").rstrip("/")
    api_key = api_key or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise JudgeError("no api key: pass --api-key or set OPENAI_API_KEY")
    body = json.dumps({
        "model": model,
        "messages": build_messages(trace, rubric),
        "temperature": 0,
        "max_tokens": 500,
    }).encode()
    req = urllib.request.Request(
        base_url + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise JudgeError("api http %s: %s" % (e.code, e.read()[:200].decode(
            "utf-8", "replace")))
    except urllib.error.URLError as e:
        raise JudgeError("api unreachable: %s" % e.reason)
    except TimeoutError:
        raise JudgeError("api timed out after %ss" % timeout)
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise JudgeError("unexpected api response shape")
    score, verdict, rationale = parse_judgement(text)
    return {"score": score, "verdict": verdict, "rationale": rationale,
            "model": model}
