# --no-sync, as in CI: `uv run --extra ...` would swap PyTorch to that extra's build.
RUN := uv run --no-sync

.PHONY: lint format format-and-lint

lint:
	$(RUN) ruff check
	$(RUN) ruff format --check
	$(RUN) ty check

format:
	$(RUN) ruff check --fix-only
	$(RUN) ruff format

format-and-lint:
	$(MAKE) format
	$(MAKE) lint
