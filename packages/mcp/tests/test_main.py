from dataclasses import dataclass
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from sqlalchemy import Engine
from starlette.testclient import TestClient

from alexandria_core.db import EMBEDDING_DIMENSION
from alexandria_mcp import main as module
from alexandria_mcp.main import Transport, main, run
from alexandria_mcp.settings import McpSettings


@dataclass
class FakeServer:
    store: Any
    embedder: Any
    reranker: Any = None
    rerank_candidates: int | None = None
    transport: str | None = None
    options: dict[str, Any] | None = None

    def run(self, transport: str, **options: Any) -> None:
        self.transport = transport
        self.options = options


class FakeEmbedder:
    dimension = EMBEDDING_DIMENSION

    def __init__(self, model: str, revision: str, device: str | None = None) -> None:
        self.model_name = model
        self.device = device


class FakeReranker:
    def __init__(self, model_name: str, revision: str | None = None, device: str | None = None) -> None:
        self.model_name = model_name
        self.revision = revision
        self.device = device


@pytest.fixture
def schema_steps(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Engine]]:
    """What main does to the database before serving; the database itself is not reached."""
    steps: list[tuple[str, Engine]] = []
    monkeypatch.setattr(module, "wait_for_database", lambda engine: steps.append(("wait", engine)))
    monkeypatch.setattr(module, "migrate", lambda engine: steps.append(("migrate", engine)))
    return steps


@pytest.fixture
def servers(monkeypatch: pytest.MonkeyPatch, schema_steps: list[tuple[str, Engine]]) -> list[FakeServer]:
    monkeypatch.setenv("ALEXANDRIA_DATABASE_URL", "postgresql+psycopg://user:secret@db/alexandria")
    monkeypatch.delenv("ALEXANDRIA_RERANK_MODEL", raising=False)
    monkeypatch.setattr(module, "SentenceTransformerEmbedder", FakeEmbedder)
    monkeypatch.setattr(module, "CrossEncoderReranker", FakeReranker)
    built: list[FakeServer] = []

    def build_server(store: Any, embedder: Any, reranker: Any, rerank_candidates: int) -> FakeServer:
        built.append(FakeServer(store, embedder, reranker, rerank_candidates))
        return built[-1]

    monkeypatch.setattr(module, "build_server", build_server)
    return built


def test_runs_the_server_with_a_cpu_embedder(servers: list[FakeServer]) -> None:
    main(Transport.HTTP)

    [server] = servers
    assert server.transport == "streamable-http"
    assert server.options == {  # localhost only by default
        "host": "127.0.0.1",
        "port": 8000,
        "transport_security": McpSettings().transport_security(),
    }
    assert server.embedder.device == "cpu"
    assert server.embedder.model_name == "BAAI/bge-m3"


def test_migrates_before_serving(servers: list[FakeServer], schema_steps: list[tuple[str, Engine]]) -> None:
    main()

    assert [step for step, _ in schema_steps] == ["wait", "migrate"]
    assert schema_steps[0][1] is schema_steps[1][1]
    assert schema_steps[0][1].url.host == "db"


def test_reranking_is_off_by_default(servers: list[FakeServer]) -> None:
    main()

    assert servers[0].reranker is None


def test_a_rerank_model_turns_reranking_on(servers: list[FakeServer], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALEXANDRIA_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
    monkeypatch.setenv("ALEXANDRIA_RERANK_CANDIDATES", "30")

    main()

    [server] = servers
    assert isinstance(server.reranker, FakeReranker)
    assert (server.reranker.model_name, server.reranker.revision) == ("BAAI/bge-reranker-v2-m3", None)
    assert server.reranker.device == server.embedder.device == "cpu"
    assert server.rerank_candidates == 30


def test_http_listens_where_the_settings_say(servers: list[FakeServer], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALEXANDRIA_MCP_HOST", "0.0.0.0")  # noqa: S104 - as in a container
    monkeypatch.setenv("ALEXANDRIA_MCP_PORT", "9000")

    main(Transport.HTTP)

    assert servers[0].options is not None
    assert (servers[0].options["host"], servers[0].options["port"]) == ("0.0.0.0", 9000)  # noqa: S104


INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}


def initialize(settings: McpSettings, host: str, origin: str | None = None) -> int:
    """The status of an initialize request sent to a server bound everywhere, as in a container."""
    app = MCPServer("test").streamable_http_app(host="0.0.0.0", transport_security=settings.transport_security())  # noqa: S104
    headers = {"Accept": "application/json, text/event-stream", "Host": host}
    if origin:
        headers["Origin"] = origin
    with TestClient(app) as client:
        return client.post("/mcp", json=INITIALIZE, headers=headers).status_code


@pytest.mark.parametrize("host", ["localhost:8000", "127.0.0.1:8000", "[::1]:8000", "localhost"])
def test_http_accepts_localhost_when_bound_everywhere(host: str) -> None:
    assert initialize(McpSettings(), host) == 200


def test_http_rejects_a_rebound_host_when_bound_everywhere() -> None:
    assert initialize(McpSettings(), "evil.example:8000") == 421
    assert initialize(McpSettings(), "localhost.evil.example:8000") == 421


def test_http_rejects_a_foreign_origin() -> None:
    assert initialize(McpSettings(), "localhost:8000", "http://evil.example") == 403


def test_http_accepts_hosts_from_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALEXANDRIA_MCP_ALLOWED_HOSTS", "mcp.example.com, mcp")
    settings = McpSettings()

    assert settings.allowed_hosts == ("mcp.example.com", "mcp")
    assert initialize(settings, "mcp.example.com", "https://mcp.example.com") == 200
    assert initialize(settings, "mcp:8000") == 200
    assert initialize(settings, "evil.example") == 421


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
    assert [(s.transport, s.options) for s in servers] == [("stdio", {})]
