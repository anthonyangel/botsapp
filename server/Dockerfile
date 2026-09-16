FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
COPY start.sh ./
RUN uv sync --frozen --no-dev && chmod +x start.sh

EXPOSE 8000
EXPOSE 8100

# Default is the public MCP server alone; docker-compose overrides `command:`
# for the admin-ui service to run botsapp.admin_main:app on 8100 instead.
# fly.toml overrides this a third way — start.sh, running both in one
# container (see docs/decisions/0013).
CMD ["uv", "run", "uvicorn", "botsapp.main:app", "--host", "0.0.0.0", "--port", "8000"]
