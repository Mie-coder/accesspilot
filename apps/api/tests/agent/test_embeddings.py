from typing import Any

import pytest

from accesspilot.agent.embeddings import (
    DashScopeEmbeddingModel,
    DeterministicEmbeddingModel,
    EmbeddingProviderError,
)


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeHttpClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.last_url: str | None = None
        self.last_headers: dict[str, str] | None = None
        self.last_json: dict[str, Any] | None = None

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: float,
    ) -> FakeResponse:
        self.last_url = url
        self.last_headers = headers
        self.last_json = json
        return self.response


def test_deterministic_embedding_is_stable_and_512_dimensions() -> None:
    model = DeterministicEmbeddingModel()

    first = model.embed(["客户数据导出权限"])[0]
    second = model.embed(["客户数据导出权限"])[0]
    different = model.embed(["服务日志读取权限"])[0]

    assert len(first) == 512
    assert first == second
    assert first != different


def test_dashscope_requests_explicit_512_dimensions() -> None:
    embedding = [0.0] * 512
    client = FakeHttpClient(
        FakeResponse(
            {
                "data": [
                    {"index": 0, "embedding": embedding},
                    {"index": 1, "embedding": embedding},
                ]
            }
        )
    )
    model = DashScopeEmbeddingModel(
        api_key="test-key",
        model_name="text-embedding-v4",
        client=client,
    )

    result = model.embed(["政策一", "政策二"])

    assert result == [embedding, embedding]
    assert client.last_url == (
        "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
    )
    assert client.last_headers == {
        "Authorization": "Bearer test-key",
        "Content-Type": "application/json",
    }
    assert client.last_json == {
        "model": "text-embedding-v4",
        "input": ["政策一", "政策二"],
        "dimensions": 512,
        "encoding_format": "float",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"data": []},
        {"data": [{"index": 0, "embedding": [0.0] * 511}]},
        {"data": [{"index": 1, "embedding": [0.0] * 512}]},
    ],
)
def test_dashscope_rejects_incomplete_or_wrong_dimension_response(
    payload: dict[str, Any],
) -> None:
    model = DashScopeEmbeddingModel(
        api_key="test-key",
        model_name="text-embedding-v4",
        client=FakeHttpClient(FakeResponse(payload)),
    )

    with pytest.raises(EmbeddingProviderError):
        model.embed(["政策一"])
