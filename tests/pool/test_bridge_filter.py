"""What a member may ask of llama-swap through the bridge, and what it may not.

The stub upstream here answers every path llama-swap answers, the ones a
member may reach and the ones it may not, so a filter that forwarded
everything would pass each of these requests through with a 200. What the
filter lets through and what it refuses is therefore the whole of what is
read back.

Nothing here reaches the rig: the upstream is an app in this process, and no
request leaves it.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from specflo.pool import bridge

# Every path the stub answers, with the method llama-swap answers it on.
SERVED = (
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/completions"),
    ("GET", "/v1/models"),
    ("GET", "/logs/stream"),
    ("GET", "/ui"),
    ("GET", "/running"),
    ("GET", "/api/version"),
    ("POST", "/api/unload"),
)

# The ones a member may not reach, each named in the requirement.
REFUSED = (
    ("GET", "/logs/stream"),
    ("GET", "/ui"),
    ("GET", "/running"),
    ("GET", "/api/version"),
)

TOKENS = ("Hel", "lo", " there")


def upstream_app() -> Starlette:
    """llama-swap's whole surface, as far as a test needs it."""
    async def answer(request):
        body = await request.body()
        return JSONResponse({
            "served": request.url.path,
            "method": request.method,
            "query": request.url.query,
            "body": body.decode("utf-8") if body else "",
            "asked": request.headers.get("x-asked-for", ""),
        })

    async def completion(request):
        """Streams its tokens where the request asks for it, as llama-swap does."""
        body = await request.body()
        asked = json.loads(body or b"{}") if body.startswith(b"{") else {}
        if not asked.get("stream"):
            return JSONResponse({"served": request.url.path, "body": body.decode("utf-8")})

        async def chunks():
            for token in TOKENS:
                yield f"data: {json.dumps({'token': token})}\n\n".encode()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    routes = [
        Route(path, answer, methods=[method])
        for method, path in SERVED
        if path != "/v1/chat/completions"
    ]
    routes.append(Route("/v1/chat/completions", completion, methods=["POST"]))
    return Starlette(routes=routes)


@pytest.fixture
def client() -> TestClient:
    """A client of the filter, whose upstream is the stub in this process."""
    upstream = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream_app()), base_url="http://llama-swap"
    )
    with TestClient(bridge.filter_app("http://llama-swap", upstream)) as client:
        yield client


def ask(client: TestClient, method: str, path: str, **kwargs) -> httpx.Response:
    return client.request(method, path, **kwargs)


# --- the stub answers everything, so the filter is what is being read --------


def test_the_upstream_answers_every_path_including_the_refused_ones() -> None:
    with TestClient(upstream_app()) as direct:
        for method, path in SERVED:
            assert direct.request(method, path).status_code == 200, path


# --- what a member may ask for ----------------------------------------------


def test_a_chat_completion_reaches_the_upstream(client: TestClient) -> None:
    answer = ask(client, "POST", "/v1/chat/completions", json={"model": "model-a"})

    assert answer.status_code == 200
    assert answer.json()["served"] == "/v1/chat/completions"
    assert json.loads(answer.json()["body"]) == {"model": "model-a"}


def test_a_completion_reaches_the_upstream(client: TestClient) -> None:
    answer = ask(client, "POST", "/v1/completions", json={"prompt": "hello"})

    assert answer.status_code == 200
    assert answer.json()["served"] == "/v1/completions"


def test_the_models_listing_reaches_the_upstream(client: TestClient) -> None:
    answer = ask(client, "GET", "/v1/models")

    assert answer.status_code == 200
    assert answer.json()["served"] == "/v1/models"


def test_the_query_and_the_headers_travel_with_an_allowed_request(client: TestClient) -> None:
    answer = ask(
        client, "GET", "/v1/models?verbose=1", headers={"x-asked-for": "the listing"}
    )

    assert answer.json()["query"] == "verbose=1"
    assert answer.json()["asked"] == "the listing"


# --- what it may not --------------------------------------------------------


@pytest.mark.parametrize("method, path", REFUSED)
def test_the_rest_of_the_surface_is_refused(client: TestClient, method, path) -> None:
    answer = ask(client, method, path)

    assert answer.status_code == bridge.REFUSED
    assert answer.status_code != 200
    assert path in answer.json()["error"]["message"]


def test_a_refusal_says_which_request_was_refused(client: TestClient) -> None:
    answer = ask(client, "GET", "/logs/stream")

    assert answer.json()["error"]["type"] == "specflo_pool_refused"
    assert "GET /logs/stream" in answer.json()["error"]["message"]


def test_the_unload_path_that_would_evict_another_members_model_is_refused(
    client: TestClient,
) -> None:
    assert ask(client, "POST", "/api/unload").status_code == bridge.REFUSED


def test_an_allowed_path_on_another_method_is_refused(client: TestClient) -> None:
    # A member asks for a completion by making one; the listing is a read.
    assert ask(client, "GET", "/v1/chat/completions").status_code == bridge.REFUSED
    assert ask(client, "POST", "/v1/models").status_code == bridge.REFUSED


def test_a_path_that_walks_up_to_a_refused_one_is_refused(client: TestClient) -> None:
    assert ask(client, "GET", "/v1/models/../../logs/stream").status_code == bridge.REFUSED


def test_a_path_that_walks_up_to_an_allowed_one_is_still_allowed(client: TestClient) -> None:
    answer = ask(client, "GET", "/logs/../v1/models")

    assert answer.status_code == 200
    assert answer.json()["served"] == "/v1/models"


def test_a_path_nobody_serves_is_refused_rather_than_asked_about(client: TestClient) -> None:
    assert ask(client, "GET", "/never-was").status_code == bridge.REFUSED


# --- the decision on its own ------------------------------------------------


def test_the_allow_list_is_the_three_requests_and_no_more() -> None:
    assert bridge.ALLOWED == {
        ("POST", "/v1/chat/completions"),
        ("POST", "/v1/completions"),
        ("GET", "/v1/models"),
    }


def test_a_trailing_slash_names_the_same_endpoint() -> None:
    assert bridge.allowed("GET", "/v1/models/")
    assert bridge.allowed("get", "/v1/models")


def test_a_streamed_completion_arrives_token_by_token(client: TestClient) -> None:
    # The filter holds none of the body: what the upstream sends as it goes
    # is what the member reads as it goes.
    with client.stream(
        "POST", "/v1/chat/completions", json={"model": "model-a", "stream": True}
    ) as answer:
        assert answer.status_code == 200
        assert answer.headers["content-type"].startswith("text/event-stream")
        assert "content-length" not in answer.headers
        read = [
            json.loads(line.removeprefix("data: "))["token"]
            for line in answer.iter_lines()
            if line.startswith("data: ") and not line.endswith("[DONE]")
        ]

    assert read == list(TOKENS)


def test_a_body_arrives_whole_however_it_was_sent(client: TestClient) -> None:
    answer = ask(client, "POST", "/v1/completions", content=b"x" * 100_000)

    assert len(json.loads(answer.text)["body"]) == 100_000


# --- what the module itself may name ----------------------------------------

# llama-swap's unload paths, its profile paths, and the ways a model is put on
# a card. The pool asks llama-swap for none of these, and the bridge, which is
# the one module that carries a request there, may not name them either. The
# scan that holds every other module to this leaves the bridge out, because
# the bridge names the completion paths on purpose; this is its half.
NEVER = (
    "/unload", "/api/models", "/api/profiles", "/upstream",
    "VISIBLE_DEVICES", "--device", "--main-gpu", "--tensor-split", "--split-mode",
    "-ngl", "--n-gpu-layers",
)


def module_text() -> str:
    return Path(bridge.__file__).read_text(encoding="utf-8")


def test_the_module_names_no_unload_profile_or_card_request() -> None:
    assert [mark for mark in NEVER if mark in module_text()] == []


def test_the_only_paths_it_names_are_the_ones_it_allows() -> None:
    text = module_text()
    named = set(re.findall(r'"(/[a-z0-9/_.-]*)"', text))

    assert named <= {path for _, path in bridge.ALLOWED} | {"/"}


def test_the_only_writing_verb_it_names_is_the_one_the_allow_list_carries() -> None:
    named = set(re.findall(r'"(POST|PUT|PATCH|DELETE)"', module_text()))

    assert named == {method for method, _ in bridge.ALLOWED} - {"GET"}


# --- how long the filter waits for llama-swap --------------------------------

# Longer than the wait an HTTP client gives up after by default. llama-swap
# answers a completion only once the model is loaded, which takes seconds to
# minutes, and a stream pauses while a long prompt is read.
SLOW = 6.0


def slow_upstream_app() -> Starlette:
    """A llama-swap that loads a model before it answers, and pauses mid-stream."""
    async def loading(request):
        await asyncio.sleep(SLOW)
        return JSONResponse({"served": request.url.path})

    async def pausing(request):
        async def chunks():
            yield f"data: {json.dumps({'token': TOKENS[0]})}\n\n".encode()
            await asyncio.sleep(SLOW)
            for token in TOKENS[1:]:
                yield f"data: {json.dumps({'token': token})}\n\n".encode()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    return Starlette(routes=[
        Route("/v1/completions", loading, methods=["POST"]),
        Route("/v1/chat/completions", pausing, methods=["POST"]),
    ])


@pytest.fixture
def slow_client():
    """A client of the filter, whose upstream is the slow app on a loopback
    port, so the filter makes its own client and its own waits apply."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        slow_upstream_app(), log_config=None, access_log=False, lifespan="off"
    ))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "the slow upstream did not start"
        time.sleep(0.01)
    try:
        with TestClient(bridge.filter_app(f"http://127.0.0.1:{port}")) as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()


def test_a_request_that_waits_on_a_model_load_is_answered(slow_client: TestClient) -> None:
    started = time.monotonic()
    answer = ask(slow_client, "POST", "/v1/completions", json={"model": "model-a"})

    assert time.monotonic() - started >= SLOW
    assert answer.status_code == 200
    assert answer.json() == {"served": "/v1/completions"}


def test_a_stream_that_pauses_between_two_chunks_arrives_whole(slow_client: TestClient) -> None:
    with slow_client.stream(
        "POST", "/v1/chat/completions", json={"model": "model-a", "stream": True}
    ) as answer:
        assert answer.status_code == 200
        lines = list(answer.iter_lines())

    read = [
        json.loads(line.removeprefix("data: "))["token"]
        for line in lines
        if line.startswith("data: ") and not line.endswith("[DONE]")
    ]
    assert read == list(TOKENS)
    assert "data: [DONE]" in lines


def test_an_upstream_that_is_not_there_fails_at_once() -> None:
    # Nothing listens on port 1: the filter may wait long for an answer, and
    # not for a connection that cannot be made.
    with TestClient(
        bridge.filter_app("http://127.0.0.1:1"), raise_server_exceptions=False
    ) as client:
        started = time.monotonic()
        answer = ask(client, "GET", "/v1/models")

    assert answer.status_code >= 500
    assert time.monotonic() - started < 2
