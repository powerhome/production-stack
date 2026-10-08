import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from unittest.mock import AsyncMock
from unittest.mock import AsyncMock

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from fastapi import FastAPI

import vllm_router.service_discovery as service_discovery_module
from vllm_router.aiohttp_client import AiohttpClientWrapper
from vllm_router.routers.main_router import main_router
from vllm_router.routers.routing_logic import (
    RoutingLogic,
    cleanup_routing_logic,
    get_routing_logic,
    initialize_routing_logic,
)
from vllm_router.service_discovery import (
    ServiceDiscoveryType,
    initialize_service_discovery,
)
from vllm_router.stats.engine_stats import (
    EngineStatsScraper,
    initialize_engine_stats_scraper,
)
from vllm_router.stats.request_stats import (
    RequestStatsMonitor,
)
from vllm_router.stats.request_stats import SingletonMeta as RequestStatsSingletonMeta
from vllm_router.stats.request_stats import (
    initialize_request_stats_monitor,
)
from vllm_router.utils import SingletonMeta

AUDIO_MODEL = "openai/whisper-tiny"
IMAGE_MODEL = "image-edit-model"


@asynccontextmanager
async def multipart_backend(endpoint, handler):
    app = web.Application()
    app.router.add_post(endpoint, handler)

    async def metrics(_):
        return web.Response(text="")

    app.router.add_get("/metrics", metrics)

    async with TestServer(app) as server:
        yield str(server.make_url("/")).rstrip("/")


@asynccontextmanager
async def router_client(
    backend_url,
    model=AUDIO_MODEL,
    routing_logic=RoutingLogic.ROUND_ROBIN,
    **routing_options,
):
    app = FastAPI()
    app.include_router(main_router)

    async with AsyncExitStack() as stack:
        service_discovery = initialize_service_discovery(
            ServiceDiscoveryType.STATIC,
            app=app,
            urls=[backend_url],
            models=[model],
        )
        stack.callback(
            setattr, service_discovery_module, "_global_service_discovery", None
        )
        stack.callback(service_discovery.close)

        engine_stats = initialize_engine_stats_scraper(1)
        stack.callback(SingletonMeta._instances.pop, EngineStatsScraper, None)
        stack.push_async_callback(asyncio.to_thread, engine_stats.close)

        request_stats = initialize_request_stats_monitor(10)
        stack.callback(
            RequestStatsSingletonMeta._instances.pop, RequestStatsMonitor, None
        )

        router = initialize_routing_logic(
            routing_logic,
            max_instance_failover_reroute_attempts=0,
            **routing_options,
        )
        stack.callback(cleanup_routing_logic)

        aiohttp_client = AiohttpClientWrapper()
        aiohttp_client.start()
        stack.push_async_callback(aiohttp_client.stop)

        app.state.engine_stats_scraper = engine_stats
        app.state.request_stats_monitor = request_stats
        app.state.router = router
        app.state.aiohttp_client_wrapper = aiohttp_client
        app.state.otel_enabled = False

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://router"
        ) as client:
            yield client


@pytest.mark.asyncio
async def test_audio_translation_accepts_standard_multipart_request():
    received = {}

    async def translate(request):
        form = await request.post()
        upload = form["file"]
        received.update(
            model=form["model"],
            filename=upload.filename,
            content_type=upload.content_type,
            content=upload.file.read(),
        )
        return web.json_response({"text": "translated"})

    async with multipart_backend("/v1/audio/translations", translate) as backend_url:
        async with router_client(backend_url) as client:
            response = await client.post(
                "/v1/audio/translations",
                data={"model": AUDIO_MODEL},
                files={"file": ("speech.wav", b"fake-audio", "audio/wav")},
            )

    assert response.status_code == 200
    assert response.json() == {"text": "translated"}
    assert received == {
        "model": AUDIO_MODEL,
        "filename": "speech.wav",
        "content_type": "audio/wav",
        "content": b"fake-audio",
    }


@pytest.mark.asyncio
async def test_audio_translation_preserves_plain_text_response():
    received = {}

    async def translate(request):
        form = await request.post()
        received["response_format"] = form["response_format"]
        return web.Response(text="translated text", content_type="text/plain")

    async with multipart_backend("/v1/audio/translations", translate) as backend_url:
        async with router_client(backend_url) as client:
            response = await client.post(
                "/v1/audio/translations",
                data={"model": AUDIO_MODEL, "response_format": "text"},
                files={"file": ("speech.wav", b"fake-audio", "audio/wav")},
            )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "translated text"
    assert received == {"response_format": "text"}


@pytest.mark.asyncio
async def test_audio_transcription_preserves_plain_text_response():
    received = {}

    async def transcribe(request):
        form = await request.post()
        upload = form["file"]
        received.update(
            model=form["model"],
            response_format=form["response_format"],
            # absent by default so the backend auto-detects; None documents that
            language=form.get("language"),
            filename=upload.filename,
            content_type=upload.content_type,
            content=upload.file.read(),
        )
        return web.Response(text="transcribed text", content_type="text/plain")

    async with multipart_backend("/v1/audio/transcriptions", transcribe) as backend_url:
        async with router_client(backend_url) as client:
            response = await client.post(
                "/v1/audio/transcriptions",
                data={"model": AUDIO_MODEL, "response_format": "text"},
                files={"file": ("speech.wav", b"fake-audio", "audio/wav")},
            )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "transcribed text"
    assert received == {
        "model": AUDIO_MODEL,
        "response_format": "text",
        "language": None,
        "filename": "speech.wav",
        "content_type": "audio/wav",
        "content": b"fake-audio",
    }


@pytest.mark.asyncio
async def test_image_edit_accepts_standard_multipart_request():
    received = {}

    async def edit_image(request):
        form = await request.post()
        upload = form["image"]
        received.update(
            model=form["model"],
            prompt=form["prompt"],
            filename=upload.filename,
            content_type=upload.content_type,
            content=upload.file.read(),
        )
        return web.json_response({"data": [{"url": "https://example.test/edit.png"}]})

    async with multipart_backend("/v1/images/edits", edit_image) as backend_url:
        async with router_client(backend_url, model=IMAGE_MODEL) as client:
            response = await client.post(
                "/v1/images/edits",
                data={"model": IMAGE_MODEL, "prompt": "add a hat"},
                files={"image": ("input.png", b"fake-image", "image/png")},
            )

    assert response.status_code == 200
    assert response.json() == {"data": [{"url": "https://example.test/edit.png"}]}
    assert received == {
        "model": IMAGE_MODEL,
        "prompt": "add a hat",
        "filename": "input.png",
        "content_type": "image/png",
        "content": b"fake-image",
    }


@pytest.mark.asyncio
async def test_priority_router_forwards_resolved_priority_and_all_form_fields():
    received = {}

    async def edit_image(request):
        form = await request.post()
        received.update(
            priority=form["priority"],
            prompt=form["prompt"],
            extras=form.getall("extra"),
            image=form["image"].file.read(),
        )
        return web.json_response({"ok": True})

    async with multipart_backend("/v1/images/edits", edit_image) as backend_url:
        async with router_client(
            backend_url,
            model=IMAGE_MODEL,
            routing_logic=RoutingLogic.PRIORITY,
            priority_default=5,
            priority_threshold=2,
        ) as client:
            response = await client.post(
                "/v1/images/edits",
                headers={"x-request-priority": "1"},
                data={
                    "model": IMAGE_MODEL,
                    "prompt": "add a hat",
                    "priority": "7",
                    "extra": ["a", "b"],
                },
                files={"image": ("input.png", b"fake-image", "image/png")},
            )

    assert response.status_code == 200
    assert received == {
        "priority": "1",
        "prompt": "add a hat",
        "extras": ["a", "b"],
        "image": b"fake-image",
    }


@pytest.mark.asyncio
async def test_prefix_router_receives_multipart_prompt():
    received = {}

    async def edit_image(request):
        form = await request.post()
        received["prompt"] = form["prompt"]
        return web.json_response({"ok": True})

    async with multipart_backend("/v1/images/edits", edit_image) as backend_url:
        async with router_client(
            backend_url,
            model=IMAGE_MODEL,
            routing_logic=RoutingLogic.PREFIXAWARE,
            prefix_min_match_length=1,
        ) as client:
            router = get_routing_logic()
            router.hashtrie.longest_prefix_match = AsyncMock(return_value=(0, set()))
            response = await client.post(
                "/v1/images/edits",
                data={"model": IMAGE_MODEL, "prompt": "add a hat"},
                files={"image": ("input.png", b"fake-image", "image/png")},
            )
            router.hashtrie.longest_prefix_match.assert_awaited_once_with(
                "add a hat", {backend_url}
            )

    assert response.status_code == 200
    assert received == {"prompt": "add a hat"}
