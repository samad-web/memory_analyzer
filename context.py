r"""The orientation a session starts with.

``SessionStart`` can hand a block of text to a fresh session, and this builds
it: where the project is going, which milestone is live, what has already been
settled, and what the last few turns actually did.

**Small on purpose.** The temptation is to inject everything -- fifty
observations, the whole milestone list, every decision -- and it is the wrong
instinct twice over. It costs tokens on every single session start, and a wall
of stale context is worse than none because it crowds out what the agent is
about to read for itself. So this stays inside a character budget and leans on
the agent pulling detail on demand instead.

Order is deliberate: goal first, because it is the thing that answers "what
next"; recent work last, because it is the thing most easily re-derived by
looking at the repository.

Two rules this module must not break:

* **Never fail.** It runs from a hook. A traceback here would land in the
  session's context, or worse, disrupt the turn. Everything is wrapped and the
  fallback is silence.
* **Never assume the console is UTF-8.** On Windows the default encoding is
  cp1252, and real prompts contain real characters -- an em dash, a rupee sign.
  Printing one through an unconfigured stdout raises ``UnicodeEncodeError``,
  which is exactly how a hook that worked in testing breaks on a Tuesday. The
  writer here reconfigures the stream and falls back to replacing what it
  cannot encode.
"""

from __future__ import annotations

import json
import os
import sys
import time

# Total characters of context. Roughly 1,000 tokens at the usual ratio, spent
# once per session start -- enough for a goal, a milestone and a handful of
# one-line summaries.
BUDGET = 4000

# How many past turns to summarise, before the budget trims further.
RECENT_TURNS = 6

MARK = {"pending": " ", "active": "~", "done": "x", "blocked": "!"}


def _ago(ts: float | None) -> str:
    if not ts:
        return ""
    delta = max(0.0, time.time() - float(ts))
    if delta < 3600:
        return "%dm ago" % int(delta // 60)
    if delta < 86400:
        return "%dh ago" % int(delta // 3600)
    return "%dd ago" % int(delta // 86400)


def _lines(proj: str, conn=None) -> list[str]:
    import store

    b = store.brief(proj, conn=conn)
    out: list[str] = []

    goal = b["goal"]
    if goal and goal["statement"]:
        out.append("GOAL  " + goal["statement"])
        for c in (goal["constraints"] or "").splitlines():
            if c.strip():
                out.append("      constraint: " + c.strip())
        out.append("")

    if b["total"]:
        out.append("MILESTONES  %d of %d done" % (b["done"], b["total"]))
        for m in b["milestones"]:
            # Finished milestones are a count, not a list. What matters is the
            # live one and what is next.
            if m["status"] == "done":
                continue
            out.append("  [%s] %s" % (MARK.get(m["status"], " "), m["title"]))
            if m["acceptance"]:
                out.append("        acceptance: " + m["acceptance"])
            if m["check_cmd"]:
                out.append("        check: " + m["check_cmd"])
            if m["blocked_by"]:
                out.append("        blocked: " + m["blocked_by"])
        out.append("")

    if b["decisions"]:
        out.append("SETTLED")
        for d in b["decisions"][:5]:
            line = "  - " + d["title"]
            if d["rationale"]:
                line += " (" + d["rationale"] + ")"
            out.append(line)
        out.append("")

    recent = b["recent"][:RECENT_TURNS]
    if recent:
        out.append("RECENT WORK")
        for o in recent:
            when = _ago(o["ended_at"])
            head = o["title"] or o["prompt"] or "(no prompt recorded)"
            out.append("  - %s%s" % (head, (" [%s]" % when) if when else ""))
            try:
                mod = json.loads(o["files_modified"] or "[]")
            except ValueError:
                mod = []
            if mod:
                names = [os.path.basename(p) for p in mod[:5]]
                more = "" if len(mod) <= 5 else " +%d" % (len(mod) - 5)
                out.append("      changed: " + ", ".join(names) + more)
            if o["tests"]:
                out.append("      tests: " + o["tests"])
        out.append("")

    return out


def render(proj: str = "", budget: int = BUDGET, conn=None) -> str:
    """The context block for one project, or "" when there is nothing to say.

    Trimming is line-wise from the end, so the goal and the live milestone
    survive a tight budget and the oldest turn summaries are what fall off.
    """
    try:
        proj = proj or os.path.basename(os.getcwd().rstrip("\\/"))
        lines = _lines(proj, conn=conn)
        if not lines:
            return ""

        header = "Project memory for %s (from the local store; " \
                 "search it with store.py for more)." % proj
        body = [header, ""] + lines

        while body and len("\n".join(body)) > budget:
            body.pop()
        text = "\n".join(body).rstrip()
        # A header and nothing else is noise, not context.
        return text if len(text) > len(header) + 8 else ""
    except Exception:
        return ""


def emit(proj: str = "") -> int:
    """Print the SessionStart hook payload. Always exits 0.

    The payload shape is what Claude Code reads to add context to a starting
    session; printing nothing is a valid no-op, which is the right behaviour
    for a project that has no goal and no history yet.
    """
    try:
        text = render(proj)
        if not text:
            return 0
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": text,
            }
        }
        _write(json.dumps(payload))
    except Exception:
        pass
    return 0


def _write(text: str) -> None:
    """Write to stdout without caring what the console's encoding is."""
    try:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except UnicodeEncodeError:
        # Last resort: push bytes straight at the buffer, dropping whatever the
        # stream cannot represent.
        try:
            sys.stdout.buffer.write(text.encode("utf-8", "replace") + b"\n")
            sys.stdout.buffer.flush()
        except Exception:
            pass
    except Exception:
        pass


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    if "--raw" in sys.argv:
        _write(render(name if name != "--raw" else ""))
    else:
        raise SystemExit(emit(name))
