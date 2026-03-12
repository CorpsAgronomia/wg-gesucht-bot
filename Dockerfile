FROM python:3.11

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY . .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/auth /app/logs/screenshots /ms-playwright \
    && chown -R appuser:appuser /app /ms-playwright

USER appuser

HEALTHCHECK --interval=5m --timeout=10s --start-period=2m --retries=3 CMD python -c "import datetime, json, pathlib, sys; p = pathlib.Path('/app/logs/metrics.json'); \
data = json.loads(p.read_text()) if p.exists() else {}; ts = data.get('last_heartbeat_at'); \
sys.exit(0 if ts and (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(ts)).total_seconds() < 900 else 1)"

CMD ["python", "main.py"]
