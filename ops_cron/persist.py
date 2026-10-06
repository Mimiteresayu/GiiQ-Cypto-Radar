"""Run persistence: one JSON line on stdout (always).

Brain writes are insert-only. CREATE TABLE IF NOT EXISTS runs when a job writes a report table
and the table is missing. No UPDATE and no DELETE.

DSN: BRAIN_DATABASE_URL, else BRAIN_DSN (older name).
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

INSERT_SQL = ("INSERT INTO raw.ops_check_run (run_id, check_name, run_at, status, n_problems, summary, alerted, report) "
              "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)")

TABLES = (
    "raw.ops_check_run",
    "raw.river_c48_ft_score",
    "raw.river_desk_veto",
    "raw.river_trade_log",
    "raw.bo_live_report",
    "raw.harbor_pnl_daily",
)
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def json_line(report: Dict[str, Any]) -> str:
    return json.dumps(report, default=str, ensure_ascii=False, separators=(",", ":"))


def emit(report: Dict[str, Any]) -> None:
    sys.stdout.write(json_line(report) + "\n")
    sys.stdout.flush()


def _connect(dsn: str):
    try:
        import psycopg
        return psycopg.connect(dsn, connect_timeout=15)
    except ImportError:
        import psycopg2
        return psycopg2.connect(dsn, connect_timeout=15)


def _jsonish(v: Any) -> Any:
    if not isinstance(v, (dict, list)):
        return v
    try:
        from psycopg.types.json import Jsonb
        return Jsonb(v)
    except Exception:  # noqa: BLE001
        return json.dumps(v, default=str)


def brain_dsn(env: Mapping[str, str]) -> str:
    return (env.get("BRAIN_DATABASE_URL") or env.get("BRAIN_DSN") or "").strip()


def _schema_statements() -> List[str]:
    text = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    parts = []
    buf: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            stmt = "\n".join(buf).strip()
            buf = []
            if stmt.upper().startswith("CREATE "):
                parts.append(stmt)
    return parts


def ensure_tables(dsn: str) -> str:
    """CREATE TABLE IF NOT EXISTS for the ops_cron tables. -> "ok" | "error: ..."."""
    try:
        conn = _connect(dsn)
        try:
            with conn.cursor() as cur:
                for stmt in _schema_statements():
                    if not stmt.upper().startswith("CREATE "):
                        raise RuntimeError("schema.sql has a non-CREATE statement")
                    cur.execute(stmt)
            conn.commit()
        finally:
            conn.close()
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"error: {type(e).__name__}: {str(e).replace(dsn, '***')[:160]}"


def insert_rows(dsn: str, table: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """Insert-only into one allow-listed table. Creates tables first if they are missing."""
    if table not in TABLES:
        return "error: table not allow-listed"
    if any(not _IDENT.match(c) for c in columns):
        return "error: bad column"
    if not rows:
        return "ok"
    made = ensure_tables(dsn)
    if made != "ok":
        return made
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    if not sql.upper().startswith("INSERT INTO "):
        return "error: refused"
    upper = sql.upper()
    if "UPDATE " in upper or "DELETE " in upper:
        return "error: refused"
    try:
        conn = _connect(dsn)
        try:
            with conn.cursor() as cur:
                writing = list(rows)
                if table == "raw.river_trade_log":
                    writing = _skip_known_trade_fills(cur, columns, writing)
                for row in writing:
                    cur.execute(sql, tuple(_jsonish(v) for v in row))
            conn.commit()
        finally:
            conn.close()
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"error: {type(e).__name__}: {str(e).replace(dsn, '***')[:160]}"


# Watermark is the newest fill already stored for this account. ops_check_run status is not consulted:
# a problem run must still advance, and a failed trade_log write must not.
JOURNAL_FILL_WATERMARK_SQL = (
    "SELECT MAX(trade_time) FROM raw.river_trade_log "
    "WHERE report->>'kind' = 'fill' "
    "AND COALESCE(report->>'account', %s) = %s"
)
JOURNAL_FILL_IDS_SQL = (
    "SELECT report->>'fill_id' FROM raw.river_trade_log "
    "WHERE report->>'kind' = 'fill' AND report->>'fill_id' IS NOT NULL "
    "AND COALESCE(report->>'account', %s) = %s"
)


def _as_utc(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def last_journal_fill_time(dsn: str, account: str) -> Optional[datetime]:
    """Max fill time stored in raw.river_trade_log for this account. None on a first run or a read error."""
    if not dsn or not JOURNAL_FILL_WATERMARK_SQL.upper().startswith("SELECT "):
        return None
    if "ops_check_run" in JOURNAL_FILL_WATERMARK_SQL or "status" in JOURNAL_FILL_WATERMARK_SQL:
        return None
    try:
        conn = _connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(JOURNAL_FILL_WATERMARK_SQL, (account, account))
                row = cur.fetchone()
                return _as_utc(row[0] if row else None)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def existing_journal_fill_ids(dsn: str, account: str) -> Optional[set]:
    """Fill ids already stored. None when the read fails (do not insert; the next run retries)."""
    if not dsn or not JOURNAL_FILL_IDS_SQL.upper().startswith("SELECT "):
        return None
    try:
        conn = _connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(JOURNAL_FILL_IDS_SQL, (account, account))
                return {r[0] for r in cur.fetchall() if r and r[0]}
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def _skip_known_trade_fills(cur, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> List[Sequence[Any]]:
    """Insert-only dedupe. Rows whose report fill_id is already stored are skipped."""
    if "report" not in columns:
        return list(rows)
    idx = list(columns).index("report")
    cur.execute(
        "SELECT report->>'fill_id' FROM raw.river_trade_log "
        "WHERE report->>'kind' = 'fill' AND report->>'fill_id' IS NOT NULL")
    known = {r[0] for r in cur.fetchall() if r and r[0]}
    kept = []
    for row in rows:
        rep = row[idx] if idx < len(row) else None
        fid = rep.get("fill_id") if isinstance(rep, dict) else None
        if fid and fid in known:
            continue
        if fid:
            known.add(fid)
        kept.append(row)
    return kept


def insert(report: Dict[str, Any], dsn: str) -> str:
    """-> "ok" | "error: <type>: <msg>" (the DSN is never included)."""
    row = (report["run_id"], report["check"], report["run_at"], report["status"], len(report.get("problems") or []),
           report.get("summary") or "", bool(report.get("alerted")), json_line(report))
    try:
        conn = _connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(INSERT_SQL, row)
            conn.commit()
        finally:
            conn.close()
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"error: {type(e).__name__}: {str(e).replace(dsn, '***')[:160]}"
