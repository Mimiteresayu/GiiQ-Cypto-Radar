FROM python:3.12-slim
WORKDIR /app
COPY ops_cron/requirements.txt ./ops_cron/requirements.txt
RUN pip install --no-cache-dir -r ops_cron/requirements.txt
# Only ops_cron/ is baked: no trading, signing or exchange-key code is in this image.
COPY ops_cron/ ./ops_cron/
ENV PYTHONUNBUFFERED=1
# The Railway start command picks the job; see ops_cron/railway.*.json
CMD ["python", "-m", "ops_cron.main", "exit-monitor"]
