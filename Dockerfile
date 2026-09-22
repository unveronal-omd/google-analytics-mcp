FROM python:3.12-slim

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir .

ENV MCP_TRANSPORT=http
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "analytics_mcp.server"]
