FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY app ./app
COPY schema ./schema
COPY scripts/storage_migrate.py ./scripts/storage_migrate.py
COPY run.py worker.py config.example.json ./

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /var/lib/dola-fetch-service \
    && chown -R appuser:appuser /app /var/lib/dola-fetch-service /ms-playwright

USER appuser

EXPOSE 8088

CMD ["python", "run.py"]
