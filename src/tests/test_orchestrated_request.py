import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.datastructures import Headers

from vllm_router.services.request_service.request import (
    route_orchestrated_disaggregated_request,
)


class Endpoint:
    def __init__(self, url, model_names, model_label):
        self.url = url
        self.model_names = model_names
        self.model_label = model_label
        self.sleep = False


class PostResult:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_):
        return False

    def __await__(self):
        async def result():
            return self.response

        return result().__await__()


def make_request(model="wanted", authorization="Bearer client-key"):
    request = MagicMock()
    request.headers = Headers({"X-Request-Id": "req-1", "Authorization": authorization})
    request.json = AsyncMock(return_value={"model": model, "stream": False})
    request.app.state.router = MagicMock()
    return request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server_key", "expected_auth"),
    [("server-key", "Bearer server-key"), (None, "Bearer client-key")],
)
async def test_orchestrated_routes_only_requested_model_with_auth(
    monkeypatch, server_key, expected_auth
):
    if server_key:
        monkeypatch.setenv("VLLM_API_KEY", server_key)
    else:
        monkeypatch.delenv("VLLM_API_KEY", raising=False)

    request = make_request()
    endpoints = [
        Endpoint("http://other-prefill:8000", ["other"], "prefill"),
        Endpoint("http://wanted-prefill:8000", ["wanted"], "prefill"),
        Endpoint("http://other-decode:8000", ["other"], "decode"),
        Endpoint("http://wanted-decode:8000", ["wanted"], "decode"),
    ]
    router = request.app.state.router
    router._find_endpoints.side_effect = lambda found: (
        [e for e in found if e.model_label == "prefill"],
        [e for e in found if e.model_label == "decode"],
    )
    router.select_prefill_endpoint.side_effect = lambda found: found[0]
    router.select_decode_endpoint.side_effect = lambda found: found[0]

    prefill_response = MagicMock()
    prefill_response.status = 200
    prefill_response.json = AsyncMock(return_value={"kv_transfer_params": {}})
    decode_response = MagicMock()
    decode_response.status = 200
    decode_response.read = AsyncMock(return_value=b'{"ok":true}')
    client = MagicMock()
    client.post.side_effect = [
        PostResult(prefill_response),
        PostResult(decode_response),
    ]
    request.app.state.aiohttp_client_wrapper.return_value = client
    discovery = MagicMock()
    discovery.get_endpoint_info.return_value = endpoints

    with patch(
        "vllm_router.services.request_service.request.get_service_discovery",
        return_value=discovery,
    ):
        response = await route_orchestrated_disaggregated_request(
            request, "/v1/chat/completions", MagicMock()
        )

    assert json.loads(response.body) == {"ok": True}
    assert [call.args[0] for call in client.post.call_args_list] == [
        "http://wanted-prefill:8000/v1/chat/completions",
        "http://wanted-decode:8000/v1/chat/completions",
    ]
    for call in client.post.call_args_list:
        headers = {key.lower(): value for key, value in call.kwargs["headers"].items()}
        assert headers["authorization"] == expected_auth
        assert headers["x-request-id"] == "req-1"


@pytest.mark.asyncio
async def test_orchestrated_rejects_missing_model():
    request = make_request(model=None)
    request.json.return_value = {"stream": False}
    response = await route_orchestrated_disaggregated_request(
        request, "/v1/chat/completions", MagicMock()
    )
    assert response.status_code == 400
    assert "missing 'model'" in json.loads(response.body)["error"]
