"""Run persistence: one JSON line on stdout (always), plus an INSERT into raw.ops_check_run when BRAIN_DSN is set.
Insert-only: no UPDATE / DELETE / DDL; create the table once with ops_cron/schema.sql."""
from __future__ import annotations

import json
import sys
from typing import Any, Dict

INSERT_SQL = ("INSERT INTO raw.ops_check_run (run_id, check_name, run_at, status, n_problems, summary, alerted, report) "
              "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)")


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
