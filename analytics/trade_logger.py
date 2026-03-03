"""
Quant trade logger — persists trades with rich reason codes.
=============================================================
Writes one row per trade to logs/quant_trades.csv, capturing:

  Signal layer   : score, confidence, regime, per-factor components
  Risk state     : circuit-breaker status, daily P&L, drawdown
  Execution      : expected edge, realized edge, order type, order ID
  Identification : strategy name, market, side, price, size, timestamp

The CSV can then be analysed by analytics/performance_report.py.

Usage:
    from analytics import QuantTradeLogger, TradeRecord
    logger = QuantTradeLogger()
    logger.log(TradeRecord(
        strategy="market_maker",
        market_id=condition_id,
        market_question=question,
        side="BUY",
        token_id=token_id,
        price=0.48,
        size_usdc=50.0,
        signal_score=0.31,
        signal_confidence=0.55,
        regime="ranging",
        expected_edge=0.025,
        order_id="abc123",
        status="filled",
    ))
"""

from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

os.makedirs("logs", exist_ok=True)
QUANT_TRADE_LOG = "logs/quant_trades.csv"

_COLUMNS = [
    "timestamp_utc",
    "strategy",
    "market_id",
    "market_question",
    "side",
    "token_id",
    "price",
    "size_usdc",
    # Signal layer
    "signal_score",
    "signal_confidence",
    "regime",
    "micro_score",
    "momentum_score",
    "mean_rev_score",
    # Execution quality
    "expected_edge",
    "realized_edge",
    "order_type",
    # Risk state
    "circuit_breaker",
    "daily_pnl",
    "drawdown",
    # Order outcome
    "order_id",
    "status",
    "dry_run",
    "notes",
]


@dataclass
class TradeRecord:
    """All fields for one logged trade.  Unset fields default to neutral."""
    strategy:          str
    market_id:         str
    market_question:   str
    side:              str   # "BUY" | "SELL"
    token_id:          str
    price:             float
    size_usdc:         float

    # Signal layer
    signal_score:      float = 0.0
    signal_confidence: float = 0.0
    regime:            str   = ""
    micro_score:       float = 0.0
    momentum_score:    float = 0.0
    mean_rev_score:    float = 0.0

    # Execution quality
    expected_edge:     float = 0.0
    realized_edge:     float = 0.0   # fill in after settlement
    order_type:        str   = "LIMIT"

    # Risk snapshot at time of trade
    circuit_breaker:   str   = "CLOSED"
    daily_pnl:         float = 0.0
    drawdown:          float = 0.0

    # Order outcome
    order_id:          str   = ""
    status:            str   = ""
    dry_run:           bool  = True
    notes:             str   = ""


class QuantTradeLogger:
    """
    Appends TradeRecord rows to logs/quant_trades.csv.

    Thread-safe for asyncio (single-threaded event loop).
    File is opened in append mode per write — no file-handle leaks.
    """

    def __init__(self, path: str = QUANT_TRADE_LOG):
        self._path = path
        self._ensure_header()

    def _ensure_header(self) -> None:
        if not os.path.isfile(self._path):
            with open(self._path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(_COLUMNS)

    def log(self, record: TradeRecord) -> None:
        """Append one trade record to the CSV."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = [
            ts,
            record.strategy,
            record.market_id,
            record.market_question[:120],   # truncate long questions
            record.side,
            record.token_id,
            round(record.price, 6),
            round(record.size_usdc, 4),
            round(record.signal_score, 4),
            round(record.signal_confidence, 4),
            record.regime,
            round(record.micro_score, 4),
            round(record.momentum_score, 4),
            round(record.mean_rev_score, 4),
            round(record.expected_edge, 6),
            round(record.realized_edge, 6),
            record.order_type,
            record.circuit_breaker,
            round(record.daily_pnl, 4),
            round(record.drawdown, 4),
            record.order_id,
            record.status,
            int(record.dry_run),
            record.notes,
        ]
        with open(self._path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)
        log.debug(
            "QuantTradeLogger: %s %s %s @ %.4f edge=%.4f",
            record.strategy, record.side, record.token_id[:8],
            record.price, record.expected_edge,
        )

    def update_realized_edge(
        self,
        order_id: str,
        realized_edge: float,
    ) -> bool:
        """
        Back-fill the realized_edge for a specific order_id.

        Reads the entire CSV, updates the matching row, and rewrites.
        Returns True if the row was found and updated.
        """
        try:
            with open(self._path, "r", newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))
        except FileNotFoundError:
            return False

        if not rows:
            return False

        header = rows[0]
        try:
            oid_col = header.index("order_id")
            edge_col = header.index("realized_edge")
        except ValueError:
            return False

        found = False
        for row in rows[1:]:
            if len(row) > oid_col and row[oid_col] == order_id:
                if len(row) > edge_col:
                    row[edge_col] = str(round(realized_edge, 6))
                    found = True
                    break

        if found:
            with open(self._path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(rows)

        return found
