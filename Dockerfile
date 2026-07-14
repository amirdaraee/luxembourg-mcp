FROM python:3.12-slim
RUN addgroup --system app && adduser --system --ingroup app app
WORKDIR /app
COPY --chown=app:app pyproject.toml README.md ./
COPY --chown=app:app src ./src
RUN pip install --no-cache-dir .
USER app
EXPOSE 8000
# MCP convention: stdio by default (introspection tools, Docker MCP clients).
# Pass args for the HTTP transport, as the Cloudflare deployment does:
#   docker run -p 8000:8000 luxembourg-mcp --transport http --host 0.0.0.0
ENTRYPOINT ["luxembourg-mcp"]
