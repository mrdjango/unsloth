# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

import json

import pytest

from auth import model_policy
from utils import model_alias_middleware as mw


@pytest.fixture(autouse = True)
def studio_home(tmp_path, monkeypatch):
    monkeypatch.setenv("UNSLOTH_STUDIO_HOME", str(tmp_path))
    model_policy._cache = None


def test_catalog_roundtrip_and_generated_alias():
    assert model_policy.get_catalog() == []
    saved = model_policy.set_catalog(
        [
            {"model_id": "Org/Big-Model-8B", "alias": "Assistant"},
            {"model_id": "org/big-model-8b", "alias": "dup"},
            {"model_id": "x/other-3b", "alias": ""},
        ]
    )
    assert saved == [
        {"model_id": "Org/Big-Model-8B", "alias": "Assistant"},
        {"model_id": "x/other-3b", "alias": "Model 1"},
    ]
    assert model_policy.is_published("ORG/big-model-8b")
    assert not model_policy.is_published("a/b")
    assert model_policy.alias_for("x/other-3b") == "Model 1"
    assert model_policy.resolve_reference("assistant") == "Org/Big-Model-8B"
    assert model_policy.resolve_reference("Assistant:Q4_K_M") == "Org/Big-Model-8B:Q4_K_M"
    assert model_policy.resolve_reference("unknown") is None


@pytest.mark.parametrize(
    "entries",
    [
        [{"model_id": "a/b", "alias": "x/y"}],
        [{"model_id": "a/b", "alias": "x:y"}],
        [{"model_id": "a/b", "alias": "same"}, {"model_id": "c/d", "alias": "SAME"}],
        [{"model_id": "a/b", "alias": "a/b"}],
        [{"model_id": "", "alias": "x"}],
    ],
)
def test_catalog_rejects_bad_entries(entries):
    with pytest.raises(ValueError):
        model_policy.set_catalog(entries)


def test_masking_hides_every_real_spelling_but_not_keys():
    model_policy.set_catalog([{"model_id": "unsloth/Qwen3-8B-GGUF", "alias": "Helper"}])
    masked = mw.mask_value(
        {
            "model": "unsloth/Qwen3-8B-GGUF",
            "path": "/cache/models--unsloth--Qwen3-8B-GGUF/snap",
            "display": "Qwen3-8B-GGUF ready",
            "other": "unsloth/something-else",
        }
    )
    assert masked["model"] == "Helper"
    assert "Qwen3" not in json.dumps(masked)
    assert masked["other"] == "unsloth/something-else"
    assert "model" in masked


def test_request_rewrite_translates_aliases_only():
    model_policy.set_catalog([{"model_id": "org/real-name-1", "alias": "Helper"}])
    assert mw._rewrite_values({"model": "Helper", "n": 1, "m": ["Helper:Q4", "keep"]}) == {
        "model": "org/real-name-1",
        "n": 1,
        "m": ["org/real-name-1:Q4", "keep"],
    }


def test_masking_covers_url_encoded_id():
    model_policy.set_catalog([{"model_id": "MCG-NJU/videomae-base", "alias": "Helper"}])
    assert mw.mask_text("cache:safetensors:MCG-NJU%2Fvideomae-base") == "cache:safetensors:Helper"


def _run_asgi(app, *, method = "GET", path = "/v1/x", body = b"", headers = None, user = None):
    import asyncio

    from utils import account_context

    sent: list[dict] = []

    async def drive():
        if user is not None:
            account_context.bind_account(user)
        hdrs = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers or hdrs,
        }
        messages = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        await mw.ModelAliasMiddleware(app)(scope, receive, send)

    asyncio.run(drive())
    return sent


def _sse_app(chunks, ctype = b"text/event-stream"):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", ctype)]})
        for i, chunk in enumerate(chunks):
            await send({"type": "http.response.body", "body": chunk, "more_body": i < len(chunks) - 1})

    return app


def test_sse_masked_for_user_even_when_event_is_split_across_chunks():
    from utils.account_context import AccountContext

    model_policy.set_catalog([{"model_id": "org/Secret-Model-7B", "alias": "Helper"}])
    event = b'data: {"model": "org/Secret-Model-7B", "delta": "hi"}\n\n'
    chunks = [event[:25], event[25:] + b"data: [DONE]\n\n"]
    user = AccountContext("u1", "alice", "user")
    out = b"".join(m.get("body", b"") for m in _run_asgi(_sse_app(chunks), user = user))
    assert b"Secret" not in out and b'"model": "Helper"' in out and b"[DONE]" in out


def test_json_masked_for_user_but_not_owner():
    from utils.account_context import OWNER, AccountContext

    model_policy.set_catalog([{"model_id": "org/Secret-Model-7B", "alias": "Helper"}])
    payload = json.dumps({"id": "org/Secret-Model-7B"}).encode()

    async def app(scope, receive, send):
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": payload})

    user_out = _run_asgi(app, user = AccountContext("u1", "alice", "user"))
    assert json.loads(user_out[1]["body"]) == {"id": "Helper"}
    assert dict(user_out[0]["headers"])[b"content-length"] == str(len(user_out[1]["body"])).encode()
    owner_out = _run_asgi(app, user = OWNER)
    assert json.loads(owner_out[1]["body"]) == {"id": "org/Secret-Model-7B"}


def test_request_body_alias_is_translated_before_the_app_sees_it():
    model_policy.set_catalog([{"model_id": "org/Secret-Model-7B", "alias": "Helper"}])
    seen = {}

    async def app(scope, receive, send):
        message = await receive()
        seen["body"] = json.loads(message["body"])
        seen["length"] = dict(scope["headers"])[b"content-length"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    body = json.dumps({"model": "Helper", "messages": []}).encode()
    _run_asgi(app, method = "POST", body = body)
    assert seen["body"]["model"] == "org/Secret-Model-7B"
    assert seen["length"] == str(len(json.dumps(seen["body"], ensure_ascii = False).encode())).encode()
