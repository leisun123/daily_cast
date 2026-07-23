# Contributing to DailyCast

Thanks for contributing. DailyCast is a review-gated personal podcast system, so changes should preserve deterministic processing, durable task state, and safe publication boundaries.

## Development Setup

Use Python 3.12 and Poetry.

```bash
git clone https://github.com/<your-account>/dailycast.git
cd dailycast
cp .env.example .env
poetry install --with dev
poetry run alembic upgrade head
```

Do not put API keys, cookies, tokens, account credentials, generated media, SQLite databases, or runtime artifacts in commits. Keep secrets in `.env` or deployment-provided environment variables only.

## Branch Strategy

- Keep `main` releasable.
- Branch from current `main` using `feat/<short-name>`, `fix/<short-name>`, `docs/<short-name>`, or `chore/<short-name>`.
- Keep one focused concern per pull request and rebase on current `main` before requesting review.
- Do not commit directly to `main` unless the repository maintainer explicitly authorizes it.

## Testing and Quality Requirements

Run these before opening a pull request:

```bash
poetry run ruff check .
poetry run black --check .
poetry run mypy src
poetry run pytest -q
docker compose config -q
```

Run `docker compose build` when Docker, dependencies, or startup configuration changes. Run the opt-in Compose startup test when a change can affect boot, migration, volumes, ports, or health checks:

```bash
DAILYCAST_RUN_DOCKER_TEST=1 poetry run pytest -q tests/integration/test_docker_startup.py
```

Schema changes require an Alembic revision and migration coverage. Do not add `Base.metadata.create_all()` as an alternative startup path.

## Pull Request Expectations

Every pull request should include:

- a concise problem and solution summary;
- tests that cover changed behavior, including failure and recovery behavior where relevant;
- documentation or configuration-example updates when user-visible behavior changes;
- a note of the commands run and their results;
- confirmation that no secret, generated runtime artifact, or unrelated reformatting is included.

Keep routes thin, preserve the modular-monolith boundaries described in `docs/design.md`, and isolate external providers behind their existing contracts. Do not introduce Redis, Celery, Node.js frontends, microservices, or future-provider placeholders without an approved design change.
