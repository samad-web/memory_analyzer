Sirahlyzer -- team build
=========================

Everything is bundled. You do NOT need to install Python, pip, .NET, Visual
C++ redistributable, or anything else. Just extract the zip and run.

Extract to a NEW, EMPTY folder (for example C:\Sirahlyzer). Do not extract
over an older Sirahlyzer / Memory Analyzer folder -- see "Updating" below.


What this program is, and what it touches
-----------------------------------------
Sirahlyzer is Sirah Digital's internal build of an app that was developed
under the working name "System Monitor Widget". Two internal names from
that period remain ON PURPOSE, so you can recognise them when auditing:

  %LOCALAPPDATA%\SystemMonitorWidget\   data folder: event log, SirahOS
                                        sign-in (encrypted for your Windows
                                        user), and  sirahlyzer.log
  "SystemMonitorWidget.singleton"       the lock that stops two copies running

They are kept because the Claude Code hooks, the auto-push task and existing
installs all read and write that folder; renaming it would split a running
install's history in two. Everything else is named Sirahlyzer.

Files it creates:
  config.json (beside Sirahlyzer.exe)   settings; written on first launch
  %LOCALAPPDATA%\SystemMonitorWidget\   data (see above)
  %USERPROFILE%\.claude\settings.json   six hook entries (setup.cmd adds them;
                                        a timestamped backup is kept)
  Scheduled task "SirahOSBridge"        runs SirahlyzerBridge.exe push
Network: the dashboard listens on 127.0.0.1 only (not reachable from other
machines). The app's outbound traffic is HTTPS to api.os.sirahagents.com,
plus team reporting only if an admin enrolls this laptop (team.json). The
dashboard page itself loads fonts from fonts.googleapis.com. Your SirahOS
password is sent once at sign-in and never stored.


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
   Right-click the island -> "Open web dashboard". Your browser opens the
   dashboard (normally http://127.0.0.1:8765; if that port is busy it uses
   8766-8770 -- the menu item always opens the right one).
   -> SirahOS tab -> sign in with your SirahOS email + password.
   If it can't start, a message says why. Details: sirahlyzer.log in
   %LOCALAPPDATA%\SystemMonitorWidget\

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
%LOCALAPPDATA%\SystemMonitorWidget\ (see "What this program is" above).
Current version: see VERSION.txt.

Troubleshooting:  SirahlyzerBridge.exe status | more
prints the config path, whether the dashboard is enabled, its port, and
your SirahOS sign-in state.


Uninstall
---------
    SirahlyzerBridge.exe hooks-uninstall
    schtasks /Delete /TN SirahOSBridge /F
Then delete this folder and  %LOCALAPPDATA%\SystemMonitorWidget\
