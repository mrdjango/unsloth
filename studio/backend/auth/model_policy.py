# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

"""Owner-managed model catalog: the only models other accounts may see or use, under alias names.

Persisted as JSON under the studio root. An entry is ``{"model_id", "alias"}``: ``model_id`` is
the real repo id, ``alias`` the name every non-owner account sees instead. The owner is never
restricted and always sees real ids.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

_lock = threading.Lock()
_cache: tuple[tuple[int, int], list[dict[str, str]]] | None = None
# Bumped whenever the catalog is re-read, so derived data can tell it went stale.
_version = 0

MAX_ALIAS_LEN = 64
# A repo id is "org/name" and "repo:variant" selects a quant, so an alias may use neither.
_BAD_ALIAS = re.compile(r"[/:\\\x00-\x1f]")


def _policy_path() -> Path:
    from utils.paths.storage_roots import studio_root

    return studio_root() / "admin_model_policy.json"


def _read() -> list[dict[str, str]]:
    global _cache, _version
    path = _policy_path()
    try:
        stat = path.stat()
    except OSError:
        with _lock:
            if _cache is not None:
                _cache, _version = None, _version + 1
        return []
    stamp = (stat.st_mtime_ns, stat.st_size)
    with _lock:
        if _cache is not None and _cache[0] == stamp:
            return _cache[1]
    try:
        data = json.loads(path.read_text(encoding = "utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get("models") if isinstance(data, dict) else None
    entries: list[dict[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("model_id"), str) and isinstance(row.get("alias"), str):
            entries.append({"model_id": row["model_id"], "alias": row["alias"]})
    with _lock:
        _cache = (stamp, entries)
        _version += 1
    return entries


def get_catalog() -> list[dict[str, str]]:
    return [dict(entry) for entry in _read()]


def _clean_catalog(entries) -> list[dict[str, str]]:
    """Normalise and validate; raises ``ValueError`` with a message fit for the admin UI."""
    cleaned: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_aliases: set[str] = set()
    pending: list[dict[str, str]] = []
    for entry in entries or []:
        model_id = str((entry or {}).get("model_id", "")).strip()
        alias = str((entry or {}).get("alias", "")).strip()
        if not model_id:
            raise ValueError("A model id is required")
        if model_id.lower() in seen_ids:
            continue
        seen_ids.add(model_id.lower())
        if not alias:
            pending.append({"model_id": model_id, "alias": ""})
            continue
        if len(alias) > MAX_ALIAS_LEN:
            raise ValueError(f"Alias for {model_id} is longer than {MAX_ALIAS_LEN} characters")
        if _BAD_ALIAS.search(alias):
            raise ValueError(f"Alias {alias!r} may not contain '/', ':' or control characters")
        if alias.lower() in seen_aliases:
            raise ValueError(f"Alias {alias!r} is used more than once")
        seen_aliases.add(alias.lower())
        cleaned.append({"model_id": model_id, "alias": alias})
    # Blank aliases get a neutral generated one, so a real name is never shown by omission.
    counter = 0
    for entry in pending:
        while True:
            counter += 1
            alias = f"Model {counter}"
            if alias.lower() not in seen_aliases:
                break
        seen_aliases.add(alias.lower())
        cleaned.append({"model_id": entry["model_id"], "alias": alias})
    for entry in cleaned:
        if entry["alias"].lower() in seen_ids:
            raise ValueError(f"Alias {entry['alias']!r} matches a real model id")
    return sorted(cleaned, key = lambda e: e["alias"].lower())


def set_catalog(entries) -> list[dict[str, str]]:
    global _cache
    catalog = _clean_catalog(entries)
    path = _policy_path()
    with _lock:
        path.parent.mkdir(parents = True, exist_ok = True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"models": catalog}, indent = 2), encoding = "utf-8")
        os.replace(tmp, path)
        _cache = None
    return catalog


def is_published(model_id: str | None) -> bool:
    ref = (model_id or "").strip().lower()
    return bool(ref) and any(e["model_id"].lower() == ref for e in _read())


def alias_for(model_id: str | None) -> str | None:
    ref = (model_id or "").strip().lower()
    for entry in _read():
        if entry["model_id"].lower() == ref:
            return entry["alias"]
    return None


def real_id_for(alias: str | None) -> str | None:
    ref = (alias or "").strip().lower()
    for entry in _read():
        if entry["alias"].lower() == ref:
            return entry["model_id"]
    return None


def resolve_reference(text: str) -> str | None:
    """``alias`` or ``alias:variant`` to the real reference, or ``None`` when no alias matches."""
    real = real_id_for(text)
    if real is not None:
        return real
    head, sep, tail = text.partition(":")
    if sep:
        real = real_id_for(head)
        if real is not None:
            return f"{real}:{tail}"
    return None


_mask_cache: tuple[int, "re.Pattern[str] | None", dict[str, str]] | None = None
_DISTINCTIVE = re.compile(r"[0-9._-]")


def mask_pattern() -> tuple["re.Pattern[str] | None", dict[str, str]]:
    """Regex matching every spelling of a published model's real name, and what each maps to.

    Spellings: ``org/name``, the cache directory forms ``org--name`` / ``models--org--name``, the URL-encoded ``org%2Fname`` and, when the bare ``name`` is
    distinctive enough to not mangle ordinary words, ``name`` itself.
    """
    global _mask_cache
    entries = _read()
    key = _version
    if _mask_cache is not None and _mask_cache[0] == key:
        return _mask_cache[1], _mask_cache[2]
    mapping: dict[str, str] = {}
    for entry in entries:
        real, alias = entry["model_id"], entry["alias"]
        spellings = {
            real,
            real.replace("/", "--"),
            "models--" + real.replace("/", "--"),
            real.replace("/", "%2F"),
        }
        name = real.rsplit("/", 1)[-1]
        if len(name) >= 5 and _DISTINCTIVE.search(name):
            spellings.add(name)
        for spelling in spellings:
            mapping.setdefault(spelling.lower(), alias)
    pattern = None
    if mapping:
        alternatives = "|".join(re.escape(s) for s in sorted(mapping, key = len, reverse = True))
        pattern = re.compile(rf"(?<![A-Za-z0-9._-])(?:{alternatives})(?![A-Za-z0-9_-])", re.IGNORECASE)
    _mask_cache = (key, pattern, mapping)
    return pattern, mapping
