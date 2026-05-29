# ── Base image ──────────────────────────────────────────────────────────────
# python:3.12-slim works on both arm64 (Pi 4/5) and armv7 (Pi 3).
# Docker will pull the correct variant automatically.
FROM python:3.12-slim

# ── System deps needed to build/install RPi.GPIO ────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python deps ─────────────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ─────────────────────────────────────────────────────────
COPY main.py .

# ── Run ─────────────────────────────────────────────────────────────────────
CMD ["python", "-u", "main.py"]