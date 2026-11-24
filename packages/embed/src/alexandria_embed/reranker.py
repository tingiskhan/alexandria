"""`CrossEncoderReranker`, the optional `Reranker` the MCP server applies to search candidates."""

from collections.abc import Sequence

from sentence_transformers import CrossEncoder


class CrossEncoderReranker:
    """Scores (query, passage) pairs with a sentence-transformers cross-encoder, e.g. BAAI/bge-reranker-v2-m3."""

    def __init__(
        self, model_name: str, revision: str | None = None, device: str | None = None, batch_size: int = 32
    ) -> None:
        """Load the model.

        Args:
            model_name: A Hugging Face cross-encoder model.
            revision: A commit, branch or tag to pin; None means the default branch.
            device: Where to run it, e.g. "cpu" or "cuda"; None picks automatically.
            batch_size: How many pairs to score at once.
        """
        self.model_name = model_name
        self._batch_size = batch_size
        self._model = CrossEncoder(model_name, revision=revision, device=device)

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        """Return one relevance score per text, higher meaning more relevant to `query`."""
        if not texts:
            return []
        scores = self._model.predict(
            [(query, text) for text in texts],
            batch_size=self._batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [float(s) for s in scores]
