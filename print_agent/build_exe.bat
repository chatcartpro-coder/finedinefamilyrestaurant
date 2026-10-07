@echo off
REM Run once on a PC with Python to build dist\PrintAgent.exe
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
%PY% -m pip install pyinstaller python-escpos requests pywin32 pyserial pyusb
%PY% -m PyInstaller --onefile --console --name PrintAgent --collect-all escpos --hidden-import win32print --hidden-import serial --hidden-import usb --hidden-import tkinter agent.py
echo.
echo Done. Copy dist\PrintAgent.exe and config.ini (from config.ini.example) to the restaurant PC.
pause
