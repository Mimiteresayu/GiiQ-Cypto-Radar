FROM python:3.12-slim
WORKDIR /app
# Install dependencies (hyperliquid-python-sdk for fail-safe worker)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Scanner is stdlib-only (urllib fallback); bake it so Railway self-refreshes without Harbor.
COPY serve.py ui.html scan_gc_radar.py mcap_tiers.py failsafe_exit_worker.py entry_candidates.py ./
# Auto-execution system modules (added 2026-09-27 for PR #6)
COPY executor.py exit_worker.py trade_log.py decisions.py exec_common.py hl_exec.py live_radar.py exec_preflight.py ./
COPY data/ ./data/
# Bake narrative watchlist fallback (Harbor may overwrite via /api/sync)
RUN mkdir -p out narrative
COPY narrative/watchlist.json ./narrative/watchlist.json
# Do NOT COPY out/*.json — runtime data lives on volume / Harbor /api/sync
ENV PORT=8080 HOST=0.0.0.0 RAILWAY=1 OTR_SCHEDULER=1
EXPOSE 8080
CMD ["python", "serve.py"]
