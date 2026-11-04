"""turns and traces: the thing verdict scores.

a trace is one agent run as a flat list of turns, plus whatever run-level
numbers the source format actually carries (cost, duration). parsers for
mule --log-file output, council markdown transcripts, and plain jsonl.
every parser records its caveats in trace.notes so nothing is silently
guessed.
"""

import json
import re


class Turn:
    def __init__(self, kind, content, name=None):
        # kind is user, assistant, or tool. name is the tool name on tool turns.
        if kind not in ("user", "assistant", "tool"):
            raise ValueError("bad turn kind: %r" % kind)
        self.kind = kind
        self.content = content or ""
        self.name = name

    def to_dict(self):
        d = {"kind": self.kind, "content": self.content}
        if self.name:
            d["name"] = self.name
        return d

    def __repr__(self):
        return "Turn(%r, %r, name=%r)" % (self.kind, self.content[:30], self.name)


class Trace:
    def __init__(self, source, turns, cost=None, duration_ms=None):
        self.source = source
        self.turns = turns
        self.cost = cost  # float usd, or None when the format has no cost data
        self.duration_ms = duration_ms  # int, or None
        self.notes = []  # parser caveats, shown with --json and in reports

    def tool_turns(self):
        return [t for t in self.turns if t.kind == "tool"]

    def tool_names(self):
        # "result" is verdict's own pseudo-turn for nested mule output,
        # not a real tool call.
        return [t.name for t in self.tool_turns()
                if t.name and t.name != "result"]

    def call_turns(self):
        # tool turns that are actual calls, not nested result records
        return [t for t in self.tool_turns()
                if not t.name or t.name != "result"]

    def final_answer(self):
        for t in reversed(self.turns):
            if t.kind == "assistant":
                return t.content
        return None

    def compact(self, max_chars=4000):
        # the trace squashed into one text block for the judge prompt.
        # long tool outputs get cut, the shape of the run stays.
        parts = []
        for t in self.turns:
            if t.kind == "user":
                tag = "[user]"
            elif t.kind == "assistant":
                tag = "[assistant]"
            else:
                tag = "[tool:%s]" % (t.name or "?")
            body = t.content.strip().replace("\n", " ")
            if len(body) > max_chars:
                body = body[:max_chars] + " ...[truncated]"
            parts.append("%s %s" % (tag, body))
        return "\n".join(parts)


# jsonl: one json object per line. accepts openai-ish messages
# {"role": "user"|"assistant"|"tool", "content": ..., "name": ...}
# and verdict's own {"kind": ..., "content": ..., "name": ...}.
def parse_jsonl(path):
    turns = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError("%s line %d: not json (%s)" % (path, i, e))
            kind = obj.get("kind") or obj.get("role")
            if kind == "system":
                continue  # system prompts are setup, not the run
            if kind not in ("user", "assistant", "tool"):
                raise ValueError("%s line %d: bad kind/role %r" % (path, i, kind))
            turns.append(Turn(kind, obj.get("content", ""), name=obj.get("name")))
    trace = Trace(path, turns)
    if not turns:
        trace.notes.append("empty trace: no turns parsed")
    return trace


# council: markdown from council's to_markdown(). sections are
# "### speaker (kind)" or "### speaker", then "## final answer",
# then "## stats" with "- turns:", "- seconds:", "- tokens: A in / B out".
def parse_council_md(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    turns = []
    duration_ms = None
    tokens_in = tokens_out = None

    sect_re = re.compile(r"^###\s+(.+?)\s*$", re.M)
    final_re = re.compile(r"^##\s+final answer\s*$", re.M | re.I)
    stats_re = re.compile(r"^##\s+stats\s*$", re.M | re.I)

    final_m = final_re.search(text)
    stats_m = stats_re.search(text)
    body = text[:final_m.start()] if final_m else text

    for m in sect_re.finditer(body):
        header = m.group(1).strip()
        start = m.end()
        nxt = sect_re.search(body, start)
        chunk = body[start:nxt.start() if nxt else len(body)].strip()
        if "(tool:" in header:
            # "### name (tool: calc)"
            name = header.split("(tool:", 1)[1].rstrip(")").strip()
            speaker = header.split("(", 1)[0].strip()
            turns.append(Turn("tool", chunk, name=name or speaker))
        elif header.lower().startswith("judge"):
            turns.append(Turn("assistant", chunk, name="judge"))
        else:
            # plain "### alice" is a participant speaking
            turns.append(Turn("assistant", chunk, name=header))

    if final_m:
        end = stats_m.start() if stats_m else len(text)
        final = text[final_m.end():end].strip()
        if final:
            turns.append(Turn("assistant", final, name="final"))

    if stats_m:
        stats = text[stats_m.end():]
        sec = re.search(r"-\s*seconds:\s*([\d.]+)", stats)
        if sec:
            duration_ms = int(float(sec.group(1)) * 1000)
        tok = re.search(r"-\s*tokens:\s*([\d,]+)\s*in\s*/\s*([\d,]+)\s*out", stats)
        if tok:
            tokens_in = int(tok.group(1).replace(",", ""))
            tokens_out = int(tok.group(2).replace(",", ""))

    trace = Trace(path, turns, duration_ms=duration_ms)
    trace.notes.append("council transcripts carry no per-turn tokens or cost, "
                       "only run totals; max_cost checks are skipped")
    if tokens_in is not None:
        trace.notes.append("tokens: %d in / %d out (run totals)" % (tokens_in, tokens_out))
    return trace


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_CALL_UNI = "\u23fa"  # mule's tool-call glyph
_RESULT_UNI = "\u23bf"  # mule's tool-result glyph
_STEP_RE = re.compile(
    r"^\s*step\s+(\d+)\s*[·|]\s*([\d,]+)\s*in\s*/\s*([\d,]+)\s*out\s*[·|]\s*\$([\d.]+)")
_BANNER_RES = [
    re.compile(r"mule\s+[\d.]+"),  # run banner line
    re.compile(r"^\s*(╭|╰|│|─|\+|\||=){3,}"),  # panel/box art
    re.compile(r"^\s*(tokens|cost|steps|model|root)\s*[:=]", re.I),  # status footer
    re.compile(r"^\s*⠋|^\s*⠙|^\s*⠹"),  # spinner frames
]


def _strip_ansi(s):
    return _ANSI.sub("", s)


# mule: --log-file is a tee of the terminal transcript. tool calls are
# lines starting with the call glyph (unicode) or "* name" (ascii mode),
# results nest under the result glyph, "step N . A in / B out . $C" lines
# carry tokens and cost. the final answer is the last text block after
# the last tool call; banners and footers are trimmed by prefix.
def parse_mule_log(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    lines = [_strip_ansi(l).rstrip() for l in raw.splitlines()]

    turns = []
    cost = 0.0
    saw_cost = False
    pending_text = []

    def flush_text():
        body = "\n".join(pending_text).strip()
        pending_text.clear()
        if body:
            turns.append(Turn("assistant", body))

    for line in lines:
        s = line.strip()
        if not s:
            continue
        step = _STEP_RE.match(s)
        if step:
            flush_text()
            saw_cost = True
            cost += float(step.group(4))
            continue
        if s.startswith(_CALL_UNI):
            flush_text()
            rest = s[len(_CALL_UNI):].strip()
            name = rest.split()[0] if rest else "?"
            summary = rest[len(name):].strip() if rest else ""
            turns.append(Turn("tool", summary, name=name))
            continue
        if s.startswith(_RESULT_UNI):
            body = s[len(_RESULT_UNI):].strip()
            turns.append(Turn("tool", body, name="result"))
            continue
        if re.match(r"^\*\s+[a-zA-Z_]\w*(\s|$)", s):
            # ascii fallback: "* name args". ambiguous with bullets,
            # so only bare identifiers count, documented as a heuristic.
            flush_text()
            rest = s[1:].strip()
            name = rest.split()[0]
            turns.append(Turn("tool", rest[len(name):].strip(), name=name))
            continue
        if re.match(r"^\|\s", s) and turns and turns[-1].kind == "tool":
            turns.append(Turn("tool", s[1:].strip(), name="result"))
            continue
        if any(p.search(s) for p in _BANNER_RES):
            continue
        pending_text.append(line.rstrip())

    flush_text()
    # merge consecutive result lines into the preceding call's record
    merged = []
    for t in turns:
        if (t.kind == "tool" and t.name == "result" and merged
                and merged[-1].kind == "tool" and merged[-1].name != "result"):
            merged[-1].content += ("\n" + t.content).rstrip()
        else:
            merged.append(t)

    trace = Trace(path, merged, cost=cost if saw_cost else None)
    trace.notes.append("mule logs are terminal output: tool args are summaries, "
                       "full results are truncated to one line, final answer "
                       "detection is a last-block heuristic")
    return trace


def load_trace(path, fmt="auto"):
    # fmt: auto, jsonl, mule, council
    if fmt == "auto":
        low = path.lower()
        if low.endswith(".jsonl"):
            fmt = "jsonl"
        elif low.endswith(".md"):
            fmt = "council"
        else:
            fmt = "mule"
    if fmt == "jsonl":
        return parse_jsonl(path)
    if fmt == "mule":
        return parse_mule_log(path)
    if fmt == "council":
        return parse_council_md(path)
    raise ValueError("unknown format %r (jsonl, mule, council)" % fmt)
