# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

"""Alias names for published models.

Requests: an alias in the path, query or JSON body is translated to the real model reference
before routing. Aliases are unique and never contain ``/`` or ``:``, so this is safe for any caller.

Responses: for non-owner accounts only, every spelling of a published model's real name in a JSON
or event-stream body is replaced by its alias, so other accounts never see real names.

Pure ASGI and installed innermost: the owner/non-owner decision reads the account bound by the
auth dependency, which only the task that runs the endpoint can see.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode

from auth import model_policy
from utils.account_context import is_owner_context

PREFIXES = ("/api/models", "/api/inference", "/api/hub", "/api/picker", "/v1")
_MAX_BODY = 16 * 1024 * 1024
_BODY_METHODS = {"POST", "PUT", "PATCH"}


def _swap_reference(value: str) -> str:
    real = model_policy.resolve_reference(value)
    return value if real is None else real


def _rewrite_values(obj: Any) -> Any:
    if isinstance(obj, str):
        return _swap_reference(obj)
    if isinstance(obj, list):
        return [_rewrite_values(item) for item in obj]
    if isinstance(obj, dict):
        return {key: _rewrite_values(value) for key, value in obj.items()}
    return obj


def mask_text(text: str) -> str:
    pattern, mapping = model_policy.mask_pattern()
    if pattern is None:
        return text
    return pattern.sub(lambda m: mapping.get(m.group(0).lower(), m.group(0)), text)


def mask_value(obj: Any) -> Any:
    """Mask string values only; keys such as ``"model"`` must keep their spelling."""
    if isinstance(obj, str):
        return mask_text(obj)
    if isinstance(obj, list):
        return [mask_value(item) for item in obj]
    if isinstance(obj, dict):
        return {key: mask_value(value) for key, value in obj.items()}
    return obj


def _mask_json_bytes(body: bytes) -> bytes:
    try:
        return json.dumps(mask_value(json.loads(body)), ensure_ascii = False).encode("utf-8")
    except ValueError:
        return mask_text(body.decode("utf-8", "replace")).encode("utf-8")


def _mask_event(event: bytes) -> bytes:
    out = []
    for line in event.split(b"\n"):
        if line.startswith(b"data:"):
            payload = line[5:].strip()
            if payload and payload != b"[DONE]":
                try:
                    line = b"data: " + json.dumps(
                        mask_value(json.loads(payload)), ensure_ascii = False
                    ).encode("utf-8")
                except ValueError:
                    line = b"data: " + mask_text(payload.decode("utf-8", "replace")).encode("utf-8")
        out.append(line)
    return b"\n".join(out)


class ModelAliasMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(PREFIXES):
            return await self.app(scope, receive, send)
        if not model_policy.get_catalog():
            return await self.app(scope, receive, send)
        receive = await self._rewrite_request(scope, receive)
        return await self.app(scope, receive, self._masking_send(send))

    async def _rewrite_request(self, scope, receive):
        path = unquote(scope["path"])
        segments = path.split("/")
        swapped = [_swap_reference(seg) if seg else seg for seg in segments]
        if swapped != segments:
            new_path = "/".join(swapped)
            scope["path"] = new_path
            scope["raw_path"] = quote(new_path, safe = "/:").encode("ascii")
        query = scope.get("query_string", b"")
        if query:
            pairs = parse_qsl(query.decode("latin-1"), keep_blank_values = True)
            new_pairs = [(k, _swap_reference(v)) for k, v in pairs]
            if new_pairs != pairs:
                scope["query_string"] = urlencode(new_pairs).encode("ascii")
        if scope.get("method") not in _BODY_METHODS:
            return receive
        headers = dict(scope.get("headers", []))
        if b"json" not in headers.get(b"content-type", b""):
            return receive
        try:
            length = int(headers.get(b"content-length", b""))
        except ValueError:
            return receive
        if not 0 < length <= _MAX_BODY:
            return receive
        chunks, more = [], True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        body = b"".join(chunks)
        try:
            new_body = json.dumps(_rewrite_values(json.loads(body)), ensure_ascii = False).encode("utf-8")
        except ValueError:
            new_body = body
        if new_body != body:
            scope["headers"] = [
                (k, str(len(new_body)).encode() if k == b"content-length" else v)
                for k, v in scope["headers"]
            ]
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": new_body, "more_body": False}
            return await receive()

        return replay

    def _masking_send(self, send):
        state = {"mode": None, "start": None, "buffer": b""}

        async def masked(message):
            kind = message["type"]
            if kind == "http.response.start":
                headers = dict(message.get("headers", []))
                ctype = headers.get(b"content-type", b"")
                mode = None
                if not is_owner_context():
                    if b"text/event-stream" in ctype:
                        mode = "sse"
                    elif b"json" in ctype:
                        mode = "json"
                state["mode"] = mode
                if mode is None:
                    return await send(message)
                if mode == "sse":
                    return await send(message)
                state["start"] = message
                return None
            if state["mode"] is None:
                return await send(message)
            body, more = message.get("body", b""), message.get("more_body", False)
            if state["mode"] == "json":
                state["buffer"] += body
                if more:
                    return None
                out = _mask_json_bytes(state["buffer"])
                start = dict(state["start"])
                start["headers"] = [
                    (k, str(len(out)).encode() if k.lower() == b"content-length" else v)
                    for k, v in start.get("headers", [])
                ]
                await send(start)
                return await send({"type": "http.response.body", "body": out, "more_body": False})
            # Event stream: mask whole events, hold back a partial one until it completes.
            state["buffer"] += body
            *events, state["buffer"] = state["buffer"].split(b"\n\n")
            if not more and state["buffer"]:
                events.append(state["buffer"])
                state["buffer"] = b""
            out = b"".join(_mask_event(e) + b"\n\n" for e in events)
            return await send({"type": "http.response.body", "body": out, "more_body": more})

        return masked
