FROM python:3.13-slim@sha256:27b7c9d4f13ca20bb1170148f02095d50a5dfce1f7f2dabf5ca273727e43b7ea

COPY --from=ghcr.io/astral-sh/uv:latest@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /usr/local/bin/uv

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
