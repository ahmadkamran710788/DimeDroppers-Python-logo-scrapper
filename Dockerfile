# Playwright's own image, not python:slim + `playwright install --with-deps`.
# Chromium needs ~60 system libs and installing them requires root, which Render's
# native Python runtime does not grant. This image already has them.
FROM mcr.microsoft.com/playwright/python:v1.55.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install chromium

COPY . .

# Render injects $PORT; the shell form is required so it expands.
CMD uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
