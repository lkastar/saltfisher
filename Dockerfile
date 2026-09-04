# The panel is served by the API process (no nginx), so the build output has
# to get into the image. A separate stage keeps node and node_modules out of
# the final layer -- only dist/ is copied across.
FROM node:24-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# Playwright's image ships the browsers and their system libraries, which is
# the whole reason to use it — installing those by hand is a long apt list
# that breaks on every base-image bump.
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependency layer, cached across source edits.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY app ./app
RUN uv sync --locked --no-dev

# app/main.py looks for web/dist next to the app package.
COPY --from=web /web/dist ./web/dist

ENV PATH="/app/.venv/bin:$PATH" SFD_DATA_DIR=/app/data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
