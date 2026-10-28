from dataclasses import dataclass
from typing import Any

import pytest

from alexandria_core.db import EMBEDDING_DIMENSION
from alexandria_mcp import main as module
from alexandria_mcp.main import Transport, main, run


@dataclass
class FakeServer:
    store: Any
    embedder: Any
    transport: str | None = None

    def run(self, transport: str) -> None:
        self.transport = transport


class FakeEmbedder:
    dimension = EMBEDDING_DIMENSION

    def __init__(self, model: str, revision: str, device: str | None = None) -> None:
        self.model_name = model
        self.device = device


@pytest.fixture
def servers(monkeypatch: pytest.MonkeyPatch) -> list[FakeServer]:
    monkeypatch.setenv("ALEXANDRIA_DATABASE_URL", "postgresql+psycopg://user:secret@db/alexandria")
    monkeypatch.setattr(module, "SentenceTransformerEmbedder", FakeEmbedder)
    built: list[FakeServer] = []

    def build_server(store: Any, embedder: Any) -> FakeServer:
        built.append(FakeServer(store, embedder))
        return built[-1]

    monkeypatch.setattr(module, "build_server", build_server)
    return built


def test_runs_the_server_with_a_cpu_embedder(servers: list[FakeServer]) -> None:
    main(Transport.HTTP)

    [server] = servers
    assert server.transport == "streamable-http"
    assert server.embedder.device == "cpu"
    assert server.embedder.model_name == "BAAI/bge-m3"


def test_refuses_a_model_that_does_not_fit_the_schema(
    servers: list[FakeServer], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FakeEmbedder, "dimension", 768)

    with pytest.raises(SystemExit, match="768 dimensions; the schema has 1024"):
        main()

    assert servers == []


def test_command_line_defaults_to_stdio(servers: list[FakeServer], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["alexandria-mcp"])

    with pytest.raises(SystemExit) as exit_info:
        run()

    assert exit_info.value.code == 0
    assert [s.transport for s in servers] == ["stdio"]
