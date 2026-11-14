Sirahlyzer -- team build
=========================

Everything is bundled. You do NOT need to install Python, pip, .NET, Visual
C++ redistributable, or anything else. Just extract the zip and run.

Extract to a NEW, EMPTY folder (for example C:\Sirahlyzer). Do not extract
over an older Sirahlyzer / Memory Analyzer folder -- see "Updating" below.


First-time setup
----------------

1. Double-click  setup.cmd
   - connects Claude Code to Sirahlyzer (so your agents show up)
   - registers the 15-minute SirahOS auto-push task
   Then CLOSE and reopen any Claude Code windows you had open.

2. Double-click  Sirahlyzer.exe
   A Dynamic Island appears at the top-centre of your screen. Click it to
   drop down live agents and stats. If SmartScreen warns:
   "More info" -> "Run anyway" (one time).

3. Sign in to SirahOS
   Right-click the island -> "Open dashboard" (or http://127.0.0.1:8765)
   -> SirahOS tab -> sign in with your SirahOS email + password.

4. Map folder -> project (optional)
   On the SirahOS tab, map each repo folder to its SirahOS project.
   Unmapped folders land under "General".

Check it works: start any Claude Code session and send a prompt -- the
island shows the agent within a second or two.


Not seeing the island? (old flat tile instead)
-----------------------------------------------
Quit it (right-click -> Quit), delete  config.json  in this folder, and
start Sirahlyzer.exe again. An old config.json from a previous version
keeps the old look. Prefer the flat tile? Set "island": false in config.json.


Updating to a new version (no reinstall)
----------------------------------------
1. Right-click the island -> Quit.
2. Extract the new zip OVER this folder, overwriting when prompted.
3. Double-click setup.cmd again (safe; refreshes the hooks).
4. Start Sirahlyzer.exe.

Sign-in, event log and the task survive -- they live in
%LOCALAPPDATA%\SystemMonitorWidget\  (old internal name, kept on purpose).
Current version: see VERSION.txt.


Uninstall
---------
    SirahlyzerBridge.exe hooks-uninstall
    schtasks /Delete /TN SirahOSBridge /F
Then delete this folder and  %LOCALAPPDATA%\SystemMonitorWidget\
