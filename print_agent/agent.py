"""
Standalone script that runs on a PC/Raspberry Pi at the store, on the same
network (or Bluetooth range) as the thermal printer. Polls the cloud app for
newly confirmed orders and prints each one as an ESC/POS receipt, either:
  - over a raw TCP connection to a network printer (port 9100, the standard
    network-print port most thermal receipt printers listen on), or
  - over a virtual serial port (e.g. a Windows COM port) created by pairing
    a Bluetooth SPP printer with the OS - most small/portable thermal
    printers (the kind with no WiFi, just Bluetooth) work this way. See the
    README for the one-time OS-level Bluetooth pairing steps; this script
    only needs the resulting port name/path, it doesn't do the pairing
    itself.

This script is intentionally self-contained - it does NOT import anything
from the main app (no shared DB, no shared config module), since it runs as
a separate process on a separate network from the cloud server. It only
needs `requests` and `python-escpos` installed.

Why polling, not a direct connection from the cloud server: the printer sits
on the store's private network (or pairs only locally, for Bluetooth) with
no public IP, and the cloud app (e.g. Render) can't reach it directly.
Having the store poll outward avoids asking the store to open any inbound
port on their router/firewall - this script initiates every connection,
nothing needs to accept connections from outside.

Setup:
    pip install python-escpos requests

Connection settings - two ways to provide them:
  1. (Recommended) Configure the printer once in the admin dashboard's
     Printer page (network IP, or Bluetooth/USB COM port) - this script
     fetches those settings automatically on every run, no local flags
     needed:
        python -m print_agent.agent --server-url https://your-app.onrender.com \\
            --token YOUR_PRINT_AGENT_TOKEN
  2. Override locally with --printer-ip (network) or --printer-port
     (Bluetooth/USB, e.g. COM5 after pairing with the OS - see README) -
     useful if you don't want the connection managed from the dashboard:
        python -m print_agent.agent --server-url https://your-app.onrender.com \\
            --token YOUR_PRINT_AGENT_TOKEN --printer-ip 192.168.1.50

    (or set PRINT_AGENT_SERVER_URL / PRINT_AGENT_TOKEN / PRINT_AGENT_PRINTER_IP
    / PRINT_AGENT_PRINTER_PORT as environment variables instead of flags)

Leave this running continuously (e.g. as a scheduled/startup task) - it
polls every --interval seconds (default 10) and prints any new confirmed
order exactly once. Note: Bluetooth printers have short range and typically
pair to only one device at a time, and cheap models may sleep/disconnect to
save battery - a failed print is retried on the next poll rather than lost
(see run_once()), but this is inherently less "leave it running for weeks
unattended" reliable than a network printer.
"""
import argparse
import logging
import os
import sys
import textwrap
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("print-agent")


def fetch_pending_orders(server_url: str, token: str) -> list:
    resp = requests.get(
        f"{server_url}/print-agent/orders/pending",
        headers={"X-Print-Agent-Token": token},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["orders"]


def fetch_printer_config(server_url: str, token: str) -> dict:
    """Fetches the printer connection settings configured in the admin
    dashboard's Printer page, used when --printer-ip/--printer-port aren't
    given locally - lets the store manage the connection from the dashboard
    instead of editing this script's launch command by hand."""
    resp = requests.get(
        f"{server_url}/print-agent/printer-config",
        headers={"X-Print-Agent-Token": token},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def acknowledge_order(server_url: str, token: str, order_id: int):
    resp = requests.post(
        f"{server_url}/print-agent/orders/{order_id}/ack",
        headers={"X-Print-Agent-Token": token},
        timeout=15,
    )
    resp.raise_for_status()


def _write_receipt(printer, order: dict, store_name: str, currency: str):
    """Renders the actual receipt content onto an already-open python-escpos
    printer object. Shared by both connection types (network/bluetooth) so
    the receipt layout only needs to be defined and maintained once.
    order_type_label/amount_excl_vat/vat_amount/customer_name come from
    print_agent/routes.py's _serialize_order() - the admin dashboard's order
    serializer, not computed here, so this stays a thin rendering layer."""
    order_type_label = order.get("order_type_label") or ("Takeaway" if order.get("order_type") == "pickup" else "Delivery")

    printer.set(align="center", bold=True, width=2, height=2)
    printer.text(f"{store_name}\n")
    printer.set(align="center", bold=False, width=1, height=1)
    printer.text("-" * 32 + "\n")

    # Order type is the first thing printed after the store name, in bold,
    # so kitchen/counter staff can tell at a glance whether to pack for
    # delivery, bag for takeaway, or prepare for a dine-in customer who'll
    # be seated shortly - no need to read the whole receipt first.
    printer.set(align="center", bold=True, width=2, height=1)
    printer.text(f"*** {order_type_label.upper()} ***\n")
    printer.set(align="center", bold=False, width=1, height=1)
    printer.text("-" * 32 + "\n")

    printer.set(align="left")
    printer.text(f"Order: {order.get('order_code') or '#' + str(order['id'])}\n")
    # confirmed_at here is already converted to the restaurant's local time
    # by the server (print_agent/routes.py's _serialize_order), not raw UTC -
    # confirmed live the receipt printed 4 hours behind the dashboard
    # (which does the same conversion via admin/templating.py's local_time
    # Jinja filter) before this was added.
    printer.text(f"Confirmed: {(order['confirmed_at'] or '')[:16].replace('T', ' ')}\n")
    if order.get("customer_name"):
        printer.text(f"Customer: {order['customer_name']}\n")
    printer.text(f"Phone: {order['phone']}\n")
    printer.text("-" * 32 + "\n")

    has_off_catalog = False
    for item in order["items"]:
        qty = item["qty"]
        qty_str = f"{qty:g}" if isinstance(qty, float) else str(qty)
        printer.text(f"{qty_str} x {item['name']}\n")
        if item.get("catalog_item_id") is None:
            # Off-catalog item (ai/agent.py's ADDITEM: trailer) - no real
            # price yet, restaurant confirms at delivery; "AED 0.00" here
            # would misleadingly read as "free".
            printer.text("  Price TBD - restaurant will confirm at delivery\n")
            has_off_catalog = True
        else:
            printer.text(f"  {currency} {item['unit_price']:.2f} = {currency} {item['line_total']:.2f}\n")

    printer.text("-" * 32 + "\n")
    printer.text(f"Subtotal: {currency} {order['subtotal']:.2f}\n")
    if order.get("order_type") not in ("pickup", "dine_in"):
        printer.text(f"Delivery: {currency} {order['delivery_fee']:.2f}\n")
    if order.get("discount_applied"):
        printer.text("Discount applied\n")
    printer.set(bold=True)
    printer.text(f"TOTAL: {currency} {order['total']:.2f}\n")
    printer.set(bold=False)
    if order.get("vat_amount") is not None:
        printer.text(f"(incl. VAT {currency} {order['vat_amount']:.2f}, excl. VAT {currency} {order['amount_excl_vat']:.2f})\n")
    if has_off_catalog:
        printer.text("Total excludes item(s) with price TBD - call restaurant to confirm\n")

    if order.get("order_type") == "dine_in":
        printer.text("-" * 32 + "\n")
        printer.text("DINE-IN - prepare for the customer at the restaurant.\n")
    elif order.get("delivery_address_text") or order.get("delivery_lat") is not None:
        # A customer can share their address as typed text
        # (delivery_address_text) or a WhatsApp location pin (delivery_lat/
        # lng) - a pin with no label previously printed nothing at all here,
        # same bug fixed on the WhatsApp receipt and browser-print template.
        printer.text("-" * 32 + "\n")
        if order.get("delivery_address_text"):
            printer.text(f"Deliver to: {order['delivery_address_text']}\n")
        if order.get("delivery_lat") is not None:
            printer.text(f"Map: https://maps.google.com/?q={order['delivery_lat']},{order['delivery_lng']}\n")
    if order.get("notes"):
        # Notes accumulate across a conversation as "Extra spicy; No
        # onions; Ring doorbell twice" (storage.store.add_order_note) - a
        # printer.text() of the whole string as one line either runs off
        # the receipt or hard-wraps mid-word on 80mm paper (~32 chars/line
        # for the standard font), since python-escpos doesn't word-wrap
        # for you. Splits back into individual notes and wraps each one
        # cleanly at word boundaries instead, as a short bulleted list -
        # far more readable for kitchen/delivery staff on a narrow ticket.
        printer.text("Notes:\n")
        for note in order["notes"].split("; "):
            note = note.strip()
            if not note:
                continue
            wrapped = textwrap.wrap(note, width=30, initial_indent="- ", subsequent_indent="  ")
            for line in wrapped:
                printer.text(f"{line}\n")

    printer.text("\n")
    printer.cut()


def print_order(printer_ip: str, order: dict, store_name: str, currency: str):
    """Prints to a network (WiFi/Ethernet) printer, connecting over TCP."""
    from escpos.printer import Network

    printer = Network(printer_ip, timeout=10)
    try:
        _write_receipt(printer, order, store_name, currency)
    finally:
        printer.close()


def print_order_bluetooth(printer_port: str, order: dict, store_name: str, currency: str):
    """Prints to a Bluetooth SPP printer via the virtual serial port (e.g.
    a Windows COM port, or /dev/rfcomm0 on Linux) created when the printer
    is paired with the OS - see the README for the one-time pairing steps.
    python-escpos has no dedicated Bluetooth connection class because it
    doesn't need one: once paired, the OS exposes the link as an ordinary
    serial port, so the existing Serial connection class talks to it
    directly."""
    from escpos.printer import Serial

    printer = Serial(devfile=printer_port, baudrate=9600, timeout=10)
    try:
        _write_receipt(printer, order, store_name, currency)
    finally:
        printer.close()


def print_order_windows(printer_name: str, order: dict, store_name: str, currency: str):
    """Prints via a printer already installed in Windows (Settings ->
    Printers & scanners), going through the normal Windows print
    spooler instead of opening our own raw TCP/network socket to the
    printer. Needed when the same printer is shared with other software
    (e.g. existing KOT/POS software) that also prints to it through its
    Windows driver - confirmed live that print_order's raw Network
    connection (its own direct socket, bypassing Windows entirely)
    conflicted with that software's KOT print jobs, since most thermal
    printers only properly service one connection "style" at a time."""
    from escpos.printer import Win32Raw

    printer = Win32Raw(printer_name)
    try:
        _write_receipt(printer, order, store_name, currency)
    finally:
        printer.close()


def _print(connection_type: str, printer_target: str, order: dict, store_name: str, currency: str):
    if connection_type == "bluetooth":
        print_order_bluetooth(printer_target, order, store_name, currency)
    elif connection_type == "windows":
        print_order_windows(printer_target, order, store_name, currency)
    else:
        print_order(printer_target, order, store_name, currency)


_popup_queue = None


def _popup_worker(q):
    """Runs all popups on one dedicated thread (Tk must stay on the thread
    that created it). Each popup is topmost so it appears over every other
    app until staff click OK."""
    try:
        import tkinter as tk
    except Exception:
        logger.warning("tkinter unavailable - desktop popup disabled")
        return
    while True:
        title, body = q.get()
        try:
            root = tk.Tk()
            root.title(title)
            root.attributes("-topmost", True)
            root.geometry("+200+120")
            tk.Label(root, text=title, font=("Segoe UI", 20, "bold"), fg="#b00020", padx=40, pady=14).pack()
            tk.Label(root, text=body, font=("Segoe UI", 14), justify="left", padx=40).pack()
            tk.Button(root, text="OK", font=("Segoe UI", 14, "bold"), width=14, command=root.destroy).pack(pady=16)
            root.lift()
            root.focus_force()
            root.mainloop()
        except Exception:
            logger.exception("Failed to show desktop popup")


def show_order_popup(order: dict, currency: str):
    """Always-on-top 'new order' window. Never blocks or breaks printing."""
    global _popup_queue
    try:
        if _popup_queue is None:
            import queue
            import threading
            _popup_queue = queue.Queue()
            threading.Thread(target=_popup_worker, args=(_popup_queue,), daemon=True).start()
        lines = [f"{i['qty']:g} x {i['name']}" for i in order.get("items", [])]
        body = "\n".join(lines + ["", f"{order.get('order_type_label') or ''}   Total: {currency} {order['total']:.2f}"])
        if order.get("notes"):
            body += "\nNotes: " + order["notes"]
        _popup_queue.put((f"NEW ORDER {order.get('order_code') or order['id']}", body))
    except Exception:
        logger.exception("Could not queue desktop popup")


def run_once(server_url: str, token: str, connection_type: str, printer_target: str, store_name: str, currency: str,
             extra_printer_ips: list = None) -> int:
    orders = fetch_pending_orders(server_url, token)
    printed = 0
    for order in orders:
        try:
            _print(connection_type, printer_target, order, store_name, currency)
            # Extra network printers (--extra-printer-ip, e.g. a second
            # kitchen/counter copy) always print over a direct network
            # connection, same as print_order - not bluetooth/windows,
            # since those require their own distinct connection_type per
            # printer, which this simple "prints the same order to every
            # configured target" feature doesn't need to support. A
            # failure on one extra printer is logged but never blocks
            # acknowledging the order or printing to the other targets -
            # the primary printer having gotten it is what matters for
            # not re-printing on the next poll.
            for extra_ip in (extra_printer_ips or []):
                try:
                    print_order(extra_ip, order, store_name, currency)
                except Exception:
                    logger.exception("Failed to print order #%s to extra printer %s (non-fatal)", order["id"], extra_ip)
            acknowledge_order(server_url, token, order["id"])
            logger.info("Printed and acknowledged order #%s", order["id"])
            show_order_popup(order, currency)
            printed += 1
        except Exception:
            logger.exception(
                "Failed to print order #%s - will retry next poll (not acknowledged)", order["id"]
            )
    return printed


def poll_loop(server_url: str, token: str, connection_type: str, printer_target: str, store_name: str, currency: str, interval: int,
              extra_printer_ips: list = None):
    logger.info(
        "Print agent started - polling %s every %ds, printing via %s to %s%s",
        server_url, interval, connection_type, printer_target,
        f" (plus extra printers: {', '.join(extra_printer_ips)})" if extra_printer_ips else "",
    )
    while True:
        try:
            run_once(server_url, token, connection_type, printer_target, store_name, currency, extra_printer_ips)
        except requests.RequestException:
            logger.exception("Failed to reach server - will retry next poll")
        except Exception:
            logger.exception("Unexpected error in poll loop - will retry next poll")
        time.sleep(interval)


def _load_config_ini():
    """Loads config.ini sitting next to the exe (or script) into PRINT_AGENT_*
    env vars, without overriding any already set - lets staff configure the
    exe by editing a plain text file instead of a .bat."""
    import configparser
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "config.ini")
    os.environ.setdefault("PRINT_AGENT_SERVER_URL", "https://finedinefamilyrestaurant.onrender.com")
    if not os.path.exists(path):
        return
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    keys = {
        "server_url": "PRINT_AGENT_SERVER_URL", "token": "PRINT_AGENT_TOKEN",
        "printer_name": "PRINT_AGENT_PRINTER_NAME", "printer_ip": "PRINT_AGENT_PRINTER_IP",
        "extra_printer_ips": "PRINT_AGENT_EXTRA_PRINTER_IPS", "store_name": "PRINT_AGENT_STORE_NAME",
        "currency": "PRINT_AGENT_CURRENCY", "interval": "PRINT_AGENT_INTERVAL",
    }
    for key, env in keys.items():
        val = cp.get("printer", key, fallback="").strip()
        if val and not os.environ.get(env):
            os.environ[env] = val


def main():
    _load_config_ini()
    parser = argparse.ArgumentParser(description="Fine Dine Family Restaurant print agent - polls for confirmed orders and prints them.")
    parser.add_argument("--server-url", default=os.getenv("PRINT_AGENT_SERVER_URL"), help="Cloud app base URL, e.g. https://your-app.onrender.com")
    parser.add_argument("--token", default=os.getenv("PRINT_AGENT_TOKEN"), help="Shared print agent token (matches PRINT_AGENT_TOKEN in the server's .env)")
    parser.add_argument("--printer-ip", default=os.getenv("PRINT_AGENT_PRINTER_IP"), help="Network printer's IP address (for WiFi/Ethernet printers) - opens its own direct TCP connection, which can conflict with other software (e.g. existing KOT/POS software) printing to the same printer through its Windows driver. Prefer --printer-name if that's your setup.")
    parser.add_argument("--printer-port", default=os.getenv("PRINT_AGENT_PRINTER_PORT"), help="Serial/COM port (for Bluetooth-paired printers, e.g. COM5 or /dev/rfcomm0)")
    parser.add_argument("--printer-name", default=os.getenv("PRINT_AGENT_PRINTER_NAME"), help="Name of a printer already installed in Windows (Settings > Printers & scanners) - prints through the normal Windows spooler instead of a direct network connection, so it coexists with other software (e.g. existing KOT/POS software) sharing the same printer.")
    parser.add_argument("--store-name", default=os.getenv("PRINT_AGENT_STORE_NAME", "Fine Dine Family Restaurant"))
    parser.add_argument("--currency", default=os.getenv("PRINT_AGENT_CURRENCY", "AED"))
    parser.add_argument("--interval", type=int, default=int(os.getenv("PRINT_AGENT_INTERVAL", "3")), help="Seconds between polls")
    parser.add_argument("--once", action="store_true", help="Print any pending orders once and exit, instead of polling forever")
    parser.add_argument(
        "--extra-printer-ip", action="append", default=None,
        help="Additional network printer IP to ALSO print every order to (e.g. a second kitchen/counter copy) - "
             "repeat the flag for more than one, or set PRINT_AGENT_EXTRA_PRINTER_IPS as a comma-separated list. "
             "Always a direct network connection regardless of the primary printer's connection type.",
    )
    args = parser.parse_args()
    extra_printer_ips = args.extra_printer_ip or [
        ip.strip() for ip in os.getenv("PRINT_AGENT_EXTRA_PRINTER_IPS", "").split(",") if ip.strip()
    ]

    missing = [name for name, val in [("--server-url", args.server_url), ("--token", args.token)] if not val]
    if missing:
        print(f"Missing required setting(s): {', '.join(missing)} (pass as a flag or set the matching env var)")
        sys.exit(1)

    given = [name for name, val in [("--printer-ip", args.printer_ip), ("--printer-port", args.printer_port), ("--printer-name", args.printer_name)] if val]
    if len(given) > 1:
        print(f"Specify only one of --printer-ip, --printer-port, or --printer-name, not multiple ({', '.join(given)} given).")
        sys.exit(1)

    server_url = args.server_url.rstrip("/")

    if args.printer_name:
        connection_type, printer_target = "windows", args.printer_name
    elif args.printer_ip:
        connection_type, printer_target = "network", args.printer_ip
    elif args.printer_port:
        connection_type, printer_target = "bluetooth", args.printer_port
    else:
        # No local override given - use the connection settings configured
        # in the admin dashboard's Printer page instead.
        try:
            server_config = fetch_printer_config(server_url, args.token)
        except requests.RequestException as e:
            print(f"Could not reach server to fetch printer settings: {e}")
            sys.exit(1)
        if not server_config.get("configured"):
            print(
                "No printer configured. Set one up in the admin dashboard's Printer page, "
                "or pass --printer-ip / --printer-port to override locally."
            )
            sys.exit(1)
        connection_type = server_config["connection_type"]
        printer_target = server_config["printer_ip"] if connection_type == "network" else server_config["printer_port"]
        logger.info("Using printer settings from server: %s -> %s", connection_type, printer_target)

    if args.once:
        count = run_once(server_url, args.token, connection_type, printer_target, args.store_name, args.currency, extra_printer_ips)
        print(f"Printed {count} order(s).")
    else:
        poll_loop(server_url, args.token, connection_type, printer_target, args.store_name, args.currency, args.interval, extra_printer_ips)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (0, None):
            input("Press Enter to close...")
        raise
    except Exception:
        logger.exception("Print agent crashed")
        input("Press Enter to close...")
