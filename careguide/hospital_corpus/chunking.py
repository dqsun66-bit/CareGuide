"""Heading-aware, scope-preserving chunking without model calls."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from .normalize import normalize_text


@dataclass(frozen=True)
class Section:
    scope_id: str
    heading: str
    content: str


@dataclass(frozen=True)
class Chunk:
    scope_id: str
    heading: str
    content: str
    ordinal: int


LIST_ITEM = re.compile(r"^(?:\d+[.、]|[一二三四五六七八九十]+[、.]|[-•])\s*")
SENTENCE = re.compile(r"[^。！？；\n]+[。！？；]?|[^。！？；\n]+$")


def _coerce_section(value: Section | tuple[str, str]) -> Section:
    if isinstance(value, Section):
        return value
    heading, content = value
    return Section("", heading, content)


def _semantic_blocks(content: str) -> list[str]:
    lines = normalize_text(content).split("\n")
    blocks: list[str] = []
    list_buffer: list[str] = []
    for line in lines:
        if not line:
            if list_buffer:
                blocks.append("\n".join(list_buffer))
                list_buffer = []
            continue
        if LIST_ITEM.match(line):
            list_buffer.append(line)
            continue
        if list_buffer:
            blocks.append("\n".join(list_buffer))
            list_buffer = []
        blocks.append(line)
    if list_buffer:
        blocks.append("\n".join(list_buffer))
    return blocks


def _split_long_block(block: str, target_max: int, overlap: int) -> list[str]:
    if len(block) <= target_max:
        return [block]
    sentences = [item.strip() for item in SENTENCE.findall(block) if item.strip()]
    if not sentences:
        sentences = [block]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        remaining = sentence
        while remaining:
            available = target_max - len(current)
            if available <= 0:
                chunks.append(current)
                current = current[-overlap:] if overlap else ""
                available = target_max - len(current)
            take = remaining[:available]
            current += take
            remaining = remaining[len(take):]
            if remaining:
                chunks.append(current)
                current = current[-overlap:] if overlap else ""
        if len(current) >= target_max:
            chunks.append(current)
            current = current[-overlap:] if overlap else ""
    if current and (not chunks or current != chunks[-1]):
        chunks.append(current)
    return chunks


def chunk_sections(
    values: Iterable[Section | tuple[str, str]],
    *,
    target_min: int = 250,
    target_max: int = 600,
    overlap: int = 60,
    expected_scope: str | None = None,
) -> tuple[Chunk, ...]:
    if target_min <= 0 or target_max < target_min:
        raise ValueError("Invalid chunk size bounds")
    if overlap < 0 or overlap >= target_max:
        raise ValueError("Overlap must be smaller than target_max")
    output: list[Chunk] = []
    for raw in values:
        section = _coerce_section(raw)
        if expected_scope is not None and section.scope_id != expected_scope:
            raise ValueError("Section scope does not match expected scope")
        heading = normalize_text(section.heading)
        blocks = _semantic_blocks(section.content)
        section_chunks: list[str] = []
        current = ""
        for block in blocks:
            if len(block) > target_max:
                if current:
                    section_chunks.append(current)
                    current = ""
                section_chunks.extend(_split_long_block(block, target_max, overlap))
                continue
            separator = "\n" if current else ""
            if current and len(current) + len(separator) + len(block) > target_max:
                section_chunks.append(current)
                prefix = current[-overlap:] if overlap else ""
                current = f"{prefix}\n{block}" if prefix else block
                if len(current) > target_max:
                    section_chunks.extend(_split_long_block(current, target_max, overlap)[:-1])
                    current = _split_long_block(current, target_max, overlap)[-1]
            else:
                current = f"{current}{separator}{block}"
        if current:
            section_chunks.append(current)
        for content in section_chunks:
            if content:
                output.append(Chunk(section.scope_id, heading, content, len(output)))
    return tuple(output)
