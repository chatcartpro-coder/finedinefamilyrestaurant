@echo off
REM Fine Dine Family Restaurant - auto-print setup
REM Run this once to install dependencies, then it starts the print agent.
REM Leave this window open (or minimized) - closing it stops auto-printing.

echo Installing dependencies (python-escpos, requests)...
pip install python-escpos requests

if errorlevel 1 (
    echo.
    echo FAILED to install dependencies. Make sure Python is installed and on PATH.
    echo Download Python from https://www.python.org/downloads/ if needed, then re-run this.
    pause
    exit /b 1
)

echo.
echo Starting print agent - this window must stay open to keep auto-printing working.
echo Press Ctrl+C to stop.
echo.

python -m print_agent.agent --server-url https://finedinefamilyrestaurant.onrender.com --token %PRINT_AGENT_TOKEN%

pause
