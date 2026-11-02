# System Monitor Widget

A small always-on-top desktop panel showing live CPU, memory, GPU and fan
readings. Built for an MSI MS-1584 laptop (i5-12500H + RTX 3050) on Windows 11.

```
┌──────────────────────────────────┐
│ SYSTEM                         ● │
│ CPU    21%  ────────────    93°C │
│ RAM    87%  ████████▁▁   13.6/16G│
│ GPU     7%  ──────────  0.3G 54°C│
│ FAN  5032   ████████▔▔      5085 │
│ TODAY       7 · 1.3M tok · 38s   │
└──────────────────────────────────┘
```

It also keeps score. A line of today's agent activity sits on the tile, and
**Dashboard…** in the tray menu opens the full picture: every task with the
prompt it started from, the tokens it burnt, a team board of everyone's
laptops, and the tile's own settings as live controls. The same dashboard is
served as a web page on localhost when you want it on a second screen.

See [the dashboard](#the-dashboard) and [the team](#the-team).

## Running it

Double-click **`run.vbs`**. Accept the UAC prompt -- that elevation is what buys
the fan-speed and CPU-temperature readings (see below). The widget appears at
the top-right of the primary screen.

- **Drag** it anywhere, including onto another monitor. The position is
  remembered.
- **Clicks pass straight through it** by default, so it never gets in the way.
  Two spots stay live even then: the **green dot** at the top-left, which you
  grab to drag the tile (macOS-style, across monitors, even when locked), and
  the small **down-arrow button** on the RAM row, which frees memory in one
  click. For the full menu, use the **tray icon** (bar-chart glyph) or turn
  **Click-through** off.
- **Tray icon**: left- or right-click opens the menu; hovering shows the
  readings as a tooltip.
- **Resize** by dragging the bottom-right corner, or pick a preset from
  **Size** in the menu.
- The menu has Dashboard…, "Open web dashboard", Scorecard…, Free memory,
  "Auto-free when full", "Auto-free at", Size, update rate, opacity,
  background, fan readout, Click-through, "Pop to front on alert", "Show stats
  line", "Hide from screenshots", lock position, "Start with Windows", a
  **Team** submenu (serve the web dashboard, report to the team project, set up
  a team) and Quit.
- **`python dashboard.py`** opens the dashboard on its own, without the tile.
  **`python webserver.py`** serves the web one the same way.
- **Free memory in one click** with the down-arrow button on the RAM row (works
  with click-through on). Double-clicking the RAM row still works too.
- **`run.bat`** does the same thing but keeps a console open, for when you want
  to see a traceback.

Without elevation the widget still runs; CPU/RAM/GPU work as normal and the fan
row reads `no admin`.

### Start with Windows

The right-click toggle registers a Scheduled Task (`SystemMonitorWidget`) that
runs at logon with highest privileges. A task is used rather than a Startup
shortcut because it is the only way to auto-start an elevated process without a
UAC prompt at every logon.

## Where each number comes from

| Reading | Source | Notes |
| --- | --- | --- |
| CPU load | `psutil` | Windows performance counters |
| Memory used / total | `psutil` | "used" is total minus available |
| GPU load, VRAM, temp | `nvidia-smi` | ships with the NVIDIA driver |
| CPU temperature | MSI embedded controller | needs admin |
| Fan speeds (x2) | MSI embedded controller | needs admin |

### Why fan speed is the awkward one

Three approaches were tried on this machine:

1. **`nvidia-smi --query-gpu=fan.speed`** returns `[N/A]`. On a laptop the GPU
   fan is driven by the chassis embedded controller, not by the GPU itself.
2. **`Win32_Fan`** (WMI) returns three nameless "Cooling Device" entries with
   null `DesiredSpeed`. Standard ACPI exposes no tachometer here.
3. **LibreHardwareMonitor** needs the WinRing0 kernel driver for Super-I/O and
   EC access. Windows 11's vulnerable-driver blocklist refuses to load it
   (`CodeIntegrityPolicyEnforcementStatus = 2`), so every CPU temperature,
   clock and power sensor read back `None` and the motherboard node exposed no
   sensors at all.

What does work is MSI's own ACPI-WMI interface, which MSI Center uses:
`root\WMI:MSI_ACPI`. Its `Get_*` methods exchange a 32-byte `Package_32`, with
byte 0 selecting an index:

```
Get_EC            (no args) -> "1584EMS1.107 03182022 14:31:53"  (EC firmware)
Get_Temperature,  index 0   -> [status, cpu_temp_c, gpu_temp_c, ...]
Get_Fan,          index 0   -> [status, cpu_hi, cpu_lo, gpu_hi, gpu_lo, ...]
Get_Fan,          index 1/2 -> CPU / GPU fan curve duty points (%)
Get_Temperature,  index 1/2 -> CPU / GPU fan curve thresholds (°C)
```

The index-0 fan bytes are 16-bit big-endian. MSI encodes live fan speed as a
divisor, `rpm = 478000 / raw`, matching the Linux `msi-ec` driver for this
generation.

Only `Get_*` methods are ever called. The matching `Set_*` methods write EC
state -- fan curves, thermal profile -- and are deliberately never touched, so
the widget cannot change how your machine cools itself.

### Verifying the fan numbers

`rpm = 478000 / raw` is the documented MSI formula, but the same byte can be
read as a duty percentage. If the RPM figures look wrong against MSI Center's
own fan readout, switch **Fan readout → Duty %** in the right-click menu, or
set `"fan_mode": "duty"` in `config.json`.

The GPU temperature from this interface was checked against `nvidia-smi` and
matched exactly, which is what confirmed the byte layout.

## Background tint

`bg_alpha` is `190` by default -- a tinted card, still slightly translucent.
Three presets under **Background** in the right-click menu:

| Preset | Alpha | Looks like |
| --- | --- | --- |
| Transparent | 0 | No card; readings float on the wallpaper |
| Tinted | 190 | Dark slate card you can faintly see through (default) |
| Solid | 252 | Opaque panel |

Text carries a 1px shadow whenever `bg_alpha` is below 220, which is what keeps
the dim labels readable over a pale wallpaper. Fully transparent looks best over
a plain desktop; because the widget is always-on-top it often sits over an app
window instead, where small unbacked text gets hard to read.

## Click-through

With `click_through` on, the widget adds `WS_EX_TRANSPARENT` to its extended
window style, so Windows skips it during hit-testing -- clicks, scrolls and
hovers all land on whatever is underneath. Verified by hit-testing the centre of
the widget and getting back the window behind it.

This is done with `SetWindowLong` rather than Qt's `WindowTransparentForInput`
flag, because changing Qt window flags destroys and recreates the native window,
which would drop the capture-exclusion affinity set on it.

While it is on the widget is unclickable, so the tray icon carries the menu.

## Agent alerts

The widget doubles as a live board of every coding-agent session on the
machine: each session appears the moment a prompt is submitted to it, changes
colour when it finishes or gets stuck, and disappears when it closes. Several
projects show at once, and so do several sessions inside the same project.

```
NEEDS YOU (2)        Claude needs your permission...
CPU    34%  --------------------------  88 C
RAM    72%  ########################  11.3/16G
GPU    12%  --------------------------  54 C
FAN  3084   ########################      2862
B2Consultants  2 agents
  * #a3f1   Claude needs your permission...
  o #7c08                                2m
. memory_analyzer      tweak widget colours
```

Three states per session:

| State | Colour | Behaviour |
| --- | --- | --- |
| working | blue | listed quietly under a normal header; the note is the prompt it was given |
| needs you | amber, breathing | banner takes over the header; stays until dismissed or the turn ends |
| done | green | banner + tray balloon; expires after 10 minutes |

The amber banner and its status dot breathe on a 2.4-second cosine, and a new
alert rings the card for under three seconds and then fades out. Both are read
off the clock, so the speed is fixed rather than following whichever timer fired.

**Sessions are grouped by project.** A project running two or more agents gets a
header line with its name and a count, then one indented line per session,
labelled by a short session tag (`#a3f1`) and tied to the header by a faint
elbow. A project with a single session stays a single line. Grouping keys on the
*normalised path*, so a cwd spelled `B2-Consultants` in one hook firing and
`B2Consultants` in another still collapses into one group.

A project is ranked by its most urgent session, so a group containing a blocked
agent floats to the top; inside a group the same order applies. The widget grows
a line per display row up to seven, then adds "+N more". Balloons fire only for
"needs you" and "done" -- never for "working", which would pop one at every
prompt. A session already shown as blocked will not pop the tile again for
every follow-up notification in the same wait: re-announcing the same state is
held off for 30 seconds, so a run of permission prompts raises the tile once.
Restarting the widget adopts whatever is already in the mailbox silently,
rather than announcing alerts you have already seen.

### Clicking a session raises that agent

Clicking a row brings that session's own window to the front. Each hook records
which window its session runs in: a hook process is a descendant of the terminal
or editor it was launched from, so `notify.py` walks up the process tree and
takes the nearest ancestor that owns a visible top-level window. The handle is
stored per session and reused while that window lives, so **two agents in the
same project raise their own windows** rather than both resolving to the folder
name. Clicking a group header raises that project's most urgent session.

If the handle is missing or stale, it falls back to matching the project name
against window titles (ignoring case and punctuation, so `B2-Consultants` still
finds a title reading `B2Consultants`).

**The limit worth knowing:** two sessions in two *tabs of one window* share that
window, so clicking either raises the same window. Windows exposes no way to
focus an individual tab, so the tag tells them apart but cannot switch between
them. Run agents in separate windows if you want click-to-focus per agent.

Dismissing (tray left-click, the balloon, or **Agents** in the menu) clears the
finished and waiting entries but keeps running sessions listed -- they describe
reality, not something to acknowledge. A specific session picked from the
**Agents** menu is removed outright.

### How the signal arrives

Each session drops a small JSON file in
`%LOCALAPPDATA%\SystemMonitorWidget\alerts\` and the widget polls the directory
once a second. Files are used rather than a port because hooks are short-lived
processes that may run elevated or not, from any directory, with nothing
listening.

**One file per session**, keyed by working directory (basename plus a short
digest of the full path) plus the session id. That is what lets two sessions of
the same project hold separate rows instead of overwriting each other. Expired
files are reaped as they are read: `done` after 10 minutes, `working` after 3
hours (a safety net for sessions that crashed without a SessionEnd), so the
directory does not grow without bound.

`notify.py` writes the files:

```
python notify.py prompt      ["what the user asked"]
python notify.py working      # resume after a block (PreToolUse)
python notify.py needs-input ["why it stopped"]
python notify.py done        ["what it finished"]
python notify.py end          # session closed, remove its row
python notify.py clear        # dismiss every entry
python notify.py clear .      # dismiss this project's entries only
```

With no message argument it reads the hook payload from stdin and pulls out a
sensible line (the permission message, or the prompt). The session id and
working directory come from the payload too. It always exits 0 and never writes
to stderr -- a hook that fails loudly would disrupt the agent it reports on.

### Wiring it to Claude Code

Five global hooks in `~/.claude/settings.json`, all `async` so they never delay
a turn, covering every project with no per-project setup:

| Hook event | Fires when | notify.py action |
| --- | --- | --- |
| `UserPromptSubmit` | a prompt is sent to the agent | `prompt` |
| `PreToolUse` | the agent is about to use a tool | `working` |
| `Notification` | the agent wants a permission or an answer | `needs-input` |
| `Stop` | the agent finishes a turn | `done` |
| `SessionEnd` | the session closes | `end` |

`Notification` covers two unrelated things: a real question or permission
prompt, and an idle reminder that fires a minute or so after a turn has already
ended. Only the first is someone being blocked, so the idle wording is ignored
when the session's last state is a finished turn -- otherwise a green "task
done" row turned amber on its own, with nothing waiting behind it. Mid-turn the
same wording is a genuine block and still shows as one.

`PreToolUse` fires before every tool call, so `notify.py working` skips the
write when the session is already working and fresh (under 8 seconds). Its real
job is the blocked→resumed transition: the first tool after you answer a prompt
flips the amber `needs you` row back to blue `working` straight away, rather than
waiting for the turn to end. (It also records when you unblocked the agent, which
is how the Scorecard measures your reply time.)

Run `/hooks` in Claude Code to review or disable them. A session that was
already running when the hooks were added appears after its next event -- send
it a message and it shows up as working. Any other agent or script can join the
board the same way by running `notify.py`.

## Agent productivity (the Scorecard)

The same hooks that drive the live board also keep a running log, so the widget
can answer the questions you actually care about when agents do the work: how
responsive are you to them, how much is finishing, and what it costs.

The tile shows a one-line **TODAY** summary -- tasks finished, tokens used,
median reply time -- and **Scorecard…** in the menu opens a window with the full
picture for today, the last 7 days, or the last 30, broken down per project:

| Metric | What it means |
| --- | --- |
| Tasks finished | Turns the agent completed (one `Stop` each). |
| Median / slowest reply | How long an agent sat waiting on you before you answered. |
| Median task time | Prompt → finish for a completed turn. |
| Tokens used | Total read from the session transcript. |
| Tool-calls / task | A rough proxy for how much work a task took. |
| Waiting / working now | Live, from the alert board. |

### How it is measured, cheaply

Each hook appends one small JSON line to
`%LOCALAPPDATA%\SystemMonitorWidget\events.jsonl` (`events.py`). Reply time comes
from the gap between a `wait` and your next `prompt` (or the `resume` the
`PreToolUse` hook records when you unblock the agent); task time from `prompt`
to `done`; both are paired per session so day boundaries stay honest.

Token and tool counts are **not** logged per tool call -- that would churn the
disk dozens of times a turn on an already-hot laptop. Instead they are read from
the transcript Claude Code already writes (`transcript_path` in the hook
payload) **once per turn, on `Stop`**. A per-session byte offset in
`offsets.json` means each read parses only the lines added since last time, so a
long session costs no more per turn than a short one.

`stats.py` aggregates the log. The tile shows **token counts only**; the
dashboard also prices them with the `PRICING` table in `stats.py` and labels the
result an estimate, because that table is a list of rates someone typed, not a
bill. The Scorecard mirrors the tile's "Hide from screenshots" setting, so the
board stays out of shared screens when the tile does.

Turn the tile line off with **Show stats line** in the menu; the Scorecard stays
available regardless.

### Only logging a resume that resumes something

`PreToolUse` fires before every tool call, and the `working` action used to log
a `resume` event whenever the session's mailbox row was more than eight seconds
old. Any tool call slower than that -- a build, a test run, a subagent --
slipped past the check, so the log filled with resumes that resumed nothing:
**4,606 of 5,279 events, against 62 actual waits.** 87% of the file, and at most
62 of those rows ever carried the signal they existed for.

Nothing read them. A `resume` is only there to pair with a `wait`, and
`stats._pairings` discards one that pairs with nothing. So the event is now
written only when a block is genuinely open, while the mailbox write is decided
separately on its own freshness rule -- the tile stays current, the log stops
churning. Reply-time measurement is unchanged, because the one resume that
matters is the one that still gets logged.

It was affordable while the log only fed counters. It stopped being affordable
when the log became the substrate for the store below.

## The store

`events.jsonl` is still the thing hooks write: one short line, appended, then
the process exits. That contract is why a hook costs milliseconds and why a
laptop on a train cannot stall the agent it measures, and `store.py` does not
touch it.

What `store.py` adds is a *reader*. It drains the log into SQLite from a
watermark, so a query stops costing a full-file parse:

```
python store.py ingest          # drain the log into the database
python store.py stats           # what is in there
python store.py search QUERY    # full-text over observations
python store.py brief [PROJECT] # the orientation blob for a session
```

The database sits beside the log, at
`%LOCALAPPDATA%\SystemMonitorWidget\store.db`. It needs no new dependency: the
stdlib `sqlite3` already ships FTS5.

**Re-ingesting is free**, which is the property everything else leans on. The
byte offset of each line is the primary key of `events`, so reading from an
earlier watermark lands the same rows on the same keys rather than
double-counting a turn -- the same bargain `sync.py` strikes upstream with
`(device, task_key)`. A log that shrank is taken as rotated and read again from
the top, and a torn final line is left behind the watermark for next time.

Three tables exist for what comes next rather than what works today:

| Table | Holds |
| --- | --- |
| `observations` | One row per finished turn: what it *did* -- files read and written, commands run, the prompt, a title and narrative. The log answers "how much"; this answers "what". |
| `goals` / `milestones` | Where a project is *going*. A human writes the goal; an agent maintains the decomposition. |
| `decisions` | Settled choices and their rationale, so they stop being re-litigated every session. |

`observations` carries a `source` column -- `extract`, `llm` or `manual` --
because the cheap deterministic tier and an opt-in summarisation tier should
land in the same table and let retrieval ignore which produced a row.

`milestones.check_cmd` is the load-bearing column: a milestone whose completion
can be *asserted* by running a command is one an unattended run cannot talk
itself into having finished.

## What a turn did

`events.jsonl` records that a turn happened and what it cost. `observe.py`
records what it *did*: the files it read, the files it changed, the commands it
ran, whether the tests passed, and how the agent described its own work.

It costs nothing. Everything is deterministic parsing of the transcript Claude
Code already writes, so no model is called, no tokens are spent and no socket is
opened -- which is what makes it safe to run from a hook on every turn. The read
is incremental from a byte offset per session, in the same style as
`events.tokens_since`, so a turn in a long session costs no more than a turn in
a short one.

Subagent lines (`isSidechain`) are skipped. A subagent's tool calls are means,
not outcome, and folding them in buries the turn's actual result under a dozen
searches someone else performed.

Test outcomes are read from the runner's own summary in the tool result --
pytest, jest, vitest, cargo, go, unittest and the rest. When a test ran but
printed something unrecognised, the row says `ran, outcome unparsed` rather than
staying silent, because silence would read as "no tests", which is a different
and more dangerous claim.

## Where a project is going

The measurements say something uncomfortable. Over nine days, across 15
projects: **351 prompts against 219 finished turns, and agents blocked on a
human only 62 times.** Four turns in five never interrupt anyone, and the total
blocked time was three hours.

So attention during a turn is not the scarce resource. *Intent at the start of
one* is -- 39 prompts a day, most of them saying what to do next. Batching
permission prompts would optimise the fifth of the problem that is not the
problem.

`GOAL.md`, committed in the repository, is the fix. Three roles, deliberately
separated:

```
python goals.py init      # write a starter GOAL.md
python goals.py sync      # read it into the store
python goals.py verify    # run each milestone's check and record what it proved
python goals.py export    # write status back into the checkboxes
python goals.py show      # print the board
```

- **`sync` owns structure.** The file is authoritative for the goal statement,
  the milestone titles, their order, their acceptance criteria and their checks.
- **`verify` owns status**, and is the only thing that may mark a milestone
  done. It runs the check command and stores what happened as evidence. A
  milestone with no check reports `unverifiable` rather than being quietly
  called finished, and a check that starts failing *reopens* a done milestone,
  so a regression comes back instead of staying ticked.
- **`export` closes the loop**, putting status back where a human and a diff can
  see it.

`verify` runs commands out of a file in your repository, so it is a CLI command
you invoke. Hooks stay read-only and never run a check.

### Who wins when the file and the database disagree

They disagree in exactly one interesting way: the file says `[ ]` and the
database says done. Either a human just unticked the box to reopen the work, or
`verify` proved it finished and the file is merely stale -- and the right
response is opposite in the two cases.

So `milestones.file_status` remembers what the checkbox said at the last sync. A
*change* in the file is a human editing it, and wins. An unchanged file leaves
the verified status alone. Without that column there is no way to tell the two
apart, and whichever rule you pick is wrong half the time.

## The orientation a session starts with

A sixth hook, `SessionStart`, prints what `context.py` renders: the goal, the
constraints, the live milestone and what is next, anything already settled, and
the last few turns with the files they touched.

```
GOAL  Grow this from a machine-telemetry tile into an agent control plane...
      constraint: No new runtime dependencies.
      constraint: Hooks never touch the network and never block a turn.

MILESTONES  5 of 11 done
  [ ] Memory tab in the dashboard
        acceptance: observations are searchable from both dashboards

RECENT WORK
  - stop logging resumes that resume nothing [2h ago]
      changed: notify.py, README.md
      tests: 93 passed
```

Finished milestones are a count, not a list -- done work is history, and what
matters is the live one and the next one.

It is **small on purpose**, inside a 4,000-character budget, because this is
spent on every single session start and a wall of stale context crowds out what
the agent is about to read for itself. Trimming is line-wise from the end, so
the goal survives a tight budget and the oldest turn summaries are what fall
off. Detail is pulled on demand with `store.py search` instead.

Two rules it must not break. It **never fails**, because a traceback from a hook
lands in the session's context. And it **never assumes the console is UTF-8**:
on Windows the default is cp1252, real prompts contain real characters, and
printing a rupee sign through an unconfigured stdout raises
`UnicodeEncodeError` -- which is exactly how a hook that worked in testing
breaks on a Tuesday.

This hook is the one exception to "every hook is async". An async hook's stdout
is nobody's input, so it has to be synchronous to be read at all.

### Naming a session after its repository, not its subdirectory

An agent does not stay in the directory it started in. A session working inside
`Voice_Transcriber/platform` used to appear on the board as **`platform`**,
because the row was labelled and keyed by `basename(cwd)` -- so the same agent
showed up under whichever subdirectory it happened to be standing in, and moving
between subdirectories split one session across two rows, each going stale in
turn.

`agent_alert.project_root` walks up to the nearest `.git` (or `.hg`, or `.svn`)
and the key is built from that, so one session keeps one row under the name its
human knows it by. A nested checkout still wins over its parent, because the
nearest marker is found first -- that inner repository really is the project.

## Tests

```
python -m unittest discover -s tests -t .
```

Ninety-three of them, and no test dependency: they are plain `unittest` cases,
so they run under `pytest` too if you prefer its output. Each one gets a
throwaway `LOCALAPPDATA` via `tests/_env.Sandbox`, so a test run never touches
the real event log or database.

They are also what this project's own milestone checks execute, which is the
point: `python goals.py verify` passes only because the suite does.

## The Memory tab

**Dashboard… → Memory**, and the same view in the web dashboard, make the
observations searchable: a table of what past turns did, and a pane under it
with the full narrative, every file and every command.

```
┌─ Memory ──────────────────────────────────────────────────────────────┐
│ [search files, commands, decisions…]  [All projects▾] [Any kind▾] 6/6 │
├───────────────────────────────────────────────────────────────────────┤
│ 02 Nov 13:22  bugfix   memory_analyzer  fix the fan divisor           │
│                                         msi_ec.py          7 passed   │
│ 02 Nov 12:40  feature  rdinterlocks     add the quotation flow        │
└───────────────────────────────────────────────────────────────────────┘
```

The period and scope selectors above deliberately do not apply. Memory gets
asked "when did I last touch the fan code", and the answer is whenever it was;
clamping it to the last 7 days would hide the row being looked for. The search
box is the filter.

A failing test run is drawn in the amber that already means "this needs you" on
the tile. The palette has no red in it on purpose, so a failure borrows the
colour the board already uses for attention rather than introducing a ninth hue.

### Why a row is smaller than the thing it describes

Both dashboards read one function, `store.observations_page`, for the same
reason the Tasks view already works that way: two renderers over one query
cannot quietly disagree about what a row means.

That query returns *less* than it has. A turn can touch sixty files and run
sixty commands, and at 300 characters a command that was **15 KB of JSON per
row** -- measured at 91 KB for six rows, so a hundred would have been a
megabyte and a half down a phone tunnel. The table draws no commands at all, so
a page row carries none of them; the narrative travels as a 200-character
preview; the file lists stop at twelve. What does travel is the *untrimmed
count* of each, which is what lets the detail pane say "and 28 more" honestly.

Selecting a row then fetches that one observation whole, from `store` directly
in Qt and from `/api/memory?id=` on the web. A page row is now **2.4 KB instead
of 15 KB**, and the detail is complete when someone is actually reading it.

## The dashboard

**Dashboard…** in the menu opens the full window. The Scorecard is still there
as a quick read-only glance; this is the one you work in.

```
┌─ Agent Dashboard ─────────────────────────────────────────────┐
│ [Today][7 days][30 days]   [This laptop][Team]   Sync  Refresh│
│ Overview │ Tasks │ Team │ Tile │ Deploy                       │
├───────────────────────────────────────────────────────────────┤
│   8        247.3M      $614.10     5m      2.1m      127      │
│  tasks     tokens       cost     median   reply    tools/task │
│                                                               │
│  TOKENS PER DAY                                        250M   │
│  ▁▁▁▁▁▃█▆                                                     │
│                                                               │
│  TOKENS BY MODEL          TOKENS BY PROJECT                   │
│    ◕ 247M  ■ opus 100%    memory_analyzer ████████████ 214.0M │
│                           voice assistant █              17.9M│
└───────────────────────────────────────────────────────────────┘
```

Six tabs:

| Tab | What it is for |
| --- | --- |
| **Overview** | Headline figures, the daily token trend, the split by model, the ranking by project, and what hour of the day the work happens. |
| **Memory** | What past turns did -- files, commands, test outcomes -- searchable. |
| **Tasks** | Every task as a row -- the prompt it started from, how long it took, how long it waited on you, what it burnt. Sortable, filterable, exports to CSV. |
| **Team** | One card per laptop: online or not, who is working or blocked right now, and how hot their machine is running. |
| **Tile** | The tile's own settings, as live controls. |
| **Deploy** | Enrolling a team, signing in to read the board, and building the installer for a teammate. |

Two selectors sit above all of it: the period (today / 7 / 30 days) and the
scope (this laptop / the team).

### Everything is one task record

`stats.task_rows()` turns the event log into one record per finished turn, and
`sync` turns a teammate's synced rows into records of the same shape.
`summarize_tasks()` runs over either. So the team view is not a second
implementation of the overview -- it is the same aggregation fed a different
list, which is what keeps the two from quietly disagreeing.

A turn that carried tokens but has no matching prompt still becomes a row, with
an empty task name. Those come from sessions that were already running when the
hooks were installed. They are kept rather than dropped so the token totals
stay whole: the dashboard's "tokens used" matches the Scorecard's exactly.

### The prompt text

`UserPromptSubmit` now logs the prompt line as well as the fact of it, clipped
to 240 characters. That is what fills the Tasks tab's "Task" column. Turns
recorded before this change show "no prompt recorded" rather than an empty cell.

### Customising the tile from the dashboard

The Tile tab is not a copy of `config.json`. Each control calls
`MonitorWidget.apply_setting`, a dispatcher that routes to the same setter the
tray menu uses, so a change lands on screen as you drag the slider and cannot
drift from what the menu would have done. There is one place to add a setting
and one place for it to go wrong.

Opened standalone (`python dashboard.py`) with no tile running, the tab writes
`config.json` instead and says so at the top rather than pretending to be live.

### The web dashboard

**Open web dashboard** serves the same data as a web page from inside the
widget's own process, at `http://127.0.0.1:8765/`. Useful on a second screen or
a phone, and it means the tile controls work from a browser.

Running in-process is what makes that last part possible: the request handler
emits a Qt signal and the tile's own `apply_setting` runs on the UI thread. A
separate server would have to shout at the tile across a process boundary.

Localhost needs no authentication -- reaching it already means running code on
this machine. Binding wider is opt-in via `web_bind` in `config.json`, and then
a token is generated and required on every request, because the page shows
prompt text and can resize someone's tile.

It is dependency-free: `http.server` and hand-written SVG, no framework and no
CDN. A local tool should not stop working because the laptop is offline, which
is often exactly when you want to look at it.

## The team

Each laptop reports to a Supabase project; the dashboard reads it back. Set it
up on the **Deploy** tab.

### Two credentials, on purpose

The data includes prompt text, so reading and writing are separated:

| Role | Credential | Can |
| --- | --- | --- |
| **Reporter** | the project's anon key, on every laptop | `INSERT` only |
| **Viewer** | a Supabase Auth sign-in, on the dashboard | `SELECT`, own team only |

There is no way to give a desktop agent a secret it can keep, so the key every
laptop carries is assumed to leak. The RLS policies in `schema.sql` make that
survivable: a lifted key can add rows, and cannot read one back. Seeing the
board is a privilege granted per person by inserting a row in `team_members`.

Run `schema.sql` once in the Supabase SQL editor before enrolling anyone. The
Deploy tab has a button that copies it.

### What goes up, and how

Hooks never touch the network. They write locally and exit, which is what keeps
a hook at a few milliseconds and keeps a laptop on a train from stalling the
agent it is measuring. `sync.py` is the part that goes online -- a background
thread inside the widget, or a scheduled task on a reporter-only install.

| Table | Shape | Why |
| --- | --- | --- |
| `tasks` | append-only, keyed `(device, task_key)` | Inserted with duplicates ignored, so the watermark can overlap and a retry after a half-delivered batch cannot double-count a turn. |
| `live_sessions` | upserted presence | Only the latest state matters. |
| `health` | upserted, one row per laptop | Same. |
| `devices` | upserted; doubles as the heartbeat | A laptop that reports nothing else still shows as present. |

Reporters never *delete* their presence rows -- that would mean granting
`DELETE` to the anon key, which would let anyone with a laptop wipe the board.
Rows are judged stale by age instead, and `prune_telemetry()` in `schema.sql`
clears them out on a schedule.

Each stream has its own switch, and prompt text has a switch of its own even
when tasks are shared. A laptop with no `team.json` never opens a socket.

### Installing on a teammate's laptop

The Deploy tab builds either a one-line command or a self-contained bundle
(agent files, their `team.json` already filled in, and the installer).

Two shapes, because asking a colleague to accept an always-on-top window is a
different request from asking them to let a hook count tokens:

| Mode | Installs | Needs |
| --- | --- | --- |
| **reporter** (default) | hooks and the sync task. Nothing on screen. | Python, `psutil` |
| **full** | the tile and their own dashboard as well | Python, PySide6, psutil, pywin32 |

`install.ps1` copies the files, pip-installs, writes `team.json`, registers the
five hooks and schedules the upload. It needs **no administrator rights** in
reporter mode -- elevation on this project only ever buys fan speed and CPU
temperature from the MSI embedded controller, which a reporter has no use for,
and asking for admin to install telemetry is a good way to be told no.

`hooks.py` does the hook registration programmatically: it merges into
`settings.json` rather than overwriting it, replaces its own entries rather than
appending duplicates, leaves anyone else's hooks alone and running first, and
backs the file up before writing.

To stop reporting, delete `%LOCALAPPDATA%\SystemMonitorWidget\team.json`. It is
re-read on every sync, so there is no uninstall to wait for.
`install.ps1 -Uninstall` removes the hooks and the scheduled task too.

### Charts

Series colour does **not** reuse the tile's accents. Those four are tuned to
glow on a dark tile, which puts them outside the lightness band, and blue
against purple comes out at a deuteranopia Delta-E of 7.0 -- close enough that
two series beside each other in a legend stop being distinguishable. They are
kept for the machine-health meters, where each sits on its own labelled row and
is never asked to be told apart from its neighbour.

Charts use a separate eight-hue order, validated against this dashboard's actual
surface (`#0e1119`): worst adjacent CVD Delta-E 8.4, worst normal-vision 19.3.
It lives in `viz.py`, which also generates the web page's CSS custom properties,
so there is one copy of the palette rather than one per front end.

## Freeing memory

**Free memory** -- the down-arrow button on the RAM row (one click, works
through click-through), the right-click menu, or a double-click on the RAM row
-- calls `EmptyWorkingSet` on every process it is allowed to touch. That asks
Windows to push those processes' resident pages out of physical RAM. The result
appears briefly in the header, e.g. `freed 1.3 GB`.

**Nothing is ever terminated.** Processes keep running and fault their pages
back in on demand. Skipped: PIDs 0-4, the widget itself, the foreground window's
process (so your active app stays snappy), and a short list of critical system
processes (`csrss`, `lsass`, `services`, `dwm`, ...).

The honest trade-off: pages that go out have to come back. An app you return to
afterwards may stutter briefly while it reloads from the pagefile. It is worth
doing when something has ballooned and you want headroom now; it is not
something to run on a timer, and Windows reclaims memory by itself under real
pressure.

Measured on this machine: 95.1% used with 0.77 GB available, down to 86.5% with
2.12 GB available -- 1.3 GB reclaimed in about a second, 235 processes trimmed.

### Auto-free when full

**Auto-free when full** (on by default) trims on its own once memory passes
either limit: **90% of total, or 15 GB in use**, whichever comes first. Pick the
percentage under **Auto-free at** in the menu; both limits live in `config.json`
as `auto_trim_pct` and `auto_trim_gb`, and `auto_trim` turns the whole thing
off. An unattended trim labels itself in the header, e.g. `auto freed 1.3 GB`.

It fires on *crossing* a limit, not on a timer, and then holds off for two
minutes (`AUTO_TRIM_COOLDOWN`). That cooldown is the important part: the pages a
trim pushes out fault straight back in, so without it the widget would trim on
every sample above the threshold and thrash the pagefile.

Which is the honest tension with the advice above -- trimming is not something
to run continuously. At 90% you are nearly out of headroom anyway, so one trim
and a wait is a reasonable trade; if you notice apps stuttering after it fires,
raise the threshold to 95% or switch it off.

Deliberately not done: purging the standby list. That list is clean cache which
Windows already counts as *available*, so purging it would not move the number
this widget shows -- it would only discard cache that then has to be re-read
from disk.

## Resizing

The tile scales as a whole rather than reflowing: everything is laid out in
fixed logical pixels and the painter applies a scale factor, so the text and
sparklines stay sharp and proportional at any size.

- **Drag the bottom-right corner** (three diagonal ticks). The top-left stays
  anchored and the width you drag out sets the scale; height follows.
- Or pick **Size → Small / Normal / Large / Huge** in the menu.

The scale is clamped to 0.6x-2.5x (180x120 up to 750x500) and saved as `scale`
in `config.json`. Mouse hit-testing divides by the scale to map back into layout
coordinates, so the grip, the free-RAM button and the session rows stay
clickable at every size.

## Hiding from screenshots

**Hide from screenshots** (on by default) calls
`SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)`. Windows then leaves
the widget out of anything that captures the screen -- Print Screen, Snipping
Tool, OBS, and Teams or Zoom screen shares -- while it stays fully visible on
the physical display. Verified here: with the flag set, a screen capture of the
widget's exact rectangle contained none of its pixels.

This needs Windows 10 version 2004 or newer. On older builds the call fails and
the toggle has no effect rather than blanking the window.

## Files

| File | Purpose |
| --- | --- |
| `widget.py` | Qt UI, painting, drag, menu, config, scheduled task |
| `sensors.py` | Merges psutil, nvidia-smi and the EC into one `Sample` |
| `msi_ec.py` | The MSI ACPI-WMI interface |
| `memory.py` | Working-set trimming for the Free memory action |
| `agent_alert.py` | The per-project alert mailbox agents write to |
| `events.py` | Append-only activity log; reads token usage from transcripts |
| `store.py` | The indexed store: drains the log into SQLite, holds memory and goals |
| `observe.py` | Turns a finished turn's transcript into an observation |
| `goals.py` | `GOAL.md` in, milestones out; verifies them and writes status back |
| `context.py` | The orientation block a session starts with |
| `GOAL.md` | This project's own end goal and milestone board |
| `tests/` | The suite the milestone checks run |
| `stats.py` | Aggregates the log into metrics, task records and pricing |
| `notify.py` | CLI entry point agent hooks call |
| `dashboard.py` | The Qt dashboard: overview, tasks, team, tile, deploy |
| `charts.py` | Hand-painted Qt chart widgets |
| `viz.py` | The palette and formatters both dashboards share |
| `webserver.py` | Local HTTP server and JSON API for the web dashboard |
| `web/` | The web dashboard: `index.html`, `style.css`, `app.js` |
| `identity.py` | Device identity and team enrollment (`team.json`) |
| `cloud.py` | Supabase REST transport, stdlib only |
| `sync.py` | Pushes this laptop's activity up, pulls the team board down |
| `hooks.py` | Installs and removes the five Claude Code hooks |
| `deploy.py` | Builds the install bundle for a teammate |
| `install/install.ps1` | The installer that runs on a teammate's laptop |
| `schema.sql` | Supabase tables, RLS policies and retention |
| `run.vbs` | Elevated, no-console launcher |
| `run.bat` | Elevated launcher that keeps a console for debugging |
| `config.json` | Written at runtime; position and preferences |

## config.json

| Key | Meaning |
| --- | --- |
| `x`, `y` | Last position. Ignored if it no longer lands on an attached screen. |
| `interval_ms` | Poll interval for CPU and memory. |
| `slow_every` | Poll nvidia-smi and the EC every Nth tick. |
| `opacity` | Window opacity, 0.0-1.0. |
| `bg_alpha` | Card background alpha, 0-255. `0` transparent, `190` tinted, `252` solid. |
| `hide_from_capture` | `true` keeps the widget out of screenshots. |
| `click_through` | `true` lets clicks pass to the window underneath. |
| `pop_on_alert` | `true` raises and flashes the widget when a session finishes or needs you. |
| `show_stats` | `true` shows the one-line TODAY summary on the tile. |
| `fan_mode` | `rpm` or `duty`. |
| `fan_max_rpm` | Full scale for the fan sparkline. |
| `scale` | Tile size, 0.6-2.5. Set by the corner grip or the Size menu. |
| `locked` | `true` disables dragging by the body. The green grip still works. |
| `auto_trim` | `true` frees memory on its own past the limits below. |
| `auto_trim_pct` | Percent of total memory that triggers an automatic trim. |
| `auto_trim_gb` | Gigabytes in use that triggers one, whichever comes first. |
| `web_enabled` | `true` serves the web dashboard from the widget's process. |
| `web_port` | Port to listen on; the next free one is taken if it is busy. |
| `web_bind` | `127.0.0.1` by default. Anything wider forces a token. |
| `web_token` | Generated when `web_bind` is not localhost. Required on every request. |
| `sync_enabled` | `true` uploads to the team project, when `team.json` says to. |
| `sync_seconds` | How often to upload. |

Team settings live separately, in
`%LOCALAPPDATA%\SystemMonitorWidget\team.json`, so that deleting one file stops
all reporting without touching how the tile looks:

| Key | Meaning |
| --- | --- |
| `enabled` | `false` or a missing file means nothing leaves the laptop. |
| `url`, `anon_key` | The Supabase project and its write-only key. |
| `team_id` | Which team's board this laptop reports to. |
| `member` | How it appears on the board. Blank uses the Windows user name. |
| `share_tasks` | Finished tasks and token counts. |
| `share_prompts` | The text of each prompt. Separate from `share_tasks` on purpose. |
| `share_sessions` | Live working / blocked / done states. |
| `share_health` | CPU, RAM, GPU and temperatures. |

## Setup from scratch

```
pip install -r requirements.txt
```

Python 3.13 at `C:\Python313`. If Python lives elsewhere, edit the `pythonw`
path in `run.vbs`.
