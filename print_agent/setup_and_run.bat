@echo off
REM Fine Dine Family Restaurant - auto-print setup
REM Run this once to install dependencies, then it starts the print agent.
REM Leave this window open (or minimized) - closing it stops auto-printing.

REM Prefer the "py" launcher (installed alongside Python on Windows and
REM resolves reliably even right after a fresh install, before some shells
REM have picked up the updated PATH) - fall back to "python" if "py" isn't
REM found, so this works regardless of which one ends up on PATH.
where py >nul 2>nul
if %errorlevel%==0 (
    set PY=py
) else (
    set PY=python
)

echo Using Python launcher: %PY%
echo Installing dependencies (python-escpos, requests)...
%PY% -m pip install python-escpos requests

if errorlevel 1 (
    echo.
    echo FAILED to install dependencies. Make sure Python is installed and on PATH.
    echo Download Python from https://www.python.org/downloads/ if needed, then
    echo RESTART THIS PC before trying again - a fresh Python install often
    echo needs a restart before double-clicked .bat files can find it, even if
    echo it already works in a manually-opened Command Prompt window.
    pause
    exit /b 1
)

echo.
echo Starting print agent - this window must stay open to keep auto-printing working.
echo Press Ctrl+C to stop.
echo.

%PY% -m print_agent.agent --server-url https://finedinefamilyrestaurant.onrender.com --token %PRINT_AGENT_TOKEN%

pause
