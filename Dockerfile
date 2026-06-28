# syntax=docker/dockerfile:1

# Pure-stdlib Python app — nothing to install, just a small runtime.
FROM python:3.12-slim

LABEL org.opencontainers.image.title="stockpricer" \
      org.opencontainers.image.description="Realtime stock quote HTTP server (US / HK / CN), pure-stdlib, zero dependencies." \
      org.opencontainers.image.source="https://github.com/SoulBasic/stockpricer" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8849

WORKDIR /app

# Only the source is needed (no third-party dependencies).
COPY stock_server.py q.py ./

# Drop root privileges.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8849

# Liveness via the built-in /health endpoint (no curl needed in slim image).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8849')+'/health',timeout=4).getcode()==200 else 1)"

CMD ["python3", "stock_server.py"]
