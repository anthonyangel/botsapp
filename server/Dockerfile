FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

COPY --from=ghcr.io/astral-sh/uv:latest@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv
# Caddy (docs/decisions/0028): fronts mcp-server and admin-ui on one Fly
# port; see start.sh and Caddyfile. A static binary copied from the official
# image, not installed via apt — no package manager setup, no extra layer.
COPY --from=caddy:2.11.9@sha256:0c2243efcf437ab7f7a978f1027fcf0d4f9be8a75df27b1b3fbc5dbe0ffa8cf0 /usr/bin/caddy /usr/local/bin/caddy

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
COPY start.sh Caddyfile ./
RUN uv sync --frozen --no-dev && chmod +x start.sh

EXPOSE 8000
EXPOSE 8100

# Default is the public MCP server alone; docker-compose overrides `command:`
# for the admin-ui service to run botsapp.admin_main:app on 8100 instead.
# fly.toml overrides this a third way — start.sh, running both in one
# container (see docs/decisions/0013).
CMD ["uv", "run", "uvicorn", "botsapp.main:app", "--host", "0.0.0.0", "--port", "8000"]
