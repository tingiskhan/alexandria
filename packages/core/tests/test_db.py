from contextlib import nullcontext
from typing import TYPE_CHECKING, Any, cast

import pytest
from sqlalchemy.exc import OperationalError

from alexandria_core import db

if TYPE_CHECKING:
    from sqlalchemy import Engine


class FlakyEngine:
    """Refuses `failures` connections, then accepts."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.attempts = 0

    def connect(self) -> Any:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise OperationalError("connect", {}, Exception("the database system is not yet accepting connections"))
        return nullcontext()


def test_waits_until_the_database_accepts_connections() -> None:
    engine = FlakyEngine(failures=3)

    db.wait_for_database(cast("Engine", engine), timeout=5, interval=0.01)

    assert engine.attempts == 4


def test_gives_up_after_the_timeout() -> None:
    engine = FlakyEngine(failures=1_000)

    with pytest.raises(OperationalError, match="not yet accepting"):
        db.wait_for_database(cast("Engine", engine), timeout=0.1, interval=0.01)

    assert 1 < engine.attempts < 1_000
