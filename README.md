# Support Resolution Agent (SIMULATED BUSINESS)

A portfolio project: a customer-support resolution agent for a **fictional** subscription-commerce company. All customers, orders, policies and payments are synthetic. Design rules and conventions are in `CLAUDE.md`; the full design is in `docs/SPEC.md`.

## Quickstart (local)

```
cp .env.example .env
docker compose up -d --build     # Postgres 17 + pgvector, and the backend on :8000
curl localhost:8000/health
```

Seed the synthetic dataset (idempotent, deterministic; after `alembic upgrade head`): `cd backend && uv run python -m app.seed`.

Backend development: `cd backend && uv sync && uv run pytest`. Python 3.12 is provisioned by uv.

`uv sync` needs the network once after the uv cache has been cleared (for example after reclaiming disk space), because the project is built with `hatchling`, which is fetched from the package index. `uv run --offline` fails with "hatchling was not found in the cache" until a networked `uv sync` has repopulated it.

Building the image by hand needs the policy corpus as a named build context: `docker build --build-context policies=./policies -t support-backend ./backend` (docker compose does this for you).

## Known limitations

- **Business days** are Monday to Friday with no holiday calendar. A holiday calendar would be more correct but adds no engineering signal here; the simplification is defined in one module and applies to the shipping window and presumed-lost date.
- All data is synthetic. Model and cost figures are projections for a paid tier, not incurred cost.
- arm64 support is checked from published image manifests and wheel metadata; the first native arm64 build happens at the M4 deployment.
- Free-tier model availability and rate limits change; the provider adapter layer absorbs this.
