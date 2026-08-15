"""政策文本向量化的真实与离线适配器。"""

from hashlib import sha256
from math import isfinite, sqrt
from typing import Any, Protocol

import httpx

EMBEDDING_DIMENSIONS = 512
MAX_BATCH_SIZE = 10


class EmbeddingProviderError(RuntimeError):
    """向量服务不可用或返回了不可信结构。"""


class EmbeddingModel(Protocol):
    """把一组文本转换成顺序一致的 512 维向量。"""

    def embed(self, texts: list[str]) -> list[list[float]]:
        """返回与输入一一对应的向量。"""


class HttpResponse(Protocol):
    """百炼适配器实际使用的最小 HTTP 响应边界。"""

    def raise_for_status(self) -> None: ...

    def json(self) -> dict[str, Any]: ...


class HttpClient(Protocol):
    """允许测试注入假客户端，避免消耗真实百炼额度。"""

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: float,
    ) -> HttpResponse: ...


class DeterministicEmbeddingModel:
    """用稳定字符特征生成离线向量，不伪装成真实语义模型。"""

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    @staticmethod
    def _embed_one(text: str) -> list[float]:
        normalized = "".join(text.casefold().split())
        # 单字兼顾中文；二元和三元片段让相同短语获得更高相似度。
        features = [f"char:{character}" for character in normalized]
        for size in (2, 3):
            features.extend(
                f"ngram:{normalized[index : index + size]}"
                for index in range(max(0, len(normalized) - size + 1))
            )
        if not features:
            features = ["empty"]

        vector = [0.0] * EMBEDDING_DIMENSIONS
        for feature in features:
            digest = sha256(feature.encode("utf-8")).digest()
            position = int.from_bytes(digest[:2], "big") % EMBEDDING_DIMENSIONS
            sign = 1.0 if digest[2] & 1 else -1.0
            vector[position] += sign

        norm = sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector]


class DashScopeEmbeddingModel:
    """通过百炼 OpenAI 兼容接口调用 text-embedding-v4。"""

    def __init__(
        self,
        api_key: str,
        model_name: str = "text-embedding-v4",
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        client: HttpClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model_name = model_name
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client()

    def embed(self, texts: list[str]) -> list[list[float]]:
        """按百炼批次限制请求，并拒绝数量或维度不一致的响应。"""

        if not texts:
            return []

        vectors: list[list[float]] = []
        for start in range(0, len(texts), MAX_BATCH_SIZE):
            vectors.extend(self._embed_batch(texts[start : start + MAX_BATCH_SIZE]))
        return vectors

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        try:
            response = self._client.post(
                f"{self._base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model_name,
                    "input": texts,
                    # 百炼 v4 默认 1024 维；数据库契约固定 512，必须显式指定。
                    "dimensions": EMBEDDING_DIMENSIONS,
                    "encoding_format": "float",
                },
                timeout=30.0,
            )
            response.raise_for_status()
            payload = response.json()
            data = payload["data"]
        except EmbeddingProviderError:
            raise
        except Exception as error:
            raise EmbeddingProviderError("百炼向量服务调用失败") from error

        if not isinstance(data, list) or len(data) != len(texts):
            raise EmbeddingProviderError("百炼返回的向量数量与输入不一致")

        vectors_by_index: dict[int, list[float]] = {}
        for item in data:
            if not isinstance(item, dict):
                raise EmbeddingProviderError("百炼返回了非法向量条目")
            index = item.get("index")
            raw_vector = item.get("embedding")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not isinstance(raw_vector, list)
                or len(raw_vector) != EMBEDDING_DIMENSIONS
                or any(
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not isfinite(value)
                    for value in raw_vector
                )
            ):
                raise EmbeddingProviderError("百炼返回了非法 512 维向量")
            if index in vectors_by_index:
                raise EmbeddingProviderError("百炼返回了重复向量序号")
            vectors_by_index[index] = [float(value) for value in raw_vector]

        if set(vectors_by_index) != set(range(len(texts))):
            raise EmbeddingProviderError("百炼返回的向量序号不连续")
        return [vectors_by_index[index] for index in range(len(texts))]
