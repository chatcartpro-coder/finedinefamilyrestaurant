@echo off
REM Run this ONCE on the restaurant PC to save the print agent's token
REM permanently (survives reboots). Replace YOUR_TOKEN_HERE below with the
REM actual PRINT_AGENT_TOKEN value from Render's Environment tab first.

setx PRINT_AGENT_TOKEN "YOUR_TOKEN_HERE"

echo.
echo Token saved. Close this window and open a NEW terminal before running
echo setup_and_run.bat, so the new environment variable takes effect.
pause
