"""Exact and near-duplicate grouping for normalized source text."""
from __future__ import annotations

import hashlib
import re
from typing import Iterable

from .normalize import normalize_text


UPDATE_STAMP = re.compile(r"更新时间\s*[:：]?\s*\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?")
INLINE_NAV = re.compile(r"(?:首页|就医指南|当前位置)\s*")


def _fingerprint_text(value: str) -> str:
    text = normalize_text(value)
    text = UPDATE_STAMP.sub("", text)
    text = INLINE_NAV.sub("", text)
    return re.sub(r"\s+", "", text).strip()


def _grams(value: str, width: int = 3) -> set[str]:
    if len(value) < width:
        return set()
    return {value[index:index + width] for index in range(len(value) - width + 1)}


def _similarity(left: str, right: str) -> float:
    if left == right and left:
        return 1.0
    left_grams, right_grams = _grams(left), _grams(right)
    if not left_grams or not right_grams:
        return 0.0
    return len(left_grams & right_grams) / len(left_grams | right_grams)


def duplicate_groups(
    rows: Iterable[tuple[str, str]], *, threshold: float = 0.90
) -> tuple[tuple[str, ...], ...]:
    if not 0 <= threshold <= 1:
        raise ValueError("Duplicate threshold must be between 0 and 1")
    normalized = sorted((str(identity), _fingerprint_text(text)) for identity, text in rows)
    if len({identity for identity, _ in normalized}) != len(normalized):
        raise ValueError("Duplicate row identities")
    parents = {identity: identity for identity, _ in normalized}

    def find(identity: str) -> str:
        while parents[identity] != identity:
            parents[identity] = parents[parents[identity]]
            identity = parents[identity]
        return identity

    def union(left: str, right: str) -> None:
        first, second = find(left), find(right)
        if first != second:
            parents[max(first, second)] = min(first, second)

    for index, (left_id, left_text) in enumerate(normalized):
        left_hash = hashlib.sha256(left_text.encode("utf-8")).digest()
        for right_id, right_text in normalized[index + 1:]:
            right_hash = hashlib.sha256(right_text.encode("utf-8")).digest()
            exact = bool(left_text) and left_hash == right_hash
            if exact or _similarity(left_text, right_text) >= threshold:
                union(left_id, right_id)
    groups: dict[str, list[str]] = {}
    for identity, _ in normalized:
        groups.setdefault(find(identity), []).append(identity)
    return tuple(sorted(tuple(sorted(group)) for group in groups.values() if len(group) > 1))
