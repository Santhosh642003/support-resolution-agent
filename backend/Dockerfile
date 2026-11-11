# Architecture-neutral: builds on linux/amd64 and linux/arm64 without changes.
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv

# UV_NO_CACHE: do not leave uv's download cache (about 200 MB) in an image layer.
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

# Dependencies first for layer caching.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
# Migrations ship in the image so the deployment target can run `alembic upgrade head`.
COPY alembic.ini ./
COPY alembic ./alembic
RUN uv sync --frozen --no-dev

# Policy corpus (a named build context, so the repo root and its untracked files are never sent to
# the daemon). Lands at /policies because the loader resolves the corpus as <parents[3]>/policies,
# which is "/" for /srv/app/retrieval/policy_loader.py. Lets the ingest CLI run in the container.
COPY --from=policies . /policies

# Pinned embedding model; the build FAILS on a SHA-256 mismatch (no warn-and-continue).
RUN /srv/.venv/bin/python -m app.retrieval.fetch_model

RUN useradd --system --uid 10001 appuser
USER appuser
EXPOSE 8000
CMD ["/srv/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
