@echo off
REM Starts the widget elevated, showing errors in this console window.
REM Prefer run.vbs for everyday use -- it starts the widget without a console.
REM Use this one when something is wrong and you want to see the traceback.

cd /d "%~dp0"
python widget.py %*
pause
