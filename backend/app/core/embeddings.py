"""Local embeddings with fastembed (BAAI/bge-small-en-v1.5, 384-d). No API quota used.

The ONNX model downloads once into `embedding_cache_dir` and runs on CPU. Inference runs in a
worker thread so it never blocks the event loop. bge uses different prefixes for queries and
passages; fastembed's query_embed / passage_embed apply them.
"""

import asyncio
import os
import threading
from functools import lru_cache

from app.core.config import get_settings


class EmbeddingService:
    def __init__(
        self,
        model_name: str | None = None,
        cache_dir: str | None = None,
        dim: int | None = None,
        batch_size: int = 64,
    ) -> None:
        s = get_settings()
        self.model_name = model_name or s.embedding_model
        self.cache_dir = cache_dir or s.embedding_cache_dir
        self.dim = dim or s.embedding_dim
        self.batch_size = batch_size
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                if get_settings().serverless:
                    # the Hugging Face downloader keeps a cache under $HOME, which is read-only
                    # on Vercel: without this the model never loads ("Read-only file system")
                    os.environ.setdefault("HF_HOME", "/tmp/hf")
                from fastembed import TextEmbedding  # heavy import, only when needed

                self._model = TextEmbedding(model_name=self.model_name, cache_dir=self.cache_dir)
        return self._model

    def _check(self, vectors: list[list[float]]) -> list[list[float]]:
        if vectors and len(vectors[0]) != self.dim:
            raise ValueError(f"{self.model_name} returned {len(vectors[0])}-d, expected {self.dim}")
        return vectors

    def embed_passages_sync(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        vectors = [v.tolist() for v in model.passage_embed(texts, batch_size=self.batch_size)]
        return self._check(vectors)

    def embed_query_sync(self, text: str) -> list[float]:
        model = self._load()
        return self._check([next(iter(model.query_embed([text]))).tolist()])[0]

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self.embed_passages_sync, texts)

    async def embed_query(self, text: str) -> list[float]:
        return await asyncio.to_thread(self.embed_query_sync, text)


@lru_cache
def get_embedder() -> EmbeddingService:
    return EmbeddingService()
