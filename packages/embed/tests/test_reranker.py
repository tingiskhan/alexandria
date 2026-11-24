import pytest

from alexandria_embed.reranker import CrossEncoderReranker

pytestmark = pytest.mark.gpu

MODEL = "BAAI/bge-reranker-v2-m3"


@pytest.fixture(scope="module")
def reranker() -> CrossEncoderReranker:
    return CrossEncoderReranker(MODEL, device="cuda")


def test_relevant_passage_scores_higher(reranker: CrossEncoderReranker) -> None:
    near, far = reranker.score("How much did revenue grow?", ["Revenue rose 12% in 2025.", "The cat sat on the mat."])

    assert near > far
