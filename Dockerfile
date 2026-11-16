# Company Website Research API
#
# Single image containing FastAPI, the crawler and Chromium.  Architecture
# reference: SS14, "Approved deployment target -- VPS + Docker + Caddy".
#
# The image is ~1.2 GB because of Chromium and its system libraries.  When the
# renderer is later extracted into its own service (SS1, the renderer adapter),
# this Dockerfile splits in two and the API image drops to ~200 MB.

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers

WORKDIR /srv/app

# Certificates are required for TLS verification of the sites being researched.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tini libatomic1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Chromium plus its system dependencies.  Installed as root, then made readable
# by the unprivileged runtime user.
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /opt/pw-browsers \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY ad_research ./ad_research
COPY scripts ./scripts

# Run unprivileged.  The Chromium sandbox is kept enabled; the container itself
# provides the outer isolation layer, and the egress firewall rules described in
# the README are what constrain the browser's network reach (SS12).
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /srv/app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/v1/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]

# Two workers, not 2 x CPU: worker count here is bounded by Chromium memory,
# not by CPU (SS14).  Drop WEB_CONCURRENCY to 1 on a container under ~2 GB.
#
# PORT is honoured because platforms such as Railway inject it; it defaults to
# 8000 so `docker run` and docker-compose behave unchanged.
CMD ["sh", "-c", "if [ \"${SERVICE_MODE:-company-research}\" = \"ad-research\" ]; then exec uvicorn ad_research.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --timeout-keep-alive 80; else exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --timeout-keep-alive 65 --no-access-log; fi"]
