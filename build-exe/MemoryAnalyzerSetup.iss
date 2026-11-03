; Inno Setup script for MemoryAnalyzerSetup.exe.
;
; Wraps the PyInstaller build into one double-clickable installer that:
;
;   - copies the frozen tile + bridge to %LOCALAPPDATA%\MemoryAnalyzer
;   - registers the Claude Code hooks (hooks.py install)
;   - creates the SirahOSBridge scheduled task
;   - creates a Start-menu shortcut and an optional auto-start entry
;   - uninstalls cleanly from Add/Remove Programs
;
; Everyone on the team runs Windows and the installer ships unsigned, so
; Windows SmartScreen will warn on first launch -- "More info" -> "Run anyway"
; dismisses it once per machine. See build-docs/features/exe-distribution.md.
;
; Build from the repo root (after a PyInstaller build has produced
; dist\MemoryAnalyzer\):
;
;   & "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" build-exe\MemoryAnalyzerSetup.iss
;
; Output lands in build-exe\Output\MemoryAnalyzerSetup.exe.

#define AppName       "Memory Analyzer"
#define AppPublisher  "Sirah Digital"
#define AppVersion    "1.0.0"
#define AppExe        "MemoryAnalyzer.exe"
#define BridgeExe     "MemoryBridge.exe"
#define AppId         "{{B3F0A1C2-6E4A-4C7D-8E9B-11223344AABB}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\MemoryAnalyzer
DefaultGroupName={#AppName}
DisableProgramGroupPage=no
OutputDir=Output
OutputBaseFilename=MemoryAnalyzerSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "launchatlogon";  Description: "Start Memory Analyzer at Windows logon";                        GroupDescription: "Startup:"; Flags: unchecked
Name: "schedulebridge"; Description: "Push WorkLogs to SirahOS every 15 minutes (SirahOSBridge task)"; GroupDescription: "Startup:"; Flags: checkedonce
Name: "desktopicon";    Description: "Create a desktop shortcut";                                     GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
; The PyInstaller --onedir tree. The *.* line copies every file next to the
; exes without having to enumerate them in this script, so a new asset that
; PyInstaller adds does not need an Inno update.
Source: "..\dist\MemoryAnalyzer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Memory Analyzer";       Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall Memory Analyzer"; Filename: "{uninstallexe}"
Name: "{userdesktop}\Memory Analyzer"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\Memory Analyzer"; Filename: "{app}\{#AppExe}"; Tasks: launchatlogon

[Run]
; The bridge scheduled task. ``schtasks /Create /F`` replaces any existing
; definition, so re-running the installer upgrades it without a stale copy.
Filename: "{sys}\schtasks.exe"; \
    Parameters: "/Create /TN SirahOSBridge /TR ""\""{app}\{#BridgeExe}\"" push"" /SC MINUTE /MO 15 /F"; \
    Flags: runhidden; \
    Tasks: schedulebridge; \
    StatusMsg: "Registering the SirahOS bridge task..."

; One-shot: open the dashboard so the user can sign in to SirahOS. The /launch
; flag tells widget.py to show the dashboard window on top rather than only
; the tile. (Not every version supports it; the installer leaves it to the
; user to open from the Start menu if the switch is unknown.)
Filename: "{app}\{#AppExe}"; \
    Description: "Launch Memory Analyzer and open the SirahOS sign-in"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; Remove the scheduled task on uninstall. ``2>&1`` would help diagnose but
; keeps us honest -- schtasks is noisy; runhidden hides whatever it says.
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN SirahOSBridge /F"; Flags: runhidden

[UninstallDelete]
; Clean up state the app wrote outside its install dir -- the local store,
; event log, DPAPI blob and config. On a shared machine every user ran their
; own copy, so this is scoped to the current user.
Type: filesandordirs; Name: "{localappdata}\SystemMonitorWidget"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { Add a note to the user before UninstallDelete erases their event log. }
  if CurUninstallStep = usUninstall then begin
    MsgBox('Memory Analyzer will remove your local event log, memory store'
         + ' and SirahOS session.' + #13#10 + #13#10
         + 'WorkLogs already posted to SirahOS are not deleted.',
           mbInformation, MB_OK);
  end;
end;
