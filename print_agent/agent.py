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
    printer.text(f"Order #{order['id']}\n")
    printer.text(f"Confirmed: {(order['confirmed_at'] or '')[:16].replace('T', ' ')}\n")
    if order.get("customer_name"):
        printer.text(f"Customer: {order['customer_name']}\n")
    printer.text(f"Phone: {order['phone']}\n")
    printer.text("-" * 32 + "\n")

    for item in order["items"]:
        qty = item["qty"]
        qty_str = f"{qty:g}" if isinstance(qty, float) else str(qty)
        printer.text(f"{qty_str} x {item['name']}\n")
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

    if order.get("order_type") == "dine_in":
        printer.text("-" * 32 + "\n")
        printer.text("DINE-IN - prepare for the customer at the restaurant.\n")
    elif order.get("delivery_address_text"):
        printer.text("-" * 32 + "\n")
        printer.text(f"Deliver to: {order['delivery_address_text']}\n")
    if order.get("notes"):
        printer.text(f"Notes: {order['notes']}\n")

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


def _print(connection_type: str, printer_target: str, order: dict, store_name: str, currency: str):
    if connection_type == "bluetooth":
        print_order_bluetooth(printer_target, order, store_name, currency)
    else:
        print_order(printer_target, order, store_name, currency)


def run_once(server_url: str, token: str, connection_type: str, printer_target: str, store_name: str, currency: str) -> int:
    orders = fetch_pending_orders(server_url, token)
    printed = 0
    for order in orders:
        try:
            _print(connection_type, printer_target, order, store_name, currency)
            acknowledge_order(server_url, token, order["id"])
            logger.info("Printed and acknowledged order #%s", order["id"])
            printed += 1
        except Exception:
            logger.exception(
                "Failed to print order #%s - will retry next poll (not acknowledged)", order["id"]
            )
    return printed


def poll_loop(server_url: str, token: str, connection_type: str, printer_target: str, store_name: str, currency: str, interval: int):
    logger.info(
        "Print agent started - polling %s every %ds, printing via %s to %s",
        server_url, interval, connection_type, printer_target,
    )
    while True:
        try:
            run_once(server_url, token, connection_type, printer_target, store_name, currency)
        except requests.RequestException:
            logger.exception("Failed to reach server - will retry next poll")
        except Exception:
            logger.exception("Unexpected error in poll loop - will retry next poll")
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="Al Madina print agent - polls for confirmed orders and prints them.")
    parser.add_argument("--server-url", default=os.getenv("PRINT_AGENT_SERVER_URL"), help="Cloud app base URL, e.g. https://your-app.onrender.com")
    parser.add_argument("--token", default=os.getenv("PRINT_AGENT_TOKEN"), help="Shared print agent token (matches PRINT_AGENT_TOKEN in the server's .env)")
    parser.add_argument("--printer-ip", default=os.getenv("PRINT_AGENT_PRINTER_IP"), help="Network printer's IP address (for WiFi/Ethernet printers)")
    parser.add_argument("--printer-port", default=os.getenv("PRINT_AGENT_PRINTER_PORT"), help="Serial/COM port (for Bluetooth-paired printers, e.g. COM5 or /dev/rfcomm0)")
    parser.add_argument("--store-name", default=os.getenv("PRINT_AGENT_STORE_NAME", "Al Madina Nawes Supermarket"))
    parser.add_argument("--currency", default=os.getenv("PRINT_AGENT_CURRENCY", "AED"))
    parser.add_argument("--interval", type=int, default=int(os.getenv("PRINT_AGENT_INTERVAL", "10")), help="Seconds between polls")
    parser.add_argument("--once", action="store_true", help="Print any pending orders once and exit, instead of polling forever")
    args = parser.parse_args()

    missing = [name for name, val in [("--server-url", args.server_url), ("--token", args.token)] if not val]
    if missing:
        print(f"Missing required setting(s): {', '.join(missing)} (pass as a flag or set the matching env var)")
        sys.exit(1)

    if args.printer_ip and args.printer_port:
        print("Specify only one of --printer-ip (network printer) or --printer-port (Bluetooth/USB printer), not both.")
        sys.exit(1)

    server_url = args.server_url.rstrip("/")

    if args.printer_ip:
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
        count = run_once(server_url, args.token, connection_type, printer_target, args.store_name, args.currency)
        print(f"Printed {count} order(s).")
    else:
        poll_loop(server_url, args.token, connection_type, printer_target, args.store_name, args.currency, args.interval)


if __name__ == "__main__":
    main()
