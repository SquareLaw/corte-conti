# Microsoft's official Playwright image ships Chromium (and Firefox/WebKit)
# with every system library they need already installed - this sidesteps
# the "needs root to apt-get install dependencies" problem that broke the
# plain `playwright install --with-deps` approach on Render's build.
FROM mcr.microsoft.com/playwright/python:v1.63.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static/ static/

# Render sets $PORT at runtime; default to 10000 for local testing
ENV PORT=10000
CMD uvicorn app:app --host 0.0.0.0 --port $PORT
