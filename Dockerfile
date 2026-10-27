# syntax=docker/dockerfile:1.7

# ---- stage 1: build the dashboard stylesheet (Tailwind CSS v4) --------------------------------
FROM node:22-alpine AS css
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY src/jll_harvester/web ./src/jll_harvester/web
RUN npm run build:css

# ---- stage 2: Python application ---------------------------------------------------------------
FROM python:3.12-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    IRIS_RAW_SNAPSHOT_DIR=/app/var/raw

RUN useradd --create-home --uid 10001 iris
WORKDIR /app

# dependency layer first (better caching), then the package itself
COPY pyproject.toml README.md ./
RUN mkdir -p src/jll_harvester && touch src/jll_harvester/__init__.py \
    && pip install ".[dev]" && pip uninstall -y jll-harvester

COPY src ./src
COPY --from=css /build/src/jll_harvester/web/static/dist/app.css ./src/jll_harvester/web/static/dist/app.css
RUN pip install --no-deps . && rm -rf build

COPY tests ./tests
COPY scripts ./scripts
RUN mkdir -p /app/var/raw /app/samples && chown -R iris:iris /app/var /app/samples
USER iris

# git commit of this build (set by CI) — last, so a new commit doesn't invalidate the layers above
ARG IRIS_REVISION=dev
ENV IRIS_REVISION=${IRIS_REVISION}

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)" || exit 1

CMD ["jll-harvester", "serve", "--host", "0.0.0.0", "--port", "8000"]
