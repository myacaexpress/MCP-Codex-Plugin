# MedCare Resources remote MCP server (ChatGPT). Build from the repository root:
#   docker build -f remote/Dockerfile -t medcare-resources-remote .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    MEDCARE_CACHE_DIR=/data/cache MEDCARE_LOG_TRACEBACKS=0 PORT=8080

RUN useradd --system --uid 10001 --home /app medcare && mkdir -p /data/cache && chown -R medcare /data
WORKDIR /app
COPY remote/requirements.txt remote/requirements.txt
RUN pip install --no-cache-dir -r remote/requirements.txt
COPY plugins/medcare-resources plugins/medcare-resources
COPY remote remote

USER medcare
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ.get('PORT','8080'),timeout=4)"
CMD ["python", "-m", "remote.medcare_http"]
