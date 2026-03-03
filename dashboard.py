"""
Polymarket Bot — Live Dashboard
================================
Run this in a separate terminal while bot.py is running:

    python dashboard.py

Updates every second. Shows:
  - Bot status & uptime
  - Recent log lines
  - All orders placed (from logs/orders.csv)
  - Per-strategy stats
"""

import csv
import os
import time
from collections import deque
from datetime import datetime, timezone

from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ORDERS_CSV = "logs/orders.csv"
BOT_LOG    = "logs/bot.log"
REFRESH_HZ = 1   # seconds between updates

console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_last_log_lines(n: int = 18) -> list[str]:
    if not os.path.isfile(BOT_LOG):
        return ["Waiting for bot.py to start…"]
    try:
        with open(BOT_LOG, "r", encoding="utf-8", errors="replace") as f:
            lines = deque(f, maxlen=n)
        return [l.rstrip() for l in lines]
    except OSError:
        return ["Cannot read log file."]


def read_orders(max_rows: int = 50) -> list[dict]:
    if not os.path.isfile(ORDERS_CSV):
        return []
    try:
        with open(ORDERS_CSV, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        return rows[-max_rows:]
    except OSError:
        return []


def bot_running() -> bool:
    """Heuristic: log file was written to in the last 10 seconds."""
    if not os.path.isfile(BOT_LOG):
        return False
    return (time.time() - os.path.getmtime(BOT_LOG)) < 10


# ---------------------------------------------------------------------------
# Panel builders
# ---------------------------------------------------------------------------

def make_header() -> Panel:
    running = bot_running()
    status  = Text("● LIVE", style="bold green") if running else Text("● OFFLINE", style="bold red")
    mode_path = "logs/bot.log"
    dry = True
    if os.path.isfile(mode_path):
        try:
            with open(mode_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(4096)
            dry = "DRY RUN MODE" in content
        except OSError:
            pass

    mode = Text("DRY RUN", style="yellow") if dry else Text("LIVE TRADING", style="bold red")
    now  = datetime.now(timezone.utc).strftime("%Y-%m-%d  %H:%M:%S UTC")

    grid = Table.grid(expand=True)
    grid.add_column(justify="left")
    grid.add_column(justify="center")
    grid.add_column(justify="right")
    grid.add_row(status, Text("POLYMARKET BOT", style="bold cyan"), Text(now, style="dim"))
    grid.add_row("", mode, "")

    return Panel(grid, style="bold white")


def make_orders_table(orders: list[dict]) -> Panel:
    table = Table(
        show_header=True,
        header_style="bold magenta",
        expand=True,
        show_lines=False,
        padding=(0, 1),
    )
    table.add_column("Time",       style="dim",         width=19)
    table.add_column("Type",       style="cyan",        width=7)
    table.add_column("Side",       width=5)
    table.add_column("Token",      style="dim",         width=10)
    table.add_column("Price",      justify="right",     width=7)
    table.add_column("USDC",       justify="right",     width=8)
    table.add_column("Shares",     justify="right",     width=9)
    table.add_column("Order ID",   style="dim",         width=14)
    table.add_column("Status",     width=11)
    table.add_column("Dry",        justify="center",    width=5)

    for row in reversed(orders):
        side_color = "green" if row.get("side") == "BUY" else "red"
        dry_val    = row.get("dry_run", "True")
        dry_str    = "✓" if dry_val == "True" else ""
        status_val = row.get("status", "")
        status_style = "green" if status_val in ("matched", "live") else "yellow"

        table.add_row(
            row.get("timestamp", ""),
            row.get("type", ""),
            Text(row.get("side", ""), style=side_color),
            row.get("token_id", "")[:10],
            row.get("price", ""),
            row.get("size_usdc", ""),
            row.get("size_shares", ""),
            row.get("order_id", "")[:14],
            Text(status_val, style=status_style),
            Text(dry_str, style="dim"),
        )

    count = len(orders)
    return Panel(
        table,
        title=f"[bold magenta]Orders[/bold magenta] [dim]({count} shown)[/dim]",
        border_style="magenta",
    )


def make_log_panel(lines: list[str]) -> Panel:
    text = Text()
    for line in lines:
        if "ERROR" in line:
            text.append(line + "\n", style="red")
        elif "WARNING" in line:
            text.append(line + "\n", style="yellow")
        elif "[ORDER]" in line or "[MARKET]" in line or "EXECUTE" in line:
            text.append(line + "\n", style="bold green")
        elif "DRY RUN" in line:
            text.append(line + "\n", style="yellow")
        else:
            text.append(line + "\n", style="dim white")

    return Panel(
        text,
        title="[bold cyan]Live Log[/bold cyan]",
        border_style="cyan",
    )


def make_stats_panel(orders: list[dict]) -> Panel:
    total   = len(orders)
    buys    = sum(1 for o in orders if o.get("side") == "BUY")
    sells   = sum(1 for o in orders if o.get("side") == "SELL")
    dry     = sum(1 for o in orders if o.get("dry_run") == "True")
    live    = total - dry

    try:
        total_usdc = sum(float(o.get("size_usdc", 0)) for o in orders)
    except ValueError:
        total_usdc = 0.0

    limits  = sum(1 for o in orders if o.get("type") == "LIMIT")
    markets = sum(1 for o in orders if o.get("type") == "MARKET")

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", justify="right")
    grid.add_column(style="bold white")

    grid.add_row("Total orders",   str(total))
    grid.add_row("BUY / SELL",     f"{buys} / {sells}")
    grid.add_row("LIMIT / MARKET", f"{limits} / {markets}")
    grid.add_row("Dry / Live",     f"{dry} / {live}")
    grid.add_row("Total USDC",     f"${total_usdc:,.2f}")

    return Panel(grid, title="[bold yellow]Stats[/bold yellow]", border_style="yellow")


# ---------------------------------------------------------------------------
# Main render loop
# ---------------------------------------------------------------------------

def build_layout(orders: list[dict], log_lines: list[str]) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=4),
        Layout(name="body"),
        Layout(name="footer", size=12),
    )
    layout["body"].split_row(
        Layout(name="orders", ratio=3),
        Layout(name="sidebar", ratio=1),
    )

    layout["header"].update(make_header())
    layout["orders"].update(make_orders_table(orders))
    layout["sidebar"].update(make_stats_panel(orders))
    layout["footer"].update(make_log_panel(log_lines))

    return layout


def main():
    console.clear()
    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            orders    = read_orders()
            log_lines = read_last_log_lines(18)
            live.update(build_layout(orders, log_lines))
            time.sleep(REFRESH_HZ)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.clear()
        console.print("[bold cyan]Dashboard closed.[/bold cyan]")
