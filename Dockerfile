FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY app/ ./app/
COPY scraper/ ./scraper/
COPY analyzer/ ./analyzer/
COPY agent/ ./agent/
COPY ui/ ./ui/

# Jinja2Templates(directory="ui/templates") is relative -- the app must run from /app.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin bot \
    && chown -R bot:bot /app
USER 10001

EXPOSE 8080

# One image, two entrypoints:
#   collector (Deployment):  default CMD below
#   node agent (DaemonSet):  command: ["python", "-m", "agent.node_agent"]
CMD ["python", "main.py"]
