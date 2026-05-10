# Stage 1: frontend
FROM node:22-alpine AS frontend

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build

# Stage 2: Python builder
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY pyproject.toml README.md ./
COPY tradingagents/ ./tradingagents/
COPY cli/ ./cli/
COPY run_web.py run_pipeline.py main.py ./

RUN pip install --no-cache-dir .

# Stage 3: runtime
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRADINGAGENTS_HOME=/data \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data

WORKDIR /home/appuser/app

COPY --from=builder --chown=appuser:appuser /build/run_web.py /build/run_pipeline.py /build/main.py ./
COPY --from=builder --chown=appuser:appuser /build/tradingagents ./tradingagents
COPY --from=builder --chown=appuser:appuser /build/cli ./cli

COPY --from=frontend --chown=appuser:appuser /app/frontend/dist ./frontend/dist

USER appuser

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/pipeline/state || exit 1

CMD ["uvicorn", "run_web:app", "--host", "0.0.0.0", "--port", "8000"]
