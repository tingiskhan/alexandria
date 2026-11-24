"""`SentenceTransformerEmbedder`, the `Embedder` used for both documents and queries."""

from collections.abc import Sequence

from sentence_transformers import SentenceTransformer


class SentenceTransformerEmbedder:
    """Embeds text with a pinned sentence-transformers model. Vectors are L2-normalised for cosine search.

    encode_query / encode_document apply any query or document prompt the model's config defines,
    so models that need an instruction prefix get it without code changes here.
    """

    def __init__(self, model_name: str, revision: str, device: str | None = None, batch_size: int = 32) -> None:
        self.model_name = model_name
        self._batch_size = batch_size
        self._model = SentenceTransformer(model_name, revision=revision, device=device)
        dimension = self._model.get_embedding_dimension()
        if dimension is None:
            raise ValueError(f"{model_name} does not report an embedding dimension")
        self.dimension: int = dimension

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed passages in batches, applying the model's document prompt if it has one."""
        if not texts:
            return []
        vectors = self._model.encode_document(
            list(texts), batch_size=self._batch_size, normalize_embeddings=True, convert_to_numpy=True
        )
        return [[float(x) for x in row] for row in vectors]

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query, applying the model's query prompt if it has one."""
        vector = self._model.encode_query(text, normalize_embeddings=True, convert_to_numpy=True)
        return [float(x) for x in vector]
