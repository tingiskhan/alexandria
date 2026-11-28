# Contributing

Thanks for your interest in Alexandria. Bug reports, fixes and new file sources are all welcome. For anything bigger than a small fix, please open an issue first so we can agree on the approach before you write the code.

## Setup

```bash
uv sync --extra cpu        # or --extra cu128 on an NVIDIA GPU
```

Pass the same `--extra` to `uv run` (or use `uv run --no-sync`), otherwise uv switches the environment back to PyPI's PyTorch build. See [Choosing a PyTorch build](README.md#choosing-a-pytorch-build).

Integration tests start Postgres and RustFS containers, so they need Docker.

## Before you open a pull request

```bash
uv run --extra cpu ruff check
uv run --extra cpu ruff format --check
uv run --extra cpu ty check
uv run --extra cpu pytest --cov          # fails below 95% coverage
```

CI runs the same checks. `make lint` runs the first three, `make format` applies ruff's fixes and formatting, and `make format-and-lint` does both. If you changed extraction or embedding and have an NVIDIA GPU, also run `uv run --extra cu128 pytest -m gpu`.

If you changed anything that affects search results (chunking, metadata, the search query, the embedding model), run `make eval` against your library before and after, and paste both tables into the pull request; see [Evaluating retrieval](README.md#evaluating-retrieval).

## How the code is organised

Read [docs/architecture.md](docs/architecture.md) first. In short:

- Every swappable dependency is a `Protocol` in `alexandria_core.protocols`. Depend on the protocol, not on a concrete class.
- Dependencies are passed as arguments. Real objects are built only in the composition roots: `build_pipeline()` in `alexandria_workflows.flows` and `main()` in `alexandria_mcp.main`.
- Only `alexandria_workflows` imports Prefect.
- Tests use hand-written fakes (each package's `tests/fakes.py`), not mocks.
- Public modules, classes and functions need docstrings (Google style) and full type annotations; ruff enforces both.
- Settings are pydantic-settings classes built with `settings_config("ALEXANDRIA_<COMPONENT>_")`. Document new settings in the root README's table and in `.env.example`.

To add a file source, follow [Adding a source](packages/ingest/README.md#adding-a-source).

## Adding a migration

The schema lives in two places that must agree: the ORM classes in `alexandria_core/db.py` and the Alembic migrations in `packages/core/src/alexandria_core/migrations/versions/`. To change it:

1. Change the classes in `db.py`.
2. Run `make migration m="add tags"`. It starts a throwaway Postgres container, upgrades it to the newest revision, and has Alembic compare it with the classes, writing the difference to the next numbered revision: `0004_add_tags.py`. It never touches the database in your `.env`. Without Docker, `uv run --no-sync alexandria-db revision -m "add tags" [--autogenerate]` does the same against `ALEXANDRIA_DATABASE_URL`, which must then be a scratch database at head.
3. Review the revision by hand. Autogenerate misses or gets wrong some changes: renames (it writes a drop and an add, losing the data), data migrations, `CREATE EXTENSION`, unnamed constraints (name them, or `downgrade` can't drop them), and the vector dimension or text search configuration, which should be written out rather than imported from `db.py` so the revision keeps doing what it did. Write the `downgrade` too.
4. Run the tests: `test_a_new_database_is_migrated_to_exactly_the_models` fails while the migrations and the classes disagree.

Revision ids are the next four-digit number and a snake_case description of the change (`0004_add_tags`), at most 32 characters, and the file is named after the id. `alexandria-db revision` picks the id from the message; don't rename it afterwards. Never edit a revision that has been released: add a new one. Mark the pull request `breaking` if the migration needs more than starting the new release, e.g. a re-index.

## Commits and pull requests

Keep each pull request to one change, with tests, and explain why the change is needed as well as what it does. Release notes are generated from the titles of merged pull requests, so write the title for a user ("Add a SharePoint source", not "Refactor sources"). If a change needs action when upgrading, such as a migration that needs a re-index, a new required setting or a full re-listing on the first sync, add the `breaking` label and say what to do in the description.

## Releasing

Tag a commit on `main` with a full version, `vMAJOR.MINOR.PATCH` (`v0.2.0`; `v0.2.0-rc.1` for a pre-release), and push the tag:

```bash
git tag v0.2.0 && git push origin v0.2.0
```

The Release workflow builds both image variants, pushes them to GHCR as `0.2.0`, `0.2` and `latest` (with `-cpu` appended for the CPU variant), and publishes a GitHub release with `compose.yaml` and `env.example`. Any other tag starting with `v`, such as `v0.2`, fails the workflow's first job before anything is built.

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
