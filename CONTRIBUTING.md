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

CI runs the same checks. If you changed extraction or embedding and have an NVIDIA GPU, also run `uv run --extra cu128 pytest -m gpu`.

## How the code is organised

Read [docs/architecture.md](docs/architecture.md) first. In short:

- Every swappable dependency is a `Protocol` in `alexandria_core.protocols`. Depend on the protocol, not on a concrete class.
- Dependencies are passed as arguments. Real objects are built only in the composition roots: `build_pipeline()` in `alexandria_workflows.flows` and `main()` in `alexandria_mcp.main`.
- Only `alexandria_workflows` imports Prefect.
- Tests use hand-written fakes (each package's `tests/fakes.py`), not mocks.
- Public modules, classes and functions need docstrings (Google style) and full type annotations; ruff enforces both.
- Settings are pydantic-settings classes built with `settings_config("ALEXANDRIA_<COMPONENT>_")`. Document new settings in the root README's table and in `.env.example`.

To add a file source, follow [Adding a source](packages/ingest/README.md#adding-a-source).

## Commits and pull requests

Keep each pull request to one change, with tests, and explain why the change is needed as well as what it does. Release notes are generated from the titles of merged pull requests, so write the title for a user ("Add a SharePoint source", not "Refactor sources"). If a change needs action when upgrading, such as a schema change, a new required setting or a full re-listing on the first sync, add the `breaking` label and say what to do in the description.

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
