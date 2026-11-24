"""Retrieval evaluation: run known questions through search and score where the expected pages rank.

Each question names the document (by path) and the pages that answer it. A hit is relevant at document level when
it comes from that document, and at page level when its pages also overlap the expected range. For each level the
report gives recall@k (the share of questions with a relevant hit in the top k) and the mean reciprocal rank of the
first relevant hit (0 when none is retrieved).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from alexandria_core.models import SearchHit
from alexandria_mcp.retrieval import Retrieval

CUTOFFS = (5, 10)


class Question(BaseModel):
    """A question with the document and pages that answer it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str = Field(min_length=1)
    path: str = Field(min_length=1)  # the document's display path, as `list_documents` shows it
    first_page: int = Field(ge=1)
    last_page: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> "Question":
        """Reject a range that ends before it starts."""
        if self.last_page < self.first_page:
            raise ValueError(f"last_page {self.last_page} is before first_page {self.first_page}")
        return self


class QuestionSet(BaseModel):
    """The questions file: a non-empty list of questions."""

    model_config = ConfigDict(extra="forbid")

    questions: list[Question] = Field(min_length=1)


def load_questions(path: Path) -> list[Question]:
    """Read and validate a questions file.

    Args:
        path: A YAML file with a top-level `questions` list.

    Returns:
        The questions, in file order.

    Raises:
        pydantic.ValidationError: If the file doesn't match the schema.
    """
    return QuestionSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))).questions


class Level(StrEnum):
    """How precisely a hit must match the expected answer to count."""

    DOCUMENT = "document"
    PAGE = "page"


def is_relevant(hit: SearchHit, question: Question, level: Level) -> bool:
    """Whether `hit` answers `question` at `level`.

    Args:
        hit: A search result.
        question: The question with its expected document and pages.
        level: Document level needs the right document; page level also needs the hit's pages to overlap the range.

    Returns:
        True if the hit counts as relevant.
    """
    if hit.path != question.path:
        return False
    return level is Level.DOCUMENT or (hit.page_start <= question.last_page and hit.page_end >= question.first_page)


def first_relevant_rank(hits: Sequence[SearchHit], question: Question, level: Level) -> int | None:
    """Return the 1-based rank of the first relevant hit, or None if no hit is relevant."""
    return next((rank for rank, hit in enumerate(hits, start=1) if is_relevant(hit, question, level)), None)


@dataclass(frozen=True)
class Metrics:
    """Scores for one level over all questions."""

    recall: dict[int, float]  # cutoff k -> share of questions with a relevant hit in the top k
    mrr: float


def score(ranks: Sequence[int | None], cutoffs: Sequence[int] = CUTOFFS) -> Metrics:
    """Compute recall@k for each cutoff and the mean reciprocal rank.

    Args:
        ranks: The first relevant rank for each question, None when nothing relevant was retrieved.
        cutoffs: The values of k to report recall at.

    Returns:
        The metrics; all zero when there are no questions.
    """
    if not ranks:
        return Metrics(recall=dict.fromkeys(cutoffs, 0.0), mrr=0.0)
    recall = {k: sum(1 for r in ranks if r is not None and r <= k) / len(ranks) for k in cutoffs}
    return Metrics(recall=recall, mrr=sum(1 / r for r in ranks if r is not None) / len(ranks))


@dataclass(frozen=True)
class Outcome:
    """Where the first relevant hit ranked for one question, at each level."""

    question: Question
    ranks: dict[Level, int | None]


@dataclass(frozen=True)
class Report:
    """The result of an evaluation run."""

    outcomes: list[Outcome]
    metrics: dict[Level, Metrics]
    depth: int  # how many hits were retrieved per question
    unknown_paths: list[str]  # expected paths that aren't in the library, most likely typos or deleted files


def evaluate(retrieval: Retrieval, questions: Sequence[Question], cutoffs: Sequence[int] = CUTOFFS) -> Report:
    """Search for each question as the MCP `search` tool does, reranking included, and score the results.

    Args:
        retrieval: The library to search, with the query embedder and the reranker the server uses.
        questions: The questions to ask.
        cutoffs: The values of k to report recall at; the largest is how many hits are retrieved.

    Returns:
        Per-question ranks and the metrics at document and page level.
    """
    depth = max(cutoffs)
    outcomes = []
    for question in questions:
        hits = retrieval.search(question.question, limit=depth)
        outcomes.append(Outcome(question, {level: first_relevant_rank(hits, question, level) for level in Level}))
    metrics = {level: score([o.ranks[level] for o in outcomes], cutoffs) for level in Level}
    known = {document.path for document in retrieval.store.list_documents()}
    unknown = sorted({q.path for q in questions} - known)
    return Report(outcomes=outcomes, metrics=metrics, depth=depth, unknown_paths=unknown)


def format_report(report: Report, details: bool = False) -> str:
    """Render the report as plain text, ready to paste into a pull request.

    Args:
        report: The evaluation result.
        details: Also list each question with its ranks.

    Returns:
        A table of recall@k and MRR per level, preceded by warnings and per-question lines when there are any.
    """
    lines = [f"warning: not in the library: {path}" for path in report.unknown_paths]
    if details:
        for outcome in report.outcomes:
            ranks = "  ".join(f"{level}={_rank(outcome.ranks[level])}" for level in Level)
            lines.append(f"{ranks}  {outcome.question.question}")
    if lines:
        lines.append("")
    cutoffs = sorted(next(iter(report.metrics.values())).recall)
    lines.append(f"{len(report.outcomes)} questions, top {report.depth} hits each")
    lines.append("")
    lines.append(f"{'level':<10}" + "".join(f"{f'recall@{k}':>11}" for k in cutoffs) + f"{'MRR':>8}")
    for level, metrics in report.metrics.items():
        lines.append(f"{level:<10}" + "".join(f"{metrics.recall[k]:>11.3f}" for k in cutoffs) + f"{metrics.mrr:>8.3f}")
    return "\n".join(lines)


def _rank(rank: int | None) -> str:
    """A rank for display: the number, or '-' when nothing relevant was retrieved."""
    return "-" if rank is None else str(rank)
