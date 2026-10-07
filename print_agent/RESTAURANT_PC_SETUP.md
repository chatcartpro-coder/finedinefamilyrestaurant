# Auto-print setup for the restaurant PC

This folder only needs: `agent.py`, `set_token.bat`, `setup_and_run.bat`, and a
Python 3.10+ install on this PC. Copy this whole `print_agent` folder to the
restaurant PC (USB drive, email, cloud link - any way works).

## One-time setup

1. Install Python if not already installed: https://www.python.org/downloads/
   (during install, check "Add Python to PATH").
2. Open `set_token.bat` in Notepad, replace `YOUR_TOKEN_HERE` with the actual
   `PRINT_AGENT_TOKEN` value (copy it from Render's dashboard -> Environment
   tab), save, close Notepad.
3. Double-click `set_token.bat` and let it run once.
4. Close and reopen any terminal windows (so the new token is picked up).
5. **If this printer is ALSO used by other software** (e.g. existing KOT/POS
   software) through its normal Windows driver: open `setup_and_run.bat` in
   Notepad, set `PRINTER_NAME` near the top to that printer's exact name
   (Settings > Bluetooth & devices > Printers & scanners - click it to see
   the exact name), save, close Notepad. This makes auto-print share the
   same Windows print queue as your existing software instead of opening
   its own direct network connection, which otherwise conflicts with KOT
   printing (confirmed live). Leave `PRINTER_NAME` blank only if nothing
   else prints to this printer.
6. Double-click `setup_and_run.bat` - it installs the required Python
   packages, then starts polling for confirmed orders and prints them to the
   printer configured in the admin dashboard's Printer page
   (https://finedinefamilyrestaurant.onrender.com/admin/printer), or to
   `PRINTER_NAME` if you set that in step 5.

## Keep it running

Leave the `setup_and_run.bat` window open (minimizing is fine) on a PC that's
always on at the restaurant. It checks every 10 seconds and prints any newly
confirmed order exactly once automatically - nothing else to do day-to-day.

To make it survive PC restarts without someone double-clicking the file every
morning, add a shortcut to `setup_and_run.bat` to the Windows Startup folder:
1. Press `Win+R`, type `shell:startup`, press Enter.
2. Drag a shortcut to `setup_and_run.bat` into that folder.
It'll then start automatically every time the PC boots/logs in.

## If printing stops working

- Check the terminal window for error messages - a failed print is retried
  automatically on the next poll, so a brief network/printer hiccup fixes
  itself.
- Confirm the printer's IP hasn't changed (some routers reassign IPs) by
  re-checking Windows' printer properties and updating the admin dashboard's
  Printer page if it has.
- Confirm the printer is powered on and connected to the same WiFi/network.
- If existing KOT/POS software's prints stopped working after setting this
  up, set `PRINTER_NAME` in `setup_and_run.bat` as described in step 5 above
  - a direct network connection (the default) and KOT software's
    Windows-driver printing can conflict over the same printer.
