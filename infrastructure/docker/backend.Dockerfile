# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.8.22 AS uv
FROM python:3.13.9-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=uv /uv /usr/local/bin/uv

RUN useradd --create-home --uid 10001 appuser

# LightGBM's manylinux CPU wheel links against the GNU OpenMP runtime. The slim base does not
# provide it; install only the runtime library, not compilers, OpenCL, CUDA, or GPU tooling.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/apps/backend

COPY apps/backend/pyproject.toml apps/backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY apps/backend/src ./src
COPY apps/backend/README.md ./README.md
RUN uv sync --frozen --no-dev

USER appuser

EXPOSE 8000

CMD [".venv/bin/bitcoin-intel-api"]
