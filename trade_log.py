#!/usr/bin/env python3
"""Trade log for tracking entry/exit performance.

Supports JSON and SQLite storage (configurable via TRADE_LOG_PATH env var).
Tracks: entry type, tier, colors, SL distance, entry/exit prices, MAE/MFE, exit reason, R multiple.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent

# Default: JSON in out/trades/ (Railway volume-friendly)
DEFAULT_LOG_PATH = str(ROOT / "out" / "trades" / "trades.json")
TRADE_LOG_PATH = os.environ.get("TRADE_LOG_PATH", DEFAULT_LOG_PATH)


def _log_path() -> str:
    """Resolve at call time: env TRADE_LOG_PATH wins, else the module default."""
    return os.environ.get("TRADE_LOG_PATH") or TRADE_LOG_PATH


def _is_sqlite(path: str) -> bool:
    """Check if path is SQLite database."""
    return path.endswith(".db") or path.endswith(".sqlite")


def _ensure_json_dir(path: str) -> None:
    """Ensure parent directory for JSON log exists."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _init_sqlite(conn: sqlite3.Connection) -> None:
    """Initialize SQLite schema."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT UNIQUE NOT NULL,
            symbol TEXT NOT NULL,
            entry_type TEXT,
            tier TEXT,
            trend_1d TEXT,
            trend_4h TEXT,
            sl_dist_pct REAL,
            entry_price REAL,
            entry_size REAL,
            entry_leverage REAL,
            entry_timestamp TEXT,
            exit_price REAL,
            exit_timestamp TEXT,
            exit_reason TEXT,
            mae_pct REAL,
            mfe_pct REAL,
            r_multiple REAL,
            pnl_usd REAL,
            ai_decision_reason TEXT,
            dry_run INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_trades_entry_timestamp ON trades(entry_timestamp)
    """)
    conn.commit()


def log_entry(
    trade_id: str,
    symbol: str,
    entry_type: Optional[str] = None,
    tier: Optional[str] = None,
    trend_1d: Optional[str] = None,
    trend_4h: Optional[str] = None,
    sl_dist_pct: Optional[float] = None,
    entry_price: Optional[float] = None,
    entry_size: Optional[float] = None,
    entry_leverage: Optional[float] = None,
    ai_decision_reason: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Log a new trade entry.
    
    Returns:
        dict with ok, trade_id, message
    """
    now = datetime.now(timezone.utc).isoformat()
    
    record = {
        "trade_id": trade_id,
        "symbol": symbol,
        "entry_type": entry_type,
        "tier": tier,
        "trend_1d": trend_1d,
        "trend_4h": trend_4h,
        "sl_dist_pct": sl_dist_pct,
        "entry_price": entry_price,
        "entry_size": entry_size,
        "entry_leverage": entry_leverage,
        "entry_timestamp": now,
        "exit_price": None,
        "exit_timestamp": None,
        "exit_reason": None,
        "mae_pct": None,
        "mfe_pct": None,
        "r_multiple": None,
        "pnl_usd": None,
        "ai_decision_reason": ai_decision_reason,
        "dry_run": dry_run,
    }
    
    path = _log_path()
    
    if _is_sqlite(path):
        # SQLite
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        _init_sqlite(conn)
        
        try:
            conn.execute(
                """
                INSERT INTO trades (
                    trade_id, symbol, entry_type, tier, trend_1d, trend_4h,
                    sl_dist_pct, entry_price, entry_size, entry_leverage,
                    entry_timestamp, ai_decision_reason, dry_run
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id, symbol, entry_type, tier, trend_1d, trend_4h,
                    sl_dist_pct, entry_price, entry_size, entry_leverage,
                    now, ai_decision_reason, 1 if dry_run else 0,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return {"ok": False, "error": f"trade_id {trade_id} already exists"}
        finally:
            conn.close()
    else:
        # JSON
        _ensure_json_dir(path)
        
        # Load existing
        trades = []
        if Path(path).is_file():
            try:
                trades = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        
        # Check duplicate
        if any(t.get("trade_id") == trade_id for t in trades):
            return {"ok": False, "error": f"trade_id {trade_id} already exists"}
        
        # Append
        trades.append(record)
        
        # Write
        Path(path).write_text(json.dumps(trades, indent=2), encoding="utf-8")
    
    return {"ok": True, "trade_id": trade_id, "message": "entry logged"}


def log_exit(
    trade_id: str,
    exit_price: float,
    exit_reason: str,
    mae_pct: Optional[float] = None,
    mfe_pct: Optional[float] = None,
    r_multiple: Optional[float] = None,
    pnl_usd: Optional[float] = None,
) -> Dict[str, Any]:
    """Log trade exit.
    
    Returns:
        dict with ok, trade_id, message
    """
    now = datetime.now(timezone.utc).isoformat()
    
    path = _log_path()
    
    if _is_sqlite(path):
        # SQLite
        conn = sqlite3.connect(path)
        _init_sqlite(conn)
        
        cursor = conn.execute(
            """
            UPDATE trades
            SET exit_price = ?, exit_timestamp = ?, exit_reason = ?,
                mae_pct = ?, mfe_pct = ?, r_multiple = ?, pnl_usd = ?
            WHERE trade_id = ?
            """,
            (exit_price, now, exit_reason, mae_pct, mfe_pct, r_multiple, pnl_usd, trade_id),
        )
        conn.commit()
        
        if cursor.rowcount == 0:
            conn.close()
            return {"ok": False, "error": f"trade_id {trade_id} not found"}
        
        conn.close()
    else:
        # JSON
        if not Path(path).is_file():
            return {"ok": False, "error": "trade log not found"}
        
        try:
            trades = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"ok": False, "error": "failed to read trade log"}
        
        # Find and update
        found = False
        for t in trades:
            if t.get("trade_id") == trade_id:
                t["exit_price"] = exit_price
                t["exit_timestamp"] = now
                t["exit_reason"] = exit_reason
                t["mae_pct"] = mae_pct
                t["mfe_pct"] = mfe_pct
                t["r_multiple"] = r_multiple
                t["pnl_usd"] = pnl_usd
                found = True
                break
        
        if not found:
            return {"ok": False, "error": f"trade_id {trade_id} not found"}
        
        # Write back
        Path(path).write_text(json.dumps(trades, indent=2), encoding="utf-8")
    
    return {"ok": True, "trade_id": trade_id, "message": "exit logged"}


def get_all_trades() -> List[Dict[str, Any]]:
    """Get all trades from log.
    
    Returns:
        list of trade dicts
    """
    path = _log_path()
    
    if _is_sqlite(path):
        # SQLite
        if not Path(path).is_file():
            return []
        
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM trades ORDER BY entry_timestamp DESC")
        rows = cursor.fetchall()
        conn.close()
        
        return [dict(row) for row in rows]
    else:
        # JSON
        if not Path(path).is_file():
            return []
        
        try:
            trades = json.loads(Path(path).read_text(encoding="utf-8"))
            return trades
        except (OSError, json.JSONDecodeError):
            return []


def get_open_trades(dry_run: Optional[bool] = None) -> List[Dict[str, Any]]:
    """Get open trades (no exit timestamp).

    Args:
        dry_run: None = all; False = only real trades; True = only dry-run trades.
            The exit worker passes dry_run=(not live) so dry-run records are never
            matched to real HL positions.
    """
    all_trades = get_all_trades()
    open_trades = [t for t in all_trades if not t.get("exit_timestamp")]
    if dry_run is None:
        return open_trades
    return [t for t in open_trades if bool(t.get("dry_run")) == bool(dry_run)]


def clear_all_trades() -> Dict[str, Any]:
    """Clear all trades (for testing).
    
    Returns:
        dict with ok, message
    """
    path = Path(_log_path())
    
    if _is_sqlite(str(path)):
        if path.is_file():
            path.unlink()
            return {"ok": True, "message": "cleared SQLite database"}
    else:
        if path.is_file():
            path.unlink()
            return {"ok": True, "message": "cleared JSON log"}
    
    return {"ok": True, "message": "no log to clear"}


if __name__ == "__main__":
    # Example usage
    import sys
    
    # Test entry
    result = log_entry(
        trade_id="test_btc_20260926",
        symbol="BTC",
        entry_type="Base",
        tier="mega",
        trend_1d="Green",
        trend_4h="Green",
        sl_dist_pct=5.0,
        entry_price=60000,
        entry_size=0.1,
        entry_leverage=3.0,
        ai_decision_reason="Strong uptrend",
        dry_run=True,
    )
    print(f"Entry logged: {result}")
    
    # Test exit
    result = log_exit(
        trade_id="test_btc_20260926",
        exit_price=62000,
        exit_reason="4H filter cross down",
        mae_pct=-1.0,
        mfe_pct=5.0,
        r_multiple=2.0,
        pnl_usd=200.0,
    )
    print(f"Exit logged: {result}")
    
    # Get trades
    trades = get_all_trades()
    print(f"\nAll trades ({len(trades)}):")
    for t in trades:
        print(f"  {t['symbol']}: {t['entry_price']} -> {t['exit_price']} ({t['exit_reason']})")
    
    print(f"\nTrade log path: {TRADE_LOG_PATH}")
    print(f"Storage type: {'SQLite' if _is_sqlite(TRADE_LOG_PATH) else 'JSON'}")


def count_entries_today(now: Optional[datetime] = None, dry_run: bool = False) -> int:
    """GIIQ-SoT-3 daily cap: number of entries (Base / CONTINUATION / ADD_ON) logged today (HKT day).
    dry_run=False counts only real (LIVE) fills."""
    from datetime import timedelta
    now = now or datetime.now(timezone.utc)
    hkt = timezone(timedelta(hours=8))
    today = now.astimezone(hkt).date()
    n = 0
    for t in get_all_trades():
        if bool(t.get("dry_run")) != bool(dry_run):
            continue
        ts = t.get("entry_timestamp")
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt.astimezone(hkt).date() == today:
            n += 1
    return n
