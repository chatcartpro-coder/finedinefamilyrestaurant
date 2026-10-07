@echo off
REM Fine Dine Family Restaurant - auto-print setup
REM Run this once to install dependencies, then it starts the print agent.
REM Leave this window open (or minimized) - closing it stops auto-printing.

REM If this printer is ALSO used by other software (e.g. existing KOT/POS
REM software) through its normal Windows driver, set PRINTER_NAME below to
REM that printer's exact name (Settings > Bluetooth & devices > Printers &
REM scanners - click it to see the exact name) and this script will print
REM through the same Windows spooler instead of opening its own direct
REM network connection, which was confirmed to conflict with KOT printing.
REM Leave PRINTER_NAME blank to keep using the direct network IP instead
REM (set in the admin dashboard's Printer page) - only do this if nothing
REM else prints to this printer.
set PRINTER_NAME=

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
echo Installing dependencies (python-escpos, requests, pywin32)...
REM pywin32 is needed for --printer-name (prints through the Windows
REM spooler by printer name) - harmless to always install even if
REM PRINTER_NAME above is left blank and the direct network IP is used
REM instead.
%PY% -m pip install python-escpos requests pywin32

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

REM Run agent.py directly as a script (next to this .bat file) rather than
REM as "-m print_agent.agent" - the latter requires a parent folder actually
REM named print_agent one level up, which isn't guaranteed if only this
REM folder's contents were copied to the store PC (confirmed live:
REM ModuleNotFoundError: No module named 'print_agent'). agent.py is
REM self-contained (no imports from the rest of the project), so running it
REM directly works regardless of what the containing folder is named.
if "%PRINTER_NAME%"=="" (
    %PY% "%~dp0agent.py" --server-url https://finedinefamilyrestaurant.onrender.com --token %PRINT_AGENT_TOKEN%
) else (
    %PY% "%~dp0agent.py" --server-url https://finedinefamilyrestaurant.onrender.com --token %PRINT_AGENT_TOKEN% --printer-name "%PRINTER_NAME%"
)

pause
