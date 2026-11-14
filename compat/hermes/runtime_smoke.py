#!/usr/bin/env python3
"""Exercise genuine Hermes dependencies on CPython 3.13/3.14, including Android.

Run in a fresh interpreter, for example::

    python runtime_smoke.py --package full --require-android

This is an offline compatibility test, not an inference/service availability test.
Only the aiohttp fixture opens sockets, on IPv4 loopback with an ephemeral port.
SDK HTTP requests use httpx.MockTransport and explicit, non-secret fixture keys.
No required dependency or failing test is skipped. Stdout is a JSON evidence report;
unittest diagnostics go to stderr. Host passes never constitute Android proof.
"""

import argparse
import asyncio
import base64
from contextlib import asynccontextmanager, contextmanager
import importlib
import importlib.machinery
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import socket
import sys
import sysconfig
import time
import unittest
from unittest import mock


# A deliberately bounded, reproducible compatibility set, not a "latest" claim.
PACKAGE_SPECS = {
    "jiter": ("0.19.0", "jiter", ("jiter.jiter",)),
    "pydantic-core": ("2.50.4", "pydantic_core", ("pydantic_core._pydantic_core",)),
    "pydantic": ("2.14.3", "pydantic", ()),
    "msgpack": ("1.2.4", "msgpack", ()),
    "aiohttp": (
        "3.14.5", "aiohttp",
        ("aiohttp._http_parser", "aiohttp._http_writer", "aiohttp._websocket.reader_c",
         "aiohttp._websocket.mask"),
    ),
    "openai": ("2.54.0", "openai", ()),
    "anthropic": ("2.8.0", "anthropic", ()),
    "fal-client": ("1.0.3", "fal_client", ()),
    "httpx": ("0.28.1", "httpx", ()),
}
PACKAGE_GROUPS = {
    "jiter": ("jiter",),
    "pydantic-core": ("pydantic-core",),
    "full": tuple(PACKAGE_SPECS),
}
FIXTURE_KEY = "hermes-offline-fixture-not-a-real-key"
LOG_MARKER = "HERMES_RUNTIME_SMOKE_JSON="


def platform_evidence():
    api_function = getattr(sys, "getandroidapilevel", None)
    build_api_level = api_function() if callable(api_function) else None
    api_level = None
    api_error = None
    if sys.platform == "android":
        try:
            # sys.getandroidapilevel is the interpreter's build-time minimum,
            # not the device API. android_ver reads ro.build.version.sdk.
            observed_api = platform.android_ver().api_level
            if type(observed_api) is int and observed_api > 0:
                api_level = observed_api
            else:
                api_error = "platform.android_ver returned no valid runtime API level"
        except Exception as error:
            api_error = f"{type(error).__name__}: {error}"
    sysconf = getattr(os, "sysconf", None)
    return {
        "platform": sys.platform,
        "system": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "soabi": sysconfig.get_config_var("SOABI"),
        "android_api_level": api_level,
        "android_build_api_level": build_api_level,
        "android_api_query_error": api_error,
        "page_size": sysconf("SC_PAGE_SIZE") if callable(sysconf) else None,
        "is_android": sys.platform == "android" and api_level is not None,
    }


def validate_platform(evidence, require_android):
    if evidence["implementation"] != "CPython":
        raise RuntimeError("This compatibility lane requires CPython")
    if tuple(map(int, evidence["python"].split(".")[:2])) not in ((3, 13), (3, 14)):
        raise RuntimeError("This compatibility lane supports Python 3.13 and 3.14 only")
    if require_android and not evidence["is_android"]:
        if evidence["platform"] == "android":
            raise RuntimeError(
                "--require-android requires a valid observed Android runtime API level"
            )
        raise RuntimeError("--require-android rejects host-only execution")
    if evidence["is_android"] and evidence["android_api_level"] < 24:
        raise RuntimeError("This compatibility lane requires Android API 24 or newer")


def module_evidence(name, require_native=False):
    module = importlib.import_module(name)
    file_name = getattr(module, "__file__", None)
    is_native = bool(file_name and any(
        file_name.endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES
    ))
    if not file_name:
        raise RuntimeError(f"{name} has no module-file provenance")
    if require_native and not is_native:
        raise RuntimeError(f"{name} is not a native extension: {file_name}")
    return {"module": name, "file": file_name, "native": is_native}


def package_evidence(package):
    expected, module_name, native_modules = PACKAGE_SPECS[package]
    actual = importlib.metadata.version(package)
    if actual != expected:
        raise RuntimeError(f"{package}: expected {expected}, installed {actual}")
    return {
        "distribution": package,
        "version": actual,
        "expected_version": expected,
        "module": module_evidence(module_name),
        "native_modules": [module_evidence(name, True) for name in native_modules],
    }


def prepare_msgpack():
    """Select the upstream fallback before import, never a replacement/shim package."""
    loaded = sys.modules.get("msgpack")
    if loaded is not None and loaded.Packer.__module__ != "msgpack.fallback":
        raise RuntimeError("msgpack already uses its C extension; rerun in a fresh interpreter")
    os.environ["MSGPACK_PUREPYTHON"] = "1"


class JiterTests(unittest.TestCase):
    def test_valid_json(self):
        import jiter

        payload = {"text": "Hermes \u03bb \U0001f680", "values": [0, -17, 1.25, True, None]}
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.assertEqual(jiter.from_json(raw), payload)

    def test_partial_json(self):
        import jiter

        raw = b'{"complete": [1, 2], "incomplete": "hel'
        self.assertEqual(jiter.from_json(raw, partial_mode=True), {"complete": [1, 2]})
        self.assertEqual(jiter.from_json(raw, partial_mode="trailing-strings"), {
            "complete": [1, 2], "incomplete": "hel",
        })
        with self.assertRaises(ValueError):
            jiter.from_json(raw)

    def test_invalid_json_and_duplicate_key(self):
        import jiter

        for raw in (b'{"x": nope}', b'[1,]', b'"\xff"', b'{} trailing'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                jiter.from_json(raw)
        with self.assertRaises(ValueError):
            jiter.from_json(b'{"x": 1, "x": 2}', catch_duplicate_keys=True)


class PydanticCoreTests(unittest.TestCase):
    def test_validate_and_serialize_schema(self):
        from pydantic_core import SchemaSerializer, SchemaValidator, core_schema

        schema = core_schema.typed_dict_schema({
            "count": core_schema.typed_dict_field(core_schema.int_schema(ge=1)),
            "labels": core_schema.typed_dict_field(
                core_schema.list_schema(core_schema.str_schema())
            ),
        })
        validator = SchemaValidator(schema)
        expected = {"count": 3, "labels": ["Hermes", "\u03bb"]}
        self.assertEqual(validator.validate_python({"count": "3", "labels": ["Hermes", "\u03bb"]}),
                         expected)
        self.assertEqual(validator.validate_json(br'{"count":3,"labels":["Hermes","\u03bb"]}'),
                         expected)
        serializer = SchemaSerializer(schema)
        self.assertEqual(json.loads(serializer.to_json(expected)), expected)
        self.assertEqual(serializer.to_python(expected), expected)

    def test_validation_errors_are_structured(self):
        from pydantic_core import SchemaValidator, ValidationError, core_schema

        validator = SchemaValidator(core_schema.list_schema(core_schema.int_schema(strict=True)))
        with self.assertRaises(ValidationError) as caught:
            validator.validate_python([1, "2"])
        self.assertEqual(caught.exception.errors()[0]["type"], "int_type")
        self.assertEqual(caught.exception.errors()[0]["loc"], (1,))
        with self.assertRaises(ValidationError):
            validator.validate_json(b'[1,')


class PydanticTests(unittest.TestCase):
    def test_model_type_adapter_and_json_schema(self):
        from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

        class Payload(BaseModel):
            model_config = ConfigDict(extra="forbid")
            request_id: str
            count: int = Field(ge=1)
            labels: list[str]

        adapter = TypeAdapter(list[Payload])
        instances = adapter.validate_json(
            b'[{"request_id":"fixture","count":"2","labels":["android"]}]'
        )
        self.assertEqual(instances[0].count, 2)
        self.assertEqual(json.loads(adapter.dump_json(instances))[0]["labels"], ["android"])
        schema = Payload.model_json_schema()
        self.assertEqual(schema["properties"]["count"]["minimum"], 1)
        self.assertFalse(schema["additionalProperties"])
        with self.assertRaises(ValidationError) as caught:
            Payload(request_id="fixture", count=0, labels=[])
        self.assertEqual(caught.exception.errors()[0]["type"], "greater_than_equal")
        with self.assertRaises(ValidationError):
            Payload(request_id="fixture", count=1, labels=[], unexpected=True)


class MsgpackTests(unittest.TestCase):
    def test_upstream_fallback_binary_and_extension_roundtrip(self):
        import msgpack
        from msgpack import fallback

        self.assertIs(msgpack.Packer, fallback.Packer)
        self.assertIs(msgpack.Unpacker, fallback.Unpacker)
        value = {
            "text": "Hermes \u03bb", "bytes": b"\x00\xff", "ext": msgpack.ExtType(7, b"native"),
        }
        packed = msgpack.packb(value, use_bin_type=True)
        self.assertEqual(msgpack.unpackb(packed, raw=False), value)
        self.assertEqual(fallback.unpackb(packed, raw=False), value)

    def test_streaming_split_frames_and_invalid_data(self):
        import msgpack

        values = [{"first": b"\x01"}, [1, 2, "last"]]
        stream = b"".join(msgpack.packb(value, use_bin_type=True) for value in values)
        unpacker = msgpack.Unpacker(raw=False)
        decoded = []
        for offset in range(0, len(stream), 3):
            unpacker.feed(stream[offset:offset + 3])
            decoded.extend(unpacker)
        self.assertEqual(decoded, values)
        with self.assertRaises(msgpack.ExtraData):
            msgpack.unpackb(stream, raw=False)
        with self.assertRaises(msgpack.FormatError):
            msgpack.unpackb(b"\xc1")


def assert_aiohttp_native_reader_facade():
    reader = importlib.import_module("aiohttp._websocket.reader")
    native = importlib.import_module("aiohttp._websocket.reader_c")
    for name in ("WebSocketReader", "WebSocketDataQueue"):
        if getattr(reader, name) is not getattr(native, name):
            raise AssertionError(f"aiohttp reader facade does not use native {name}")


class AiohttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_loopback_http_and_websocket(self):
        import aiohttp
        from aiohttp import web

        # Native modules being importable is insufficient if the public facade
        # selected the Python fallback (for example through NO_EXTENSIONS).
        assert_aiohttp_native_reader_facade()

        async def echo(request):
            body = await request.json()
            return web.json_response({"body": body, "query": request.query["lane"]})

        async def websocket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for message in ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await ws.send_str("echo:" + message.data)
                elif message.type == aiohttp.WSMsgType.BINARY:
                    await ws.send_bytes(message.data[::-1])
            return ws

        app = web.Application()
        app.router.add_post("/echo", echo)
        app.router.add_get("/ws", websocket)
        runner = web.AppRunner(app)
        fixture_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        fixture_socket.bind(("127.0.0.1", 0))
        port = fixture_socket.getsockname()[1]
        try:
            async with asyncio.timeout(15):
                await runner.setup()
                await web.SockSite(runner, fixture_socket).start()
                base = f"http://127.0.0.1:{port}"
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=5), trust_env=False
                ) as client:
                    async with client.post(
                        base + "/echo?lane=android", json={"count": 3}
                    ) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(await response.json(), {
                            "body": {"count": 3}, "query": "android",
                        })
                    async with client.ws_connect(base + "/ws") as ws:
                        await ws.send_str("Hermes \u03bb")
                        self.assertEqual(await ws.receive_str(), "echo:Hermes \u03bb")
                        await ws.send_bytes(b"\x00\x01\xff")
                        self.assertEqual(await ws.receive_bytes(), b"\xff\x01\x00")
        finally:
            await runner.cleanup()
            fixture_socket.close()


def require_fixture(condition, detail):
    # Unlike a language-level assert, this remains active under python -O.
    if not condition:
        raise AssertionError(detail)


def openai_fixture(request):
    """Return real API-shaped JSON through a test transport, never a fake SDK."""
    import httpx

    require_fixture(request.method == "POST" and request.url.path == "/v1/chat/completions",
                    "Unexpected OpenAI route")
    require_fixture(request.url.host == "hermes.fixture.invalid", "Unexpected OpenAI host")
    require_fixture(request.headers["authorization"] == f"Bearer {FIXTURE_KEY}",
                    "Unexpected OpenAI authorization")
    payload = json.loads(request.content)
    require_fixture(payload["model"] == "fixture-model", "Unexpected OpenAI model")
    require_fixture(payload["messages"] == [{"role": "user", "content": "ping"}],
                    "Unexpected OpenAI payload")
    return httpx.Response(200, json={
        "id": "chatcmpl-fixture", "object": "chat.completion", "created": 1,
        "model": "fixture-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })


def sse_bytes(events, *, named=False, done=False):
    """Encode the public SSE wire format, including comments and CRLF delimiters."""
    frames = [": hermes offline fixture\r\n\r\n"]
    for event in events:
        if named:
            frames.append(f"event: {event['type']}\r\n")
        frames.append("data: " + json.dumps(event, ensure_ascii=False) + "\r\n\r\n")
    if done:
        frames.append("data: [DONE]\r\n\r\n")
    return "".join(frames).encode("utf-8")


def fragmented_sse_response(events, *, named=False, done=False):
    import httpx

    body = sse_bytes(events, named=named, done=done)

    class FragmentedStream(httpx.SyncByteStream, httpx.AsyncByteStream):
        def __iter__(self):
            # One-byte fragments deliberately split UTF-8, JSON and SSE separators.
            return (body[index:index + 1] for index in range(len(body)))

        async def __aiter__(self):
            for chunk in self:
                yield chunk

    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=FragmentedStream()
    )


def openai_chat_stream_fixture(request):
    # Reuse the non-streaming handler's method, host, auth and message assertions.
    openai_fixture(request)
    payload = json.loads(request.content)
    require_fixture(payload.get("stream") is True, "Chat streaming was not requested")
    require_fixture(payload.get("stream_options") == {"include_usage": True},
                    "Chat streaming usage was not requested")
    response_format = payload.get("response_format")
    if response_format is not None:
        require_fixture(response_format["type"] == "json_schema", "Missing structured format")
        schema = response_format["json_schema"]
        require_fixture(schema["strict"] is True, "Structured output schema must be strict")
        require_fixture(schema["schema"]["properties"]["answer"]["type"] == "string",
                        "Unexpected structured output schema")
        fragments = ['{"answer":"', "po", "ng \u03bb", '"}']
    else:
        fragments = ["po", "ng \u03bb"]

    common = {
        "id": "chatcmpl-stream-fixture", "object": "chat.completion.chunk",
        "created": 1, "model": "fixture-model",
    }

    def chunk(delta, finish_reason=None):
        return {**common, "choices": [{
            "index": 0, "delta": delta, "finish_reason": finish_reason,
        }]}

    events = [chunk({"role": "assistant", "content": ""})]
    events.extend(chunk({"content": fragment}) for fragment in fragments)
    events.append(chunk({}, "stop"))
    events.append({**common, "choices": [], "usage": {
        "prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3,
    }})
    return fragmented_sse_response(events, done=True)


def response_fixture_payload(*, completed=True):
    content = {"type": "output_text", "text": "pong \u03bb", "annotations": [], "logprobs": []}
    message = {
        "id": "msg-response-fixture", "type": "message", "role": "assistant",
        "status": "completed", "content": [content],
    }
    return {
        "id": "resp-fixture", "object": "response", "created_at": 1,
        "model": "fixture-model", "status": "completed" if completed else "in_progress",
        "output": [message] if completed else [], "parallel_tool_calls": True,
        "tool_choice": "auto", "tools": [], "error": None, "incomplete_details": None,
        "usage": {
            "input_tokens": 1,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens": 2, "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 3,
        } if completed else None,
    }


def response_fixture_events(*, completed=True):
    final = response_fixture_payload()
    message = final["output"][0]
    content = message["content"][0]
    location = {"item_id": message["id"], "output_index": 0, "content_index": 0}
    events = [
        {"type": "response.created", "response": response_fixture_payload(completed=False)},
        {"type": "response.in_progress", "response": response_fixture_payload(completed=False)},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {**message, "status": "in_progress", "content": []}},
        {"type": "response.content_part.added", **location, "part": {**content, "text": ""}},
        {"type": "response.output_text.delta", **location, "delta": "po", "logprobs": []},
        {"type": "response.output_text.delta", **location, "delta": "ng \u03bb", "logprobs": []},
        {"type": "response.output_text.done", **location, "text": "pong \u03bb", "logprobs": []},
        {"type": "response.content_part.done", **location, "part": content},
        {"type": "response.output_item.done", "output_index": 0, "item": message},
    ]
    if completed:
        events.append({"type": "response.completed", "response": final})
    return [{**event, "sequence_number": index} for index, event in enumerate(events)]


def openai_responses_fixture(request, *, completed=True):
    import httpx

    require_fixture(request.method == "POST" and request.url.path == "/v1/responses",
                    "Unexpected Responses route")
    require_fixture(request.url.host == "hermes.fixture.invalid", "Unexpected Responses host")
    require_fixture(request.headers["authorization"] == f"Bearer {FIXTURE_KEY}",
                    "Unexpected Responses authorization")
    payload = json.loads(request.content)
    require_fixture(payload["model"] == "fixture-model", "Unexpected Responses model")
    require_fixture(payload["input"] == "ping" and payload["max_output_tokens"] == 16,
                    "Unexpected Responses input or output-token limit")
    if payload.get("stream") is True:
        return fragmented_sse_response(response_fixture_events(completed=completed), named=True)
    require_fixture(payload.get("stream") in (None, False), "Unexpected Responses stream value")
    return httpx.Response(200, json=response_fixture_payload())


@contextmanager
def offline_openai_client(handler):
    import httpx
    from openai import OpenAI

    responses = []

    def record_response(request):
        response = handler(request)
        responses.append(response)
        return response

    with httpx.Client(
        transport=httpx.MockTransport(record_response), trust_env=False, timeout=5
    ) as transport:
        with OpenAI(
            api_key=FIXTURE_KEY, base_url="https://hermes.fixture.invalid/v1",
            http_client=transport, max_retries=0, timeout=5,
        ) as client:
            yield client
    require_fixture(responses and all(response.is_closed for response in responses),
                    "OpenAI HTTP response was not closed")


@asynccontextmanager
async def offline_async_openai_client(handler):
    import httpx
    from openai import AsyncOpenAI

    responses = []

    def record_response(request):
        response = handler(request)
        responses.append(response)
        return response

    async with asyncio.timeout(15):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(record_response), trust_env=False, timeout=5
        ) as transport:
            async with AsyncOpenAI(
                api_key=FIXTURE_KEY, base_url="https://hermes.fixture.invalid/v1",
                http_client=transport, max_retries=0, timeout=5,
            ) as client:
                yield client
    require_fixture(responses and all(response.is_closed for response in responses),
                    "Async OpenAI HTTP response was not closed")


def assert_response_result(test, response):
    test.assertEqual(response.id, "resp-fixture")
    test.assertEqual(response.status, "completed")
    test.assertEqual(response.output_text, "pong \u03bb")
    test.assertEqual(response.output[0].role, "assistant")
    test.assertEqual(response.usage.total_tokens, 3)
    test.assertEqual(response.model_dump()["output"][0]["content"][0]["text"], "pong \u03bb")


def assert_response_events(test, events):
    test.assertEqual([event.type for event in events], [
        event["type"] for event in response_fixture_events()
    ])
    test.assertEqual([event.sequence_number for event in events], list(range(10)))
    test.assertEqual("".join(event.delta for event in events
                             if event.type == "response.output_text.delta"), "pong \u03bb")
    assert_response_result(test, events[-1].response)


class OpenAIResponsesAndStreamsTests(unittest.TestCase):
    def test_responses_create(self):
        with offline_openai_client(openai_responses_fixture) as client:
            response = client.responses.create(
                model="fixture-model", input="ping", max_output_tokens=16
            )
        assert_response_result(self, response)

    def test_chat_sse_raw_and_structured_helper(self):
        from pydantic import BaseModel

        class Reply(BaseModel):
            answer: str

        with offline_openai_client(openai_chat_stream_fixture) as client:
            with self.subTest(mode="create-stream"), client.chat.completions.create(
                model="fixture-model", messages=[{"role": "user", "content": "ping"}],
                stream=True, stream_options={"include_usage": True},
            ) as stream:
                chunks = list(stream)
                text = "".join(choice.delta.content or ""
                               for chunk in chunks for choice in chunk.choices)
                self.assertEqual(text, "pong \u03bb")
                self.assertEqual(chunks[0].choices[0].delta.role, "assistant")
                self.assertEqual(chunks[-2].choices[0].finish_reason, "stop")
                self.assertEqual(chunks[-1].usage.total_tokens, 3)
            with self.subTest(mode="structured-stream-helper"), client.chat.completions.stream(
                model="fixture-model", messages=[{"role": "user", "content": "ping"}],
                response_format=Reply, stream_options={"include_usage": True},
            ) as stream:
                events = list(stream)
                response = stream.get_final_completion()
                self.assertEqual(response.choices[0].message.parsed.answer, "pong \u03bb")
                self.assertEqual(response.usage.total_tokens, 3)
                self.assertTrue(any(event.type == "content.delta" for event in events))
                self.assertEqual(next(event.parsed for event in events
                                      if event.type == "content.done").answer, "pong \u03bb")

    def test_responses_sse_create_and_stream_helper(self):
        with offline_openai_client(openai_responses_fixture) as client:
            with self.subTest(mode="create-stream"), client.responses.create(
                model="fixture-model", input="ping", max_output_tokens=16, stream=True
            ) as stream:
                assert_response_events(self, list(stream))
            with self.subTest(mode="stream-helper"), client.responses.stream(
                model="fixture-model", input="ping", max_output_tokens=16
            ) as stream:
                events = list(stream)
                assert_response_events(self, events)
                snapshots = [event.snapshot for event in events
                             if event.type == "response.output_text.delta"]
                self.assertEqual(snapshots, ["po", "pong \u03bb"])
                assert_response_result(self, stream.get_final_response())

    def test_responses_helper_rejects_missing_completion(self):
        with offline_openai_client(
            lambda request: openai_responses_fixture(request, completed=False)
        ) as client:
            with client.responses.stream(
                model="fixture-model", input="ping", max_output_tokens=16
            ) as stream:
                with self.assertRaisesRegex(RuntimeError, "response.completed"):
                    stream.get_final_response()


class OpenAIAsyncResponsesAndStreamsTests(unittest.IsolatedAsyncioTestCase):
    async def test_responses_create(self):
        async with offline_async_openai_client(openai_responses_fixture) as client:
            response = await client.responses.create(
                model="fixture-model", input="ping", max_output_tokens=16
            )
        assert_response_result(self, response)

    async def test_chat_sse_raw_and_structured_helper(self):
        from pydantic import BaseModel

        class Reply(BaseModel):
            answer: str

        async with offline_async_openai_client(openai_chat_stream_fixture) as client:
            with self.subTest(mode="create-stream"):
                async with await client.chat.completions.create(
                    model="fixture-model", messages=[{"role": "user", "content": "ping"}],
                    stream=True, stream_options={"include_usage": True},
                ) as stream:
                    chunks = [chunk async for chunk in stream]
                    text = "".join(choice.delta.content or ""
                                   for chunk in chunks for choice in chunk.choices)
                    self.assertEqual(text, "pong \u03bb")
                    self.assertEqual(chunks[-2].choices[0].finish_reason, "stop")
                    self.assertEqual(chunks[-1].usage.total_tokens, 3)
            with self.subTest(mode="structured-stream-helper"):
                async with client.chat.completions.stream(
                    model="fixture-model", messages=[{"role": "user", "content": "ping"}],
                    response_format=Reply, stream_options={"include_usage": True},
                ) as stream:
                    events = [event async for event in stream]
                    response = await stream.get_final_completion()
                    self.assertEqual(response.choices[0].message.parsed.answer, "pong \u03bb")
                    self.assertEqual(response.usage.total_tokens, 3)
                    self.assertEqual(next(event.parsed for event in events
                                          if event.type == "content.done").answer, "pong \u03bb")

    async def test_responses_sse_create_and_stream_helper(self):
        async with offline_async_openai_client(openai_responses_fixture) as client:
            with self.subTest(mode="create-stream"):
                async with await client.responses.create(
                    model="fixture-model", input="ping", max_output_tokens=16, stream=True
                ) as stream:
                    assert_response_events(self, [event async for event in stream])
            with self.subTest(mode="stream-helper"):
                async with client.responses.stream(
                    model="fixture-model", input="ping", max_output_tokens=16
                ) as stream:
                    events = [event async for event in stream]
                    assert_response_events(self, events)
                    snapshots = [event.snapshot for event in events
                                 if event.type == "response.output_text.delta"]
                    self.assertEqual(snapshots, ["po", "pong \u03bb"])
                    assert_response_result(self, await stream.get_final_response())

    async def test_responses_helper_rejects_missing_completion(self):
        async with offline_async_openai_client(
            lambda request: openai_responses_fixture(request, completed=False)
        ) as client:
            async with client.responses.stream(
                model="fixture-model", input="ping", max_output_tokens=16
            ) as stream:
                with self.assertRaisesRegex(RuntimeError, "response.completed"):
                    await stream.get_final_response()


def anthropic_fixture(request):
    import httpx2 as httpx

    require_fixture(request.method == "POST" and request.url.path == "/v1/messages",
                    "Unexpected Anthropic route")
    require_fixture(request.url.host == "hermes.fixture.invalid", "Unexpected Anthropic host")
    require_fixture(request.headers["x-api-key"] == FIXTURE_KEY, "Unexpected Anthropic key")
    require_fixture(request.headers["anthropic-version"] == "2026-10-21",
                    "Unexpected Anthropic API version")
    payload = json.loads(request.content)
    require_fixture(payload["model"] == "fixture-model" and payload["max_tokens"] == 16,
                    "Unexpected Anthropic parameters")
    require_fixture(payload["messages"] == [{"role": "user", "content": "ping"}],
                    "Unexpected Anthropic messages")
    return httpx.Response(200, json={
        "id": "msg-fixture", "type": "message", "role": "assistant",
        "model": "fixture-model", "content": [{"type": "text", "text": "pong"}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })


class SDKSyncTests(unittest.TestCase):
    def test_openai_genuine_sync_client(self):
        import httpx
        from openai import OpenAI

        with httpx.Client(
            transport=httpx.MockTransport(openai_fixture), trust_env=False
        ) as transport:
            with OpenAI(api_key=FIXTURE_KEY, base_url="https://hermes.fixture.invalid/v1",
                        http_client=transport, max_retries=0) as client:
                response = client.chat.completions.create(
                    model="fixture-model", messages=[{"role": "user", "content": "ping"}]
                )
                self.assertEqual(response.choices[0].message.content, "pong")
                self.assertEqual(response.usage.total_tokens, 2)
                self.assertEqual(response.model_dump()["id"], "chatcmpl-fixture")

    def test_anthropic_genuine_sync_client(self):
        import httpx2 as httpx
        from anthropic import Anthropic

        with httpx.Client(
            transport=httpx.MockTransport(anthropic_fixture), trust_env=False
        ) as transport:
            with Anthropic(api_key=FIXTURE_KEY, base_url="https://hermes.fixture.invalid",
                           http_client=transport, max_retries=0) as client:
                response = client.messages.create(
                    model="fixture-model", max_tokens=16,
                    messages=[{"role": "user", "content": "ping"}],
                )
                self.assertEqual(response.content[0].text, "pong")
                self.assertEqual(response.usage.output_tokens, 1)
                self.assertEqual(response.model_dump()["id"], "msg-fixture")


class SDKAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_genuine_async_client(self):
        import httpx
        from openai import AsyncOpenAI

        async with asyncio.timeout(15):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(openai_fixture), trust_env=False
            ) as transport:
                async with AsyncOpenAI(
                    api_key=FIXTURE_KEY, base_url="https://hermes.fixture.invalid/v1",
                    http_client=transport, max_retries=0,
                ) as client:
                    response = await client.chat.completions.create(
                        model="fixture-model", messages=[{"role": "user", "content": "ping"}]
                    )
                    self.assertEqual(response.choices[0].message.content, "pong")
                    self.assertEqual(response.usage.total_tokens, 2)

    async def test_anthropic_genuine_async_client(self):
        import httpx2 as httpx
        from anthropic import AsyncAnthropic

        async with asyncio.timeout(15):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(anthropic_fixture), trust_env=False
            ) as transport:
                async with AsyncAnthropic(
                    api_key=FIXTURE_KEY, base_url="https://hermes.fixture.invalid",
                    http_client=transport, max_retries=0,
                ) as client:
                    response = await client.messages.create(
                        model="fixture-model", max_tokens=16,
                        messages=[{"role": "user", "content": "ping"}],
                    )
                    self.assertEqual(response.content[0].text, "pong")
                    self.assertEqual(response.usage.input_tokens, 1)


def fal_fixture(request):
    import httpx

    require_fixture(request.method == "POST" and request.url.path == "/fixture/model",
                    "Unexpected FAL route")
    require_fixture(request.url.host == "fal.fixture.invalid", "Unexpected FAL host")
    require_fixture(request.headers["authorization"] == f"Key {FIXTURE_KEY}",
                    "Unexpected FAL authorization")
    require_fixture(json.loads(request.content) == {"prompt": "ping"}, "Unexpected FAL payload")
    return httpx.Response(200, json={"output": "pong", "fixture": True})


class FalSyncTests(unittest.TestCase):
    def test_genuine_client_serialization_and_encoding(self):
        import fal_client
        import fal_client.client as implementation
        import httpx

        real_client = httpx.Client
        opened = []

        def fixture_client(**kwargs):
            client = real_client(
                transport=httpx.MockTransport(fal_fixture), trust_env=False, **kwargs
            )
            opened.append(client)
            return client

        # fal-client 1.0.1 has no public transport parameter. Replace only its
        # HTTP-client factory, retaining the genuine auth, run and JSON code.
        try:
            with (
                mock.patch.object(implementation.httpx, "Client", side_effect=fixture_client),
                mock.patch.object(implementation, "RUN_URL_FORMAT", "https://fal.fixture.invalid/"),
            ):
                client = fal_client.SyncClient(key=FIXTURE_KEY)
                self.assertEqual(client.run("fixture/model", arguments={"prompt": "ping"}), {
                    "output": "pong", "fixture": True,
                })
            encoded = fal_client.encode(b"\x00\xff", "application/octet-stream")
            prefix, payload = encoded.split(",", 1)
            self.assertEqual(prefix, "data:application/octet-stream;base64")
            self.assertEqual(base64.b64decode(payload), b"\x00\xff")
        finally:
            for client in opened:
                client.close()


class FalAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_genuine_async_client(self):
        import fal_client
        import fal_client.client as implementation
        import httpx

        real_client = httpx.AsyncClient
        opened = []

        def fixture_client(**kwargs):
            client = real_client(
                transport=httpx.MockTransport(fal_fixture), trust_env=False, **kwargs
            )
            opened.append(client)
            return client

        try:
            async with asyncio.timeout(15):
                with (
                    mock.patch.object(
                        implementation.httpx, "AsyncClient", side_effect=fixture_client
                    ),
                    mock.patch.object(
                        implementation, "RUN_URL_FORMAT", "https://fal.fixture.invalid/"
                    ),
                ):
                    client = fal_client.AsyncClient(key=FIXTURE_KEY)
                    response = await client.run("fixture/model", arguments={"prompt": "ping"})
                    self.assertEqual(response, {"output": "pong", "fixture": True})
        finally:
            for client in opened:
                await client.aclose()


TEST_GROUPS = {
    "jiter": (JiterTests,),
    "pydantic-core": (PydanticCoreTests,),
    "full": (JiterTests, PydanticCoreTests, PydanticTests, MsgpackTests, AiohttpTests,
             SDKSyncTests, SDKAsyncTests, FalSyncTests, FalAsyncTests,
             OpenAIResponsesAndStreamsTests, OpenAIAsyncResponsesAndStreamsTests),
}


def build_suite(package):
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for case in TEST_GROUPS[package]:
        suite.addTests(loader.loadTestsFromTestCase(case))
    return suite


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.append(test.id())


def run(package, require_android=False):
    started = time.monotonic()
    report = {
        "schema_version": 1,
        "package_group": package,
        "require_android": require_android,
        "status": "failed",
        "platform": platform_evidence(),
        "packages": [],
        "network_scope": "loopback-and-in-memory-fixtures-only",
        "service_inference_verified": False,
    }
    try:
        validate_platform(report["platform"], require_android)
        if package == "full":
            prepare_msgpack()
        for name in PACKAGE_GROUPS[package]:
            report["packages"].append(package_evidence(name))
        result = unittest.TextTestRunner(
            verbosity=2, stream=sys.stderr, resultclass=EvidenceResult
        ).run(build_suite(package))
        report["tests"] = {
            "run": result.testsRun,
            "passed": result.passed,
            "failures": [{"test": test.id(), "traceback": text} for test, text in result.failures],
            "errors": [{"test": test.id(), "traceback": text} for test, text in result.errors],
            "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
        }
        if result.wasSuccessful() and result.testsRun > 0 and not result.skipped:
            report["status"] = "passed"
    except Exception as error:
        report["preflight_error"] = f"{type(error).__name__}: {error}"
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["android_execution_verified"] = (
        report["status"] == "passed" and report["platform"]["is_android"]
    )
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", choices=tuple(PACKAGE_GROUPS), default="full")
    parser.add_argument("--require-android", action="store_true")
    parser.add_argument("--json-output", type=Path, help="Also persist the complete JSON evidence")
    parser.add_argument("--log-marker", action="store_true",
                        help="Prefix the stdout report with a distinctive logcat marker")
    args = parser.parse_args(argv)
    report = run(args.package, args.require_android)
    output = json.dumps(report, ensure_ascii=True, sort_keys=True)
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(output + "\n", encoding="utf-8")
    print((LOG_MARKER if args.log_marker else "") + output, flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
