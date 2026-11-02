r"""Turning a finished turn into an observation.

The event log records that a turn happened and what it cost. This records what
it *did*: which files it read, which it changed, what it ran, whether the tests
passed, and how it described its own work. That is the difference between
"eleven turns, 2.1M tokens" and "the fan divisor was wrong in msi_ec.py".

**It costs nothing.** Everything here is deterministic parsing of the transcript
Claude Code already writes. No model is called, no tokens are spent, and no
network is touched -- which is what makes it safe to run from a hook on every
turn. An opt-in summarisation tier can enrich these rows later; it writes to the
same table with ``source='llm'`` so retrieval never has to care which produced a
row.

The read is incremental, in the same style as ``events.tokens_since``: a byte
offset per session, in ``obs_offsets.json``, so a turn in a long session costs
no more than a turn in a short one. The offset is kept separate from the one
``events.py`` uses, deliberately -- the two passes then cannot break each other,
and the token-counting path that already works is left alone.

Subagent turns (``isSidechain``) are skipped by default. A subagent's tool calls
are means, not outcome, and folding them in buries the turn's actual result
under a dozen searches someone else performed.
"""

from __future__ import annotations

import json
import os
import re

import events

OFFSETS_PATH = os.path.join(events.ROOT, "obs_offsets.json")

# Tools grouped by what they tell us about a turn. Anything not listed still
# counts toward the tool list; it just does not imply a file or a command.
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "Grep", "Glob"}
SHELL_TOOLS = {"Bash", "PowerShell"}

# How long a stored field may get. Generous enough to stay useful, short enough
# that a thousand observations are still a small database.
MAX_NARRATIVE = 1200
MAX_TITLE = 160
MAX_COMMAND = 300
MAX_ITEMS = 60

# Commands that look like a test run. Matched against the start of the command
# after stripping a leading `cd ... &&`, so the usual prefixes still hit.
TEST_PATTERNS = (
    r"pytest\b", r"python\s+-m\s+pytest\b", r"python\s+-m\s+unittest\b",
    r"npm\s+(run\s+)?test\b", r"yarn\s+test\b", r"pnpm\s+(run\s+)?test\b",
    r"jest\b", r"vitest\b", r"mocha\b", r"go\s+test\b", r"cargo\s+test\b",
    r"mvn\s+(\S+\s+)?test\b", r"gradle\s+test\b", r"dotnet\s+test\b",
    r"rspec\b", r"phpunit\b", r"tox\b", r"ctest\b", r"bun\s+test\b",
)
_TEST_RE = re.compile("|".join(TEST_PATTERNS), re.I)

# Outcome lines the common runners print. First match wins, so the more
# specific ones come first.
_RESULT_RES = (
    # pytest: "===== 5 passed, 1 failed in 2.31s ====="
    re.compile(r"(\d+)\s+passed(?:,\s*(\d+)\s+failed)?", re.I),
    # jest / vitest: "Tests: 1 failed, 4 passed, 5 total"
    re.compile(r"(\d+)\s+failed,\s*(\d+)\s+passed", re.I),
    # cargo: "test result: ok. 5 passed; 0 failed"
    re.compile(r"test result:\s*(\w+)\.\s*(\d+)\s+passed;\s*(\d+)\s+failed", re.I),
    # unittest: "OK" / "FAILED (failures=1)"
    re.compile(r"^(OK|FAILED)\b", re.I | re.M),
    # go: "ok   pkg  0.1s" / "FAIL  pkg"
    re.compile(r"^(ok|FAIL)\s+\S+", re.I | re.M),
)

_FAIL_WORDS = re.compile(
    r"\b(fail|failed|failures|error|errors|panic|assertionerror)\b", re.I
)


def _read_offsets() -> dict:
    try:
        with open(OFFSETS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_offsets(data: dict) -> None:
    try:
        os.makedirs(events.ROOT, exist_ok=True)
        tmp = OFFSETS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
        os.replace(tmp, OFFSETS_PATH)
    except OSError:
        pass


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _strip_prefix(cmd: str) -> str:
    """Drop a leading ``cd ... &&`` so a test command is still recognisable."""
    return re.sub(r"^\s*cd\s+[^&;|]+(&&|;)\s*", "", cmd or "").strip()


def classify(prompt: str, modified: list, read: list, tests: str) -> str:
    """Pick one of ``store.KINDS`` from the shape of the turn.

    A heuristic, and labelled as one. It reads the human's own framing first --
    they said why they asked -- and falls back to what the turn touched. Wrong
    occasionally; useful as a filter regardless, and cheaper than asking a model
    to classify every turn.
    """
    p = (prompt or "").lower().strip()

    def has(*words: str) -> bool:
        return any(re.search(r"\b" + w, p) for w in words)

    # A choice being put to someone is a decision even though "should" also
    # opens a question, so this is tested before the interrogative rule.
    if has("decide", "should we", "should i", "choose", "pick between",
           "trade-off", "tradeoff", "which approach", "or should"):
        return "decision"

    # A question is an enquiry first, whatever else it contains. Without this,
    # "why is the GPU temperature wrong" classifies as a bugfix on the strength
    # of one word, when nothing is being fixed.
    if p.endswith("?") or re.match(
        r"^(why|what|what's|how|when|where|which|who|is|are|was|were|does|do|"
        r"did|can|could|would|will|should|has|have|any)\b", p
    ):
        return "discovery"

    if has("fix", "bug", "broken", "fails", "failing", "error", "crash", "wrong",
           "regress", "repair"):
        return "bugfix"
    if has("refactor", "clean", "rename", "simplif", "tidy", "reorganis",
           "reorganiz", "extract", "dedup"):
        return "refactor"
    if has("add", "implement", "create", "build", "support", "introduc",
           "write a", "new "):
        return "feature"
    if has("why", "what", "how", "explain", "investigat", "understand",
           "analyse", "analyz", "review", "check", "find", "look at"):
        return "discovery"
    if not modified and read:
        return "discovery"
    return "change"


def _tests_from(command: str, output: str) -> str:
    """A one-line verdict for a test command, or "" when it is not one.

    The output is the tool result as the agent saw it, so this is reading the
    runner's own summary rather than re-running anything.
    """
    if not _TEST_RE.search(_strip_prefix(command)):
        return ""
    text = output or ""
    tail = text[-4000:]

    m = _RESULT_RES[2].search(tail)          # cargo
    if m:
        return "cargo: %s (%s passed, %s failed)" % (m.group(1), m.group(2), m.group(3))

    m = _RESULT_RES[1].search(tail)          # jest / vitest, failed-first
    if m:
        return "%s failed, %s passed" % (m.group(1), m.group(2))

    m = _RESULT_RES[0].search(tail)          # pytest-style, passed-first
    if m:
        failed = m.group(2)
        return "%s passed%s" % (m.group(1), (", %s failed" % failed) if failed else "")

    m = _RESULT_RES[3].search(tail) or _RESULT_RES[4].search(tail)
    if m:
        return m.group(1).lower()

    # A test ran but printed nothing we recognise. Say so rather than claim a
    # pass -- a silent "" here would read as "no tests", which is a different
    # and more dangerous statement.
    return "ran, outcome unparsed" + (" (output mentions failure)"
                                      if _FAIL_WORDS.search(tail) else "")


def _result_text(block: dict) -> str:
    """Flatten a tool_result block's content into text."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    out = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                out.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                out.append(part)
    return "\n".join(out)


def extract(
    transcript_path: str,
    sid: str,
    main_only: bool = True,
    advance: bool = True,
) -> dict:
    """Parse the transcript since this session's last read into one observation.

    Returns a dict of fields ready for ``store.observations``, plus ``lines``
    and ``offset``. Returns an empty dict when there is nothing new or the
    transcript cannot be read -- callers are hooks, which must never fail.
    """
    blank: dict = {}
    if not sid or not transcript_path or not os.path.exists(transcript_path):
        return blank

    offsets = _read_offsets()
    start = offsets.get(sid, 0)
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        return blank
    if not isinstance(start, int) or start < 0 or start > size:
        start = 0

    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(start)
            data = fh.read()
    except OSError:
        return blank

    nl = data.rfind(b"\n")
    if nl == -1:
        return blank
    consumed = start + nl + 1
    text = data[: nl + 1].decode("utf-8", "replace")

    files_read: list[str] = []
    files_mod: list[str] = []
    commands: list[str] = []
    tools: list[str] = []
    narrative_parts: list[str] = []
    test_notes: list[str] = []
    branch = ""
    cwd = ""
    lines = 0
    # tool_use id -> command, so a later tool_result can be paired with the
    # command that produced it.
    pending: dict[str, str] = {}

    def add(seq: list, value: str) -> None:
        value = (value or "").strip()
        if value and value not in seq and len(seq) < MAX_ITEMS:
            seq.append(value)

    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        lines += 1
        if main_only and obj.get("isSidechain"):
            continue
        branch = obj.get("gitBranch") or branch
        cwd = obj.get("cwd") or cwd

        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")

            if role == "assistant" and btype == "text":
                narrative_parts.append(str(block.get("text") or ""))

            elif role == "assistant" and btype == "tool_use":
                name = str(block.get("name") or "")
                add(tools, name)
                inp = block.get("input")
                if not isinstance(inp, dict):
                    continue
                if name in WRITE_TOOLS:
                    add(files_mod, str(inp.get("file_path") or inp.get("notebook_path") or ""))
                elif name in READ_TOOLS:
                    add(files_read, str(inp.get("file_path") or inp.get("path") or ""))
                elif name in SHELL_TOOLS:
                    cmd = _clip(inp.get("command") or "", MAX_COMMAND)
                    add(commands, cmd)
                    tid = block.get("id")
                    if tid and _TEST_RE.search(_strip_prefix(cmd)):
                        pending[str(tid)] = cmd

            elif btype == "tool_result":
                tid = str(block.get("tool_use_id") or "")
                if tid in pending:
                    verdict = _tests_from(pending.pop(tid), _result_text(block))
                    if verdict:
                        add(test_notes, verdict)

    if advance:
        offsets[sid] = consumed
        _write_offsets(offsets)

    if not lines:
        return blank

    # The last thing the agent said is its own account of the turn, which beats
    # anything this module could synthesise from tool calls.
    narrative = _clip(narrative_parts[-1] if narrative_parts else "", MAX_NARRATIVE)

    facts = ", ".join(filter(None, [
        "%d file(s) changed" % len(files_mod) if files_mod else "",
        "%d read" % len(files_read) if files_read else "",
        "%d command(s)" % len(commands) if commands else "",
        "tests: " + "; ".join(test_notes) if test_notes else "",
        "branch " + branch if branch else "",
    ]))

    return {
        "files_read": files_read,
        "files_modified": files_mod,
        "commands": commands,
        "tools": tools,
        "narrative": narrative,
        "facts": facts,
        "tests": "; ".join(test_notes),
        "branch": branch,
        "cwd": cwd,
        "lines": lines,
        "offset": consumed,
    }


def record(
    sid: str,
    transcript_path: str,
    cwd: str,
    proj: str,
    prompt: str = "",
    started_at: float | None = None,
    ended_at: float | None = None,
    tok: dict | None = None,
    model: str = "",
    conn=None,
) -> int:
    """Extract a turn and store it as an observation. Returns its row id, or 0.

    Swallows everything: this runs from a hook, and a hook that fails loudly
    would disrupt the agent it reports on.
    """
    try:
        import time

        import store

        fields = extract(transcript_path, sid)
        if not fields:
            return 0
        # A turn with no visible work and nothing said is not worth a row.
        if not any((fields["files_modified"], fields["files_read"],
                    fields["commands"], fields["narrative"])):
            return 0

        now = time.time()
        tok = tok or {}
        own = conn is None
        conn = conn or store.connect()
        try:
            # The prompt is logged when the turn starts and is gone from the
            # payload by the time it ends, so it is read back from the store.
            # Draining the log here is what keeps the store current without a
            # scheduled task: the read is incremental from a watermark, so it
            # costs the few lines this turn added.
            if not prompt:
                try:
                    store.ingest(conn)
                    row = conn.execute(
                        "select msg from events where sid=? and ev='prompt' "
                        "and msg<>'' order by t desc limit 1", (sid,)
                    ).fetchone()
                    if row:
                        prompt = row["msg"]
                    if started_at is None:
                        begun = conn.execute(
                            "select t from events where sid=? and ev='prompt' "
                            "order by t desc limit 1", (sid,)
                        ).fetchone()
                        if begun:
                            started_at = begun["t"]
                except Exception:
                    pass

            cur = conn.execute(
                "insert into observations (sid, proj, cwd, started_at, ended_at, "
                " kind, title, narrative, facts, prompt, files_read, "
                " files_modified, commands, tools, tests, tokens, model, source, "
                " created_at) "
                "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid, proj, cwd or fields["cwd"], started_at, ended_at or now,
                    classify(prompt, fields["files_modified"],
                             fields["files_read"], fields["tests"]),
                    _clip(prompt, MAX_TITLE),
                    fields["narrative"],
                    fields["facts"],
                    _clip(prompt, events.MAX_MSG_CHARS),
                    json.dumps(fields["files_read"]),
                    json.dumps(fields["files_modified"]),
                    json.dumps(fields["commands"]),
                    json.dumps(fields["tools"]),
                    fields["tests"],
                    int(sum(int(tok.get(k, 0) or 0) for k in events.TOK_KEYS)),
                    model,
                    "extract",
                    now,
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            if own:
                conn.close()
    except Exception:
        return 0
