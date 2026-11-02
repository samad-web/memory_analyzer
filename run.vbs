' Launches the widget elevated and with no console window.
' Double-click this file to start the monitor. You get one UAC prompt, which is
' what buys the fan-speed and CPU-temperature readings from the MSI embedded
' controller. To skip the prompt at every logon, use "Start with Windows" in the
' widget's right-click menu -- that registers a scheduled task instead.

Option Explicit

Dim shell, fso, here, pythonw, script
Set shell = CreateObject("Shell.Application")
Set fso = CreateObject("Scripting.FileSystemObject")

here = fso.GetParentFolderName(WScript.ScriptFullName)
script = here & "\widget.py"

' pythonw.exe runs a GUI script without opening a console window.
pythonw = "C:\Python313\pythonw.exe"
If Not fso.FileExists(pythonw) Then
    pythonw = "pythonw.exe"
End If

shell.ShellExecute pythonw, """" & script & """", here, "runas", 0
