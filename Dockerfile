FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg gosu \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "poetry==2.4.1"

WORKDIR /app

COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --only main --no-root
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

COPY alembic.ini ./
COPY config ./config
COPY migrations ./migrations
COPY src ./src
COPY docker-entrypoint.sh ./

RUN chmod +x /app/docker-entrypoint.sh \
    && poetry install --only main \
    && mkdir -p /app/data /app/public

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn dailycast.main:app --host 0.0.0.0 --port \"${PORT:-8000}\" --workers 1"]
