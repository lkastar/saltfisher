# The panel is served by the API process (no nginx), so the build output has
# to get into the image. A separate stage keeps node and node_modules out of
# the final layer -- only dist/ is copied across.
FROM node:24-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# Playwright's image is used for its SYSTEM LIBRARIES, not for the browsers it
# ships. The browsers are installed below from the locked playwright package
# instead.
#
# Why: the image tag and the version in uv.lock are two independent sources of
# the same number, and they drifted. Found in T8 -- the container had never
# started: the image carried browsers for 1.56 while the lock resolved
# playwright 1.62, and launch died on
# "Executable doesn't exist at /ms-playwright/chromium_headless_shell-...".
# Installing the browser from the installed package makes a mismatch
# impossible rather than something to remember.
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependency layer, cached across source edits.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY app ./app
RUN uv sync --locked --no-dev

# The system dependencies already come from the base image, so only the
# browser binary is downloaded here -- and it is the one this exact playwright
# build expects.
RUN uv run playwright install chromium

# app/main.py looks for web/dist next to the app package.
COPY --from=web /web/dist ./web/dist

ENV PATH="/app/.venv/bin:$PATH" SFD_DATA_DIR=/app/data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
