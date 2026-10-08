@echo off
REM Run once on a PC with Python to build dist\PrintAgent.exe
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
%PY% -m pip install pyinstaller python-escpos requests pywin32 pyserial pyusb
for /f "delims=" %%i in ('%PY% -c "import escpos,os;print(os.path.join(os.path.dirname(escpos.__file__),'capabilities.json'))"') do set CAPS=%%i
%PY% -m PyInstaller --onefile --add-data "%CAPS%;escpos" --console --name PrintAgent --collect-all escpos --hidden-import win32print --hidden-import serial --hidden-import usb --hidden-import tkinter agent.py
echo.
echo Done. Copy dist\PrintAgent.exe and config.ini (from config.ini.example) to the restaurant PC.
pause
