r"""Where a project is going, and how it proves it got there.

The activity log and ``observations`` are both backward-looking: they record
what happened. This module is the forward-looking half, and it is the half that
reduces how often a human has to say what next.

Measured on this machine over nine days: 351 prompts against 219 completed
turns, across 15 projects, while agents blocked on a human only 62 times. Four
turns in five never interrupt anyone. So the scarce resource is not attention
during a turn -- it is intent at the start of one. A project that carries its
own goal can answer "what next" without being told.

**Three roles, deliberately separated:**

* ``sync`` reads ``GOAL.md`` -- the human's file, committed in the repo -- and
  takes it as authoritative for *structure*: the goal statement, the milestone
  titles, their order, their acceptance criteria and their check commands.
* ``verify`` runs each milestone's ``check`` command and is the only thing that
  may mark one ``done``. Status is *asserted*, never assumed, which is what
  stops an unattended run talking itself into having finished.
* ``export`` writes status back into ``GOAL.md``, so progress shows up in a
  diff rather than only in a database.

``verify`` runs commands out of a file in your repository, so it is a CLI
command you invoke, never something a hook fires. Hooks here stay read-only.

The file format is forgiving:

    # Goal
    Ship the control plane: memory, remote access, supervised autonomy.

    ## Constraints
    - No new runtime dependencies.
    - Hooks never touch the network.

    ## Milestones
    - [x] Indexed store behind the event log
          check: python store.py stats
    - [~] Deterministic observation extractor
          acceptance: a finished turn yields files, commands and a narrative
          check: python -m pytest -q tests/test_observe.py
    - [ ] SessionStart injection
    - [!] Mobile push
          blocked: needs a VAPID key

Checkbox marks: ``[ ]`` pending, ``[~]`` active, ``[x]`` done, ``[!]`` blocked.
"""

from __future__ import annotations

import os
import re
import subprocess
import time

import store

GOAL_FILENAME = "GOAL.md"

# How long a single check command may run before it is given up on. A check is
# meant to be a fast assertion -- a test file, a build, a smoke script -- not a
# full suite, and a milestone that needs ten minutes to verify wants splitting.
CHECK_TIMEOUT = 300

_MARKS = {
    " ": store.PENDING,
    "": store.PENDING,
    "~": store.ACTIVE,
    "-": store.ACTIVE,
    "x": store.DONE,
    "X": store.DONE,
    "!": store.BLOCKED,
}

_ITEM_RE = re.compile(r"^\s*[-*]\s*\[(.?)\]\s*(.+?)\s*$")
_FIELD_RE = re.compile(r"^\s*(acceptance|check|blocked|evidence)\s*:\s*(.*)$", re.I)
_HEADING_RE = re.compile(r"^\s*#{1,6}\s*(.+?)\s*$")


def goal_path(repo: str = "") -> str:
    return os.path.join(repo or os.getcwd(), GOAL_FILENAME)


def parse(text: str) -> dict:
    """Parse a GOAL.md into ``{statement, constraints, milestones}``.

    Unknown sections are ignored rather than rejected: this is a file humans
    edit, and refusing to read one because it grew an extra heading would be a
    good way to have it go stale.
    """
    statement: list[str] = []
    constraints: list[str] = []
    milestones: list[dict] = []
    section = "goal"
    current: dict | None = None
    # Which field a bare continuation line belongs to. A human wrapping an
    # acceptance criterion over two lines means one criterion, not half of one,
    # and silently keeping the first line is the worst of the three options.
    last_field = "acceptance"

    for raw in (text or "").splitlines():
        head = _HEADING_RE.match(raw)
        if head:
            name = head.group(1).strip().lower()
            current = None
            if name.startswith("goal"):
                section = "goal"
            elif name.startswith("constraint"):
                section = "constraints"
            elif name.startswith("milestone"):
                section = "milestones"
            else:
                section = "other"
            continue

        if section == "goal":
            if raw.strip():
                statement.append(raw.strip())
            continue

        if section == "constraints":
            item = _ITEM_RE.match(raw)
            if item:
                constraints.append(item.group(2).strip())
            elif raw.strip().startswith(("-", "*")):
                constraints.append(raw.strip().lstrip("-* ").strip())
            elif raw.strip() and constraints and raw[:1].isspace():
                # An indented line under a bullet continues it.
                constraints[-1] = (constraints[-1] + " " + raw.strip()).strip()
            continue

        if section == "milestones":
            item = _ITEM_RE.match(raw)
            if item:
                mark = (item.group(1) or " ").strip() or " "
                current = {
                    "title": item.group(2).strip(),
                    "status": _MARKS.get(mark, store.PENDING),
                    "acceptance": "",
                    "check_cmd": "",
                    "blocked_by": "",
                    "ordinal": len(milestones),
                }
                milestones.append(current)
                last_field = "acceptance"
                continue
            field = _FIELD_RE.match(raw)
            if field and current is not None:
                key, value = field.group(1).lower(), field.group(2).strip()
                if key == "acceptance":
                    current["acceptance"] = value
                    last_field = "acceptance"
                elif key == "check":
                    current["check_cmd"] = value.strip("`")
                    last_field = "check_cmd"
                elif key == "blocked":
                    current["blocked_by"] = value
                    current["status"] = store.BLOCKED
                    last_field = "blocked_by"
                elif key == "evidence":
                    last_field = "acceptance"
                continue
            if raw.strip() and current is not None and raw[:1].isspace():
                # A wrapped value. `check` is a command, so it is joined
                # without a space; prose gets one.
                joiner = "" if last_field == "check_cmd" else " "
                current[last_field] = (
                    (current[last_field] + joiner + raw.strip()).strip()
                    if current[last_field] else raw.strip()
                )
            continue

    return {
        "statement": " ".join(statement).strip(),
        "constraints": constraints,
        "milestones": milestones,
    }


def sync(proj: str, repo: str = "", conn=None) -> dict:
    """Read GOAL.md into the store. Structure from the file, status from the DB.

    The split matters. The file owns what the milestones *are*; the database
    owns how far along they are, because status is set by ``verify`` from
    evidence and a hand-edited checkbox should not silently undo that. The one
    exception is a human explicitly writing ``[x]`` or ``[!]``, which is taken
    as an override -- they are allowed to overrule a check.
    """
    path = goal_path(repo)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return {"ok": False, "error": "no %s in %s" % (GOAL_FILENAME, repo or os.getcwd())}

    parsed = parse(text)
    own = conn is None
    conn = conn or store.connect()
    try:
        now = time.time()
        conn.execute(
            "insert into goals (proj, statement, constraints, source, revised_at) "
            "values (?,?,?,?,?) on conflict(proj) do update set "
            "statement=excluded.statement, constraints=excluded.constraints, "
            "source=excluded.source, revised_at=excluded.revised_at",
            (proj, parsed["statement"], "\n".join(parsed["constraints"]), path, now),
        )

        added = updated = 0
        for m in parsed["milestones"]:
            existing = conn.execute(
                "select * from milestones where proj=? and title=?", (proj, m["title"])
            ).fetchone()
            if existing is None:
                conn.execute(
                    "insert into milestones (proj, ordinal, title, acceptance, "
                    " check_cmd, status, file_status, blocked_by, created_at, "
                    " updated_at) values (?,?,?,?,?,?,?,?,?,?)",
                    (proj, m["ordinal"], m["title"], m["acceptance"],
                     m["check_cmd"], m["status"], m["status"], m["blocked_by"],
                     now, now),
                )
                added += 1
            else:
                # Who wins is decided by whether the *file* changed, not by
                # which status looks further along. An edited checkbox is a
                # human overruling a check and takes effect; an untouched file
                # leaves whatever `verify` established, so a verified milestone
                # is not quietly reopened by a GOAL.md that was never updated.
                status = existing["status"]
                if m["status"] != (existing["file_status"] or ""):
                    status = m["status"]
                conn.execute(
                    "update milestones set ordinal=?, acceptance=?, check_cmd=?, "
                    " status=?, file_status=?, blocked_by=?, updated_at=? "
                    "where id=?",
                    (m["ordinal"], m["acceptance"], m["check_cmd"], status,
                     m["status"], m["blocked_by"], now, existing["id"]),
                )
                updated += 1

        conn.commit()
        return {
            "ok": True, "path": path, "added": added, "updated": updated,
            "milestones": len(parsed["milestones"]),
        }
    finally:
        if own:
            conn.close()


def verify(proj: str, repo: str = "", conn=None, only: str = "") -> list[dict]:
    """Run each milestone's check command and record what it proved.

    This is the assertion step. A milestone with no ``check`` is left alone and
    reported as unverifiable -- it needs a human to confirm it, and saying so is
    more useful than quietly calling it done.

    Only ever invoked explicitly. It executes commands out of a file in the
    repository, which is fine when a person asked for it and is not something a
    hook should ever do on its own.

    Each result is committed before the next command runs. Batching the writes
    to the end leaves a write transaction open across every later subprocess,
    and a check command that touches the store -- which the interesting ones do,
    since the store is what this project builds -- then dies on a locked
    database and is recorded as a failure it never had.
    """
    own = conn is None
    conn = conn or store.connect()
    cwd = repo or os.getcwd()
    try:
        rows = [dict(r) for r in conn.execute(
            "select * from milestones where proj=? order by ordinal, id", (proj,)
        )]
        out = []
        for m in rows:
            if only and only.lower() not in m["title"].lower():
                continue
            cmd = (m["check_cmd"] or "").strip()
            if not cmd:
                out.append({**m, "result": "unverifiable", "detail": "no check command"})
                continue
            try:
                proc = subprocess.run(
                    cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                    timeout=CHECK_TIMEOUT, errors="replace",
                )
                passed = proc.returncode == 0
                tail = ((proc.stdout or "") + (proc.stderr or "")).strip()
                detail = " ".join(tail.split())[-300:]
            except subprocess.TimeoutExpired:
                passed, detail = False, "timed out after %ds" % CHECK_TIMEOUT
            except OSError as exc:
                passed, detail = False, "could not run: %s" % exc

            stamp = time.strftime("%Y-%m-%d %H:%M")
            evidence = "%s %s: %s" % (
                stamp, "passed" if passed else "failed", cmd)
            status = store.DONE if passed else (
                store.ACTIVE if m["status"] == store.DONE else m["status"])
            conn.execute(
                "update milestones set status=?, evidence=?, updated_at=? where id=?",
                (status, evidence + (" | " + detail if detail else ""),
                 time.time(), m["id"]),
            )
            conn.commit()
            out.append({**m, "status": status,
                        "result": "passed" if passed else "failed",
                        "detail": detail})
        return out
    finally:
        if own:
            conn.close()


def export(proj: str, repo: str = "", conn=None) -> dict:
    """Rewrite GOAL.md's checkboxes from the stored status.

    Closes the loop: ``sync`` reads structure in, ``verify`` establishes status,
    and this puts status back where a human (and a diff) can see it. Only the
    mark between the brackets is touched -- prose, ordering and comments in the
    file are left exactly as written.
    """
    path = goal_path(repo)
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines(keepends=True)
    except OSError:
        return {"ok": False, "error": "no %s" % path}

    own = conn is None
    conn = conn or store.connect()
    try:
        status_by_title = {
            r["title"]: r["status"]
            for r in conn.execute(
                "select title, status from milestones where proj=?", (proj,))
        }
    finally:
        if own:
            conn.close()

    mark_for = {store.PENDING: " ", store.ACTIVE: "~",
                store.DONE: "x", store.BLOCKED: "!"}
    changed = 0
    for i, line in enumerate(lines):
        item = _ITEM_RE.match(line.rstrip("\r\n"))
        if not item:
            continue
        title = item.group(2).strip()
        if title not in status_by_title:
            continue
        want = mark_for.get(status_by_title[title], " ")
        new = re.sub(r"\[(.?)\]", "[%s]" % want, line, count=1)
        if new != line:
            lines[i] = new
            changed += 1

    if changed:
        try:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.writelines(lines)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
    return {"ok": True, "path": path, "changed": changed}


_TEMPLATE = """# Goal

<One or two sentences: the end state this project is working towards. Written
by you, revised rarely. This is what lets a session -- or an unattended run --
know what next without being told.>

## Constraints

- <Things that must stay true. "No new runtime dependencies", that sort of thing.>

## Milestones

- [ ] <First milestone>
      acceptance: <how you would know it is done>
      check: <a command that exits 0 when it is>
"""


def scaffold(repo: str = "") -> dict:
    """Write a starter GOAL.md, refusing to clobber an existing one."""
    path = goal_path(repo)
    if os.path.exists(path):
        return {"ok": False, "error": "%s already exists" % path}
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_TEMPLATE)
        return {"ok": True, "path": path}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


_USAGE = """usage: python goals.py <command> [--project NAME] [--repo DIR]

  init       write a starter GOAL.md
  sync       read GOAL.md into the store
  verify     run each milestone's check command and record the result
  export     write stored status back into GOAL.md's checkboxes
  show       print the stored goal and milestones
"""


def main(argv: list[str]) -> int:
    args = argv[1:]
    cmd = (args[0] if args else "").strip().lower()
    repo = os.getcwd()
    proj = ""
    i = 1
    while i < len(args):
        if args[i] == "--repo" and i + 1 < len(args):
            repo = args[i + 1]
            i += 2
        elif args[i] == "--project" and i + 1 < len(args):
            proj = args[i + 1]
            i += 2
        else:
            i += 1
    proj = proj or os.path.basename(repo.rstrip("\\/"))

    if cmd == "init":
        r = scaffold(repo)
        print(r.get("path") if r["ok"] else r["error"])
        return 0 if r["ok"] else 1

    if cmd == "sync":
        r = sync(proj, repo)
        if not r["ok"]:
            print(r["error"])
            return 1
        print("%s: %d milestone(s), %d added, %d updated"
              % (proj, r["milestones"], r["added"], r["updated"]))
        return 0

    if cmd == "verify":
        rows = verify(proj, repo)
        if not rows:
            print("no milestones for %s -- run `python goals.py sync` first" % proj)
            return 1
        for m in rows:
            print("[%s] %-44s %s" % (
                {"passed": "x", "failed": " ", "unverifiable": "?"}.get(m["result"], " "),
                m["title"][:44], m["result"]))
            if m.get("detail") and m["result"] == "failed":
                print("      %s" % m["detail"][:120])
        bad = sum(1 for m in rows if m["result"] == "failed")
        return 1 if bad else 0

    if cmd == "export":
        r = export(proj, repo)
        print(("%s: %d checkbox(es) updated" % (r["path"], r["changed"]))
              if r["ok"] else r["error"])
        return 0 if r["ok"] else 1

    if cmd == "show":
        b = store.brief(proj)
        if not b["goal"]:
            print("no goal stored for %s" % proj)
            return 1
        print("GOAL  %s" % b["goal"]["statement"])
        if b["goal"]["constraints"]:
            for c in b["goal"]["constraints"].splitlines():
                print("      - %s" % c)
        print("\n%d of %d milestone(s) done" % (b["done"], b["total"]))
        for m in b["milestones"]:
            mark = {"pending": " ", "active": "~", "done": "x", "blocked": "!"}
            print("  [%s] %s" % (mark.get(m["status"], " "), m["title"]))
            if m["evidence"]:
                print("      %s" % m["evidence"][:140])
        return 0

    print(_USAGE, end="")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv))
