"""pytest config.

- Unit-test radar fixtures have a handful of rows, so the production radar row-count guardrail
  (exec_common.RADAR_MIN_ROWS) is disabled by default for the suite; test_guardrails.py
  re-enables it explicitly.
- Run reports / pending store go to a temp dir so tests never write into out/ (the Railway
  volume layout).
- CONTINUATION / ADD_ON pendings are disabled by default in production (Cove HEALTH FAIL
  2026-10-05); the suite runs the re-enabled path (PENDING_CONTINUATION_DISABLED=0) so those rules
  stay covered. test_pending_disabled.py covers the disabled default explicitly."""
import os
import tempfile

os.environ.setdefault("EXEC_RADAR_MIN_ROWS", "0")
_TMP = tempfile.mkdtemp(prefix="otr-tests-")
os.environ.setdefault("RUN_REPORT_DIR", os.path.join(_TMP, "run_reports"))
os.environ.setdefault("PENDING_PATH", os.path.join(_TMP, "pending_entries.json"))
os.environ.setdefault("PENDING_CONTINUATION_DISABLED", "0")
