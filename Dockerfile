# Alexandria: one image for the Prefect worker, the MCP server and the Prefect server; the command picks the role
# (see deploy/compose.yaml). It serves nothing by default.
#   docker build -t alexandria .                          # CUDA 12.8 PyTorch (needs the NVIDIA Container Toolkit to use the GPU)
#   docker build --build-arg TORCH=cpu -t alexandria:cpu .  # CPU-only PyTorch, much smaller
# Models are not baked in: they download on first start into $HF_HOME, which deploy/compose.yaml keeps in a volume.

ARG PYTHON=3.13-slim

FROM python:${PYTHON} AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv
# cpu or cu128: the root project's extra that picks the PyTorch build.
ARG TORCH=cu128
ENV UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3
WORKDIR /app

# .dockerignore lets in only the workspace's metadata and sources. Alexandria is installed as regular
# (non-editable) packages, so the venv stands on its own in the runtime stage.
COPY . .
RUN uv sync --locked --no-dev --extra "${TORCH}" --no-editable


FROM python:${PYTHON}
ARG TORCH=cu128
LABEL org.opencontainers.image.source="https://github.com/tingiskhan/alexandria" \
      org.opencontainers.image.description="Alexandria: a searchable document library for LLM agents over MCP" \
      org.opencontainers.image.licenses="MIT"

RUN useradd --create-home --uid 1000 alexandria \
    && mkdir -p /models /data/prefect \
    && chown alexandria:alexandria /models /data/prefect
COPY --from=build --chown=alexandria:alexandria /app/.venv /app/.venv

# HF_HOME holds the embedding model and Docling's models; PREFECT_HOME the Prefect server's SQLite database.
# Volumes mounted on these paths start out owned by `alexandria`, because the directories are.
ENV PATH="/app/.venv/bin:${PATH}" \
    HF_HOME=/models \
    PREFECT_HOME=/data/prefect \
    PYTHONUNBUFFERED=1 \
    ALEXANDRIA_TORCH=${TORCH}
USER alexandria
WORKDIR /home/alexandria

CMD ["alexandria-workflows", "--help"]
