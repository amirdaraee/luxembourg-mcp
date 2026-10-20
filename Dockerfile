# Pinned by digest: wrangler builds on the local Docker daemon, which never
# re-pulls a cached tag, so a floating tag can silently ship a stale base.
# Dependabot (docker ecosystem) keeps this digest current.
FROM python:3.15-slim@sha256:2da676046630d7bf14c6764ae1143dc7fa0377a53f94bf5a642f0b214aca5bce
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN addgroup --system app && adduser --system --ingroup app app
WORKDIR /app
# Sources and the installed package stay root-owned, so the unprivileged
# runtime user cannot modify the code it executes.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --root-user-action=ignore . && rm -rf /app/src /app/build
USER app
EXPOSE 8000
# MCP convention: stdio by default (introspection tools, Docker MCP clients).
# Pass args for the HTTP transport, as the Cloudflare deployment does:
#   docker run -p 8000:8000 luxembourg-mcp --transport http --host 0.0.0.0
ENTRYPOINT ["luxembourg-mcp"]
