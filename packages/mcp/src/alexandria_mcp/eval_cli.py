"""The `alexandria-eval` command: scores retrieval on the real library against a set of known questions."""

from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from alexandria_mcp.evaluation import evaluate, format_report, load_questions
from alexandria_mcp.main import build_retrieval

DEFAULT_QUESTIONS = Path("eval/questions.yaml")


def main(
    questions: Annotated[
        Path, typer.Argument(help="The questions file.", exists=True, dir_okay=False, readable=True)
    ] = DEFAULT_QUESTIONS,
    details: Annotated[bool, typer.Option("--details", help="Also list each question with its ranks.")] = False,
) -> None:
    """Print recall@5, recall@10 and MRR at document and page level for the current library."""
    try:
        loaded = load_questions(questions)
    except ValidationError as error:
        raise SystemExit(f"{questions}: {error}") from error
    typer.echo(format_report(evaluate(build_retrieval(), loaded), details=details))


def run() -> None:
    """Entry point for the `alexandria-eval` script."""
    typer.run(main)
