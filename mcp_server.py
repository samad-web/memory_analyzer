r"""An MCP server over the store, so an agent can ask mid-turn.

``context.py`` tells a session what it needs at session *start*: one read,
inside a character budget, decided before the agent knows what it has been
asked to do. Twenty turns later, standing in front of "have we tried this
before?", it has no way to ask -- the answer is in ``store.db`` and
unreachable. So it re-derives what it already knew, and that lands on the one
number this project exists to reduce: prompts per completed milestone.

This exposes the store as MCP tools. There is no framework here on purpose:
MCP over stdio is newline-delimited JSON-RPC 2.0, which is ``json`` and a
``while`` loop, and "no new runtime dependencies" is a constraint in GOAL.md.

Three rules the protocol imposes, each a silent killer when broken:

* **stdout belongs to the protocol.** One stray ``print`` corrupts the frame
  and the client sees a dead server. Diagnostics go to stderr, always.
* **A notification gets no reply.** A JSON-RPC message without an ``id`` is a
  notification; answering it is a protocol violation.
* **A handler must not be able to kill the loop.** A server that dies takes the
  agent's memory with it for the rest of the session, so every handler is
  wrapped and failure comes back as tool content.

Deliberately *not* exposed: ``goals.verify``. It runs the ``check_cmd`` shell
strings out of GOAL.md, and reachable from an agent mid-turn that is a shell
escape around the permission system. Verification stays a human action.
"""

from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import agent_alert
import goals
import store

NAME = "memory-analyzer"
VERSION = "1.0.0"

# What we speak. A client that asks for an older revision we recognise is
# answered in its own version rather than refused -- the tool surface is
# identical across these, and a version string is not worth a failed handshake.
PROTOCOL = "2025-06-18"
KNOWN_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

# A tool result is a context cost, exactly like an injected payload. The
# dashboards already learned this the expensive way: untrimmed rows were 15 KB
# each. These are the ceilings, and the tools say what they hid rather than
# hiding it quietly.
MAX_RESULT = 6000
MAX_LIMIT = 50
DEFAULT_LIMIT = 8


def _project(given: str = "") -> str:
    """Which project a tool call is about.

    Defaults from the server's own working directory via the same repository
    walk the board uses, so an agent does not have to know what its project is
    called -- and so the name it gets is the one on the tile.
    """
    given = (given or "").strip()
    if given:
        return given
    try:
        return agent_alert.project_name(os.getcwd())
    except Exception:
        return ""


def _limit(value, default: int = DEFAULT_LIMIT) -> int:
    """Clamp a caller-supplied limit.

    Unclamped it is a context-exhaustion vector: one tool call asking for ten
    thousand rows.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_LIMIT, n))


def _cap(text: str) -> str:
    if len(text) <= MAX_RESULT:
        return text
    return text[: MAX_RESULT - 40].rstrip() + "\n... (truncated)"


def _ago(ts) -> str:
    try:
        delta = time.time() - float(ts or 0)
    except (TypeError, ValueError):
        return "?"
    if delta < 90:
        return "just now"
    if delta < 5400:
        return "%dm ago" % (delta // 60)
    if delta < 172800:
        return "%dh ago" % (delta // 3600)
    return "%dd ago" % (delta // 86400)


def _more(shown: int, total: int) -> str:
    return " (+%d more)" % (total - shown) if total > shown else ""


def _render_row(row: dict, full: bool = False) -> str:
    """One observation as lines a model can read without parsing anything."""
    out = ["#%s  %s  %s  %s" % (row.get("id"), row.get("when", ""),
                               row.get("kind", ""), row.get("proj", ""))]
    title = (row.get("title") or "").strip()
    if title:
        out.append("  " + title)
    if row.get("tests"):
        out.append("  tests: %s" % row["tests"])
    modified = row.get("files_modified") or []
    if modified:
        names = modified if full else [os.path.basename(p) for p in modified]
        out.append("  changed: %s%s" % (
            ", ".join(names), _more(len(modified), row.get("files_modified_n", 0))))
    if full and row.get("files_read"):
        out.append("  read: %s%s" % (
            ", ".join(os.path.basename(p) for p in row["files_read"]),
            _more(len(row["files_read"]), row.get("files_read_n", 0))))
    commands = row.get("commands") or []
    if commands:
        out.append("  ran:")
        out.extend("    " + c for c in commands)
    elif row.get("commands_n"):
        out.append("  ran: %d command(s) -- observation_detail(%s) for them"
                   % (row["commands_n"], row.get("id")))
    narrative = (row.get("narrative") or "").strip()
    if narrative:
        out.append("  " + narrative)
    if full and (row.get("prompt") or "").strip():
        out.append("  asked: " + row["prompt"].strip())
    return "\n".join(out)


# --------------------------------------------------------------- the tools --


def search_memory(query: str = "", project: str = "", limit=DEFAULT_LIMIT) -> str:
    """Full-text search across remembered turns."""
    query = (query or "").strip()
    if not query:
        return "search_memory needs a query. Use project_status for an overview."
    proj = (project or "").strip()
    conn = store.connect()
    try:
        rows = store.search(query, proj=proj, limit=_limit(limit), conn=conn)
        total = conn.execute("select count(*) n from observations").fetchone()["n"]
    finally:
        conn.close()
    scope = " in %s" % proj if proj else ""
    if not rows:
        if not total:
            return ("Nothing remembered yet -- the store is empty. Observations "
                    "are written when a turn finishes.")
        return "No match for %r%s, across %d remembered turn(s)." % (
            query, scope, total)
    head = "%d match(es) for %r%s:" % (len(rows), query, scope)
    return _cap("\n\n".join([head] + [_render_row(store._row_out(r)) for r in rows]))


def project_status(project: str = "") -> str:
    """Where a project is: its goal, the live milestone, what is settled."""
    proj = _project(project)
    if not proj:
        return "Could not tell which project this is, and none was given."
    _sync(proj)
    conn = store.connect()
    try:
        data = store.brief(proj, conn=conn)
        turns = conn.execute(
            "select count(*) n, max(ended_at) last from observations where proj=?",
            (proj,)).fetchone()
    finally:
        conn.close()

    lines = ["%s -- %d of %d milestone(s) done" % (
        proj, data["done"], data["total"])]
    goal = data.get("goal")
    if goal and (goal.get("statement") or "").strip():
        lines += ["", "goal: " + goal["statement"].strip()]
        if (goal.get("constraints") or "").strip():
            lines.append("constraints: " + goal["constraints"].strip())
    elif not data["total"]:
        lines.append("No GOAL.md board for this project yet.")

    for label, milestone in (("active", data.get("active")),
                             ("next", data.get("next"))):
        if milestone:
            lines.append("%s: %s" % (label, milestone["title"]))
            if (milestone.get("acceptance") or "").strip():
                lines.append("  acceptance: " + milestone["acceptance"].strip())
    for milestone in data.get("blocked") or []:
        lines.append("blocked: %s -- %s" % (
            milestone["title"],
            milestone.get("blocked_by") or "reason not recorded"))

    if data.get("decisions"):
        lines += ["", "settled already (%d):" % len(data["decisions"])]
        lines += ["  - " + d["title"] for d in data["decisions"]]

    lines.append("")
    if turns["n"]:
        lines.append("%d turn(s) remembered, last %s. Use search_memory to look "
                     "inside them." % (turns["n"], _ago(turns["last"])))
    else:
        lines.append("No turns remembered for this project yet.")
    return _cap("\n".join(lines))


def milestone_state(project: str = "") -> str:
    """The whole board, with what each milestone's completion rests on."""
    proj = _project(project)
    if not proj:
        return "Could not tell which project this is, and none was given."
    _sync(proj)
    conn = store.connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "select * from milestones where proj=? order by ordinal, id", (proj,))]
    finally:
        conn.close()
    if not rows:
        return ("No milestones for %s. The board is read from GOAL.md at the "
                "repository root." % proj)

    marks = {"pending": " ", "active": "~", "done": "x", "blocked": "!"}
    lines = ["Board for %s (%d/%d done):" % (
        proj, sum(1 for r in rows if r["status"] == "done"), len(rows))]
    for row in rows:
        lines.append("")
        lines.append("[%s] %s" % (marks.get(row["status"], "?"), row["title"]))
        for field, label in (("acceptance", "acceptance"), ("check_cmd", "check"),
                             ("evidence", "evidence"), ("blocked_by", "blocked by")):
            value = (row.get(field) or "").strip()
            if value:
                lines.append("    %s: %s" % (label, value))
        if not (row.get("check_cmd") or "").strip():
            lines.append("    check: none -- completion cannot be asserted")
    lines += ["", "Status is asserted by running the check command, not claimed. "
                  "Run `python goals.py verify` to re-prove the board."]
    return _cap("\n".join(lines))


def recent_decisions(project: str = "", limit=DEFAULT_LIMIT) -> str:
    """Choices already made, so they are not re-litigated."""
    proj = _project(project)
    conn = store.connect()
    try:
        sql = "select * from decisions "
        args: list = []
        if proj:
            sql += "where proj=? "
            args.append(proj)
        sql += "order by decided_at desc limit ?"
        args.append(_limit(limit))
        rows = [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()
    if not rows:
        return "No decisions recorded%s yet." % (" for %s" % proj if proj else "")
    lines = ["%d decision(s), newest first:" % len(rows)]
    for row in rows:
        lines.append("")
        lines.append("- %s  (%s)" % (row["title"], _ago(row["decided_at"])))
        if (row.get("rationale") or "").strip():
            lines.append("  because: " + row["rationale"].strip())
    return _cap("\n".join(lines))


def observation_detail(id=None) -> str:
    """One remembered turn in full: every command, every file, the prompt."""
    conn = store.connect()
    try:
        row = store.observation(id, conn=conn)
    finally:
        conn.close()
    if not row:
        return "No such observation: %r." % (id,)
    return _cap(_render_row(row, full=True))


def _sync(proj: str) -> None:
    """Reflect GOAL.md before reporting the board -- but only our own.

    An edit to the file should not need a separate command before an agent can
    see it. ``sync`` only reads the file and writes the database -- the file
    owns structure -- and a malformed or missing GOAL.md must degrade to "no
    board", never to a failed tool call.

    The guard is the load-bearing part. ``goals.sync`` resolves GOAL.md against
    the working directory, so syncing on behalf of a project we are *not*
    standing in would read this repository's board and write it under that
    project's name -- one read tool corrupting another project's milestones.
    A named project that is not this one is reported from the database alone.
    """
    try:
        if proj != agent_alert.project_name(os.getcwd()):
            return
        goals.sync(proj, repo=agent_alert.project_root(os.getcwd()))
    except Exception as exc:  # noqa: BLE001 -- a bad GOAL.md is not a crash
        print("goals.sync(%s) failed: %s" % (proj, exc), file=sys.stderr)


_STRING = {"type": "string"}

TOOLS = (
    {
        "name": "search_memory",
        "description": (
            "Search what past turns in this project actually did -- the files "
            "they changed, the commands they ran, the tests that passed. Use it "
            "before re-deriving something: 'have we touched this before?'"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": dict(_STRING, description="Words to search for."),
                "project": dict(_STRING, description=(
                    "Project name. Defaults to the repository this server "
                    "runs in.")),
                "limit": {"type": "integer",
                          "description": "Results to return (1-%d)." % MAX_LIMIT},
            },
            "required": ["query"],
        },
        "handler": search_memory,
    },
    {
        "name": "project_status",
        "description": (
            "Where this project is going: the goal statement, the live "
            "milestone, what has been settled, and how much is remembered."),
        "inputSchema": {
            "type": "object",
            "properties": {"project": dict(_STRING, description=(
                "Project name. Defaults to the repository this server runs in."))},
        },
        "handler": project_status,
    },
    {
        "name": "milestone_state",
        "description": (
            "The full milestone board with each milestone's acceptance "
            "criteria, check command and evidence."),
        "inputSchema": {
            "type": "object",
            "properties": {"project": dict(_STRING, description="Project name.")},
        },
        "handler": milestone_state,
    },
    {
        "name": "recent_decisions",
        "description": (
            "Decisions already taken in this project and why, so they are not "
            "re-litigated."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": dict(_STRING, description="Project name."),
                "limit": {"type": "integer",
                          "description": "Decisions to return (1-%d)." % MAX_LIMIT},
            },
        },
        "handler": recent_decisions,
    },
    {
        "name": "observation_detail",
        "description": (
            "One remembered turn in full, by the id that search_memory prints: "
            "every command, every file and the prompt that started it."),
        "inputSchema": {
            "type": "object",
            "properties": {"id": {
                "type": "integer",
                "description": "Observation id, as printed by search_memory."}},
            "required": ["id"],
        },
        "handler": observation_detail,
    },
)

BY_NAME = {t["name"]: t for t in TOOLS}


def tool_list() -> list[dict]:
    """The tool list as the wire wants it: no Python callables in the JSON."""
    return [{k: v for k, v in t.items() if k != "handler"} for t in TOOLS]


def _content(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": str(text)}], "isError": error}


def call_tool(name: str, arguments: dict | None) -> dict:
    """Run one tool. Never raises: a dead server is worse than a bad answer.

    Unknown argument names are dropped rather than passed on, so a client that
    invents a parameter gets an answer instead of a ``TypeError``.
    """
    tool = BY_NAME.get(name)
    if tool is None:
        return _content("No such tool: %s. Available: %s"
                        % (name, ", ".join(sorted(BY_NAME))), error=True)
    arguments = arguments if isinstance(arguments, dict) else {}
    allowed = set(tool["inputSchema"].get("properties") or {})
    kwargs = {k: v for k, v in arguments.items() if k in allowed}
    try:
        return _content(tool["handler"](**kwargs))
    except Exception as exc:  # noqa: BLE001
        print("%s failed: %r" % (name, exc), file=sys.stderr)
        return _content("%s failed: %s" % (name, exc), error=True)


# ------------------------------------------------------------ the protocol --

PARSE_ERROR = -32700
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": code, "message": message}}


def _dispatch(method: str, params: dict):
    if method == "initialize":
        asked = str(params.get("protocolVersion") or "")
        return {
            "protocolVersion": asked if asked in KNOWN_PROTOCOLS else PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": NAME, "version": VERSION},
            "instructions": (
                "Memory for this machine's Claude Code sessions. Ask "
                "project_status when you start on something, search_memory "
                "before re-deriving anything, and recent_decisions before "
                "reopening a settled choice."),
        }
    if method.startswith("notifications/"):
        return {}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": tool_list()}
    if method == "tools/call":
        return call_tool(params.get("name") or "", params.get("arguments"))
    # A client probing for capabilities we do not advertise should get an empty
    # list, not an error it has to interpret.
    if method == "resources/list":
        return {"resources": []}
    if method == "prompts/list":
        return {"prompts": []}
    raise KeyError(method)


def handle(message: dict) -> dict | None:
    """One request in, one response out -- or ``None`` for a notification.

    A JSON-RPC message with no ``id`` is a notification and must not be
    answered. ``notifications/initialized`` is the one every client sends, and
    replying to it is how a handshake ends in a stall.
    """
    method = message.get("method") or ""
    mid = message.get("id")
    is_request = mid is not None

    try:
        result = _dispatch(method, message.get("params") or {})
    except KeyError:
        return _error(mid, METHOD_NOT_FOUND, "unknown method: %s" % method) \
            if is_request else None
    except Exception as exc:  # noqa: BLE001
        print("%s failed: %r" % (method, exc), file=sys.stderr)
        return _error(mid, INTERNAL_ERROR, str(exc)) if is_request else None

    if not is_request:
        return None
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _send(stdout, payload: dict) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stdout.flush()


def serve(stdin=None, stdout=None) -> int:
    """Read framed JSON-RPC until EOF.

    The streams are reconfigured to UTF-8 explicitly because a Windows console
    defaults to cp1252, which would mangle a narrative and corrupt the frame --
    and ``python -I``, which is how a hook-adjacent script gets launched,
    ignores ``PYTHONIOENCODING``.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for stream in (stdin, stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    while True:
        line = stdin.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _send(stdout, _error(None, PARSE_ERROR, "invalid JSON"))
            continue
        if not isinstance(message, dict):
            _send(stdout, _error(None, PARSE_ERROR, "expected a JSON object"))
            continue
        response = handle(message)
        if response is not None:
            _send(stdout, response)


# -------------------------------------------------------------- the install --

def config_path(scope: str = "user") -> str:
    """Where the server registration goes.

    ``user`` is Claude Code's global ``~/.claude.json``, which is what makes the
    memory reachable from *every* project -- the point of the feature.
    ``project`` writes a committed ``.mcp.json`` beside the code, which is the
    right scope for a teammate cloning this repository.
    """
    if scope == "project":
        return os.path.join(HERE, ".mcp.json")
    return os.path.join(os.path.expanduser("~"), ".claude.json")


def entry(python: str = "python", root: str = "") -> dict:
    """The registration for one server.

    Forward slashes even on Windows, for the reason ``hooks.py`` gives: the
    value is JSON, and a backslash path has to be escaped twice to survive the
    file, which is a reliable way to produce a server that silently never runs.
    """
    script = os.path.join(root or HERE, "mcp_server.py").replace("\\", "/")
    return {"type": "stdio", "command": python, "args": [script], "env": {}}


def install(scope: str = "user", python: str = "python", root: str = "") -> dict:
    """Register the server, merging rather than overwriting.

    Same three rules as ``hooks.py``: merge (that file holds the whole Claude
    Code configuration), idempotent (replace our own entry, never append a
    second), and back up first.
    """
    path = config_path(scope)
    config = _read_config(path)
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    before = json.dumps(servers.get(NAME), sort_keys=True)
    servers[NAME] = entry(python, root)
    config["mcpServers"] = servers
    _write_config(path, config)
    return {"path": path, "server": NAME,
            "changed": before != json.dumps(servers[NAME], sort_keys=True)}


def uninstall(scope: str = "user") -> dict:
    path = config_path(scope)
    config = _read_config(path)
    servers = config.get("mcpServers")
    removed = isinstance(servers, dict) and servers.pop(NAME, None) is not None
    if removed:
        config["mcpServers"] = servers
        _write_config(path, config)
    return {"path": path, "removed": removed}


def status(scope: str = "user") -> dict:
    """Whether the registration is present and points at this copy."""
    path = config_path(scope)
    servers = _read_config(path).get("mcpServers")
    found = servers.get(NAME) if isinstance(servers, dict) else None
    return {"path": path, "registered": found is not None,
            "current": found == entry(found.get("command", "python"))
            if isinstance(found, dict) else False,
            "entry": found}


def _read_config(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            config = json.load(fh)
        return config if isinstance(config, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_config(path: str, config: dict) -> None:
    """Write via a backup and a temporary file.

    ``~/.claude.json`` holds the whole Claude Code configuration, so a partial
    write is not a cosmetic failure. ``os.replace`` is atomic on Windows too.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if os.path.exists(path):
        try:
            backup = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
            with open(path, "rb") as src, open(backup, "wb") as dst:
                dst.write(src.read())
        except OSError:
            pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
    os.replace(tmp, path)


_USAGE = """usage: python mcp_server.py [serve|install|uninstall|status|tools|call]

  serve       speak MCP on stdin/stdout (what Claude Code runs; the default)
  install     register the server in Claude Code's config
  uninstall   remove the registration
  status      report whether it is registered
  tools       print the tool list, as a sanity check
  call NAME [key=value ...]
              run one tool from the shell, as a sanity check

  --project   write .mcp.json beside this repo instead of ~/.claude.json
  --python P  the interpreter to register (default: python)
"""


def main(argv: list[str]) -> int:
    args = [a for a in argv if a != "--project"]
    scope = "project" if "--project" in argv else "user"
    python = "python"
    if "--python" in args:
        i = args.index("--python")
        python = args[i + 1] if i + 1 < len(args) else "python"
        del args[i:i + 2]

    command = args[0] if args else "serve"
    if command in ("-h", "--help", "help"):
        print(_USAGE)
        return 0
    if command == "serve":
        return serve()
    if command == "install":
        done = install(scope, python)
        print("%s %s in %s" % (NAME, "registered" if done["changed"]
                               else "already registered", done["path"]))
        print("Restart Claude Code, then /mcp should list it.")
        return 0
    if command == "uninstall":
        done = uninstall(scope)
        print("%s in %s" % ("removed" if done["removed"] else "not present",
                            done["path"]))
        return 0
    if command == "status":
        state = status(scope)
        print("%s: %s" % (state["path"],
                          "registered" if state["registered"] else "not registered"))
        if state["entry"]:
            print(json.dumps(state["entry"], indent=2))
        return 0 if state["registered"] else 1
    if command == "tools":
        for tool in TOOLS:
            print("%-20s %s" % (tool["name"], tool["description"]))
        return 0
    if command == "call":
        if len(args) < 2:
            print(_USAGE)
            return 2
        kwargs = dict(a.split("=", 1) for a in args[2:] if "=" in a)
        result = call_tool(args[1], kwargs)
        print(result["content"][0]["text"])
        return 1 if result["isError"] else 0
    print(_USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
