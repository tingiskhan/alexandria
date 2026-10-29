"""Keep the release's deploy/.env.example in step with the settings the code and docs know about."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SETTING = re.compile(r"^#?\s*(ALEXANDRIA_[A-Z0-9_]+)=", re.MULTILINE)


def settings_in(path: Path) -> set[str]:
    return set(SETTING.findall(path.read_text()))


def test_every_deploy_setting_is_documented() -> None:
    deploy = settings_in(ROOT / "deploy" / ".env.example") - {"ALEXANDRIA_VERSION"}  # read by compose, not Alexandria
    readme = (ROOT / "README.md").read_text()

    assert deploy, "no settings found in deploy/.env.example"
    assert deploy - settings_in(ROOT / ".env.example") == set()
    assert {name for name in deploy if f"`{name}`" not in readme} == set()


def test_compose_runs_the_image_for_every_role() -> None:
    compose = (ROOT / "deploy" / "compose.yaml").read_text()

    assert "ghcr.io/tingiskhan/alexandria:${ALEXANDRIA_VERSION:-latest}" in compose  # the release job pins this
    for command in ('"alexandria-workflows", "serve"', '"alexandria-mcp", "--transport"', '"prefect", "server"'):
        assert command in compose
