# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

# Standard, reproducible Python container settings
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps kept minimal — psycopg[binary] ships its own libpq.
# curl is used by the compose healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first for better layer caching
COPY requirements.txt .
RUN pip install -r requirements.txt

# Application code
COPY . .

# Run as a non-root user (security best practice)
RUN adduser --disabled-password --gecos "" appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Default = production server. docker-compose overrides this for local dev.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
