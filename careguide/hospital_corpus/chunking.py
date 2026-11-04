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


@dataclass(frozen=True)
class _Block:
    content: str
    is_list: bool


LIST_ITEM = re.compile(r"^(?:\d+[.、]|[一二三四五六七八九十]+[、.]|[-•])\s*")


def _coerce_section(value: Section | tuple[str, str]) -> Section:
    if isinstance(value, Section):
        return value
    heading, content = value
    return Section("", heading, content)


def _semantic_blocks(content: str) -> list[_Block]:
    lines = normalize_text(content).split("\n")
    blocks: list[_Block] = []
    list_buffer: list[str] = []
    for line in lines:
        if not line:
            if list_buffer:
                blocks.append(_Block("\n".join(list_buffer), True))
                list_buffer = []
            continue
        if LIST_ITEM.match(line):
            list_buffer.append(line)
            continue
        if list_buffer:
            blocks.append(_Block("\n".join(list_buffer), True))
            list_buffer = []
        blocks.append(_Block(line, False))
    if list_buffer:
        blocks.append(_Block("\n".join(list_buffer), True))
    return blocks


def _split_long_block(block: str, target_max: int, overlap: int) -> list[str]:
    if len(block) <= target_max:
        return [block]
    chunks: list[str] = []
    start = 0
    while start < len(block):
        hard_end = min(start + target_max, len(block))
        end = hard_end
        if hard_end < len(block):
            minimum_end = start + max(1, target_max // 2)
            boundaries = [block.rfind(mark, minimum_end, hard_end) for mark in "。！？；\n"]
            boundary = max(boundaries)
            if boundary >= minimum_end:
                end = boundary + 1
        chunks.append(block[start:end])
        if end == len(block):
            break
        next_start = end - overlap if overlap else end
        start = max(start + 1, next_start)
    return chunks


def _split_list_block(block: str, target_max: int) -> list[str]:
    """Split a list only between complete items; an oversized item stays intact."""
    chunks: list[str] = []
    current = ""
    for item in block.split("\n"):
        candidate = f"{current}\n{item}" if current else item
        if current and len(candidate) > target_max:
            chunks.append(current)
            current = item
        else:
            current = candidate
        if len(current) > target_max:
            chunks.append(current)
            current = ""
    if current:
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
    sections = tuple(_coerce_section(raw) for raw in values)
    scoped = {section.scope_id for section in sections if section.scope_id}
    if len(scoped) > 1:
        raise ValueError("Input batch contains mixed scopes")
    output: list[Chunk] = []
    for section in sections:
        if expected_scope is not None and section.scope_id != expected_scope:
            raise ValueError("Section scope does not match expected scope")
        heading = normalize_text(section.heading)
        blocks = _semantic_blocks(section.content)
        section_chunks: list[str] = []
        current = ""
        for block in blocks:
            if len(block.content) > target_max:
                if current:
                    section_chunks.append(current)
                    current = ""
                if block.is_list:
                    section_chunks.extend(_split_list_block(block.content, target_max))
                else:
                    section_chunks.extend(_split_long_block(block.content, target_max, overlap))
                continue
            separator = "\n" if current else ""
            if current and len(current) + len(separator) + len(block.content) > target_max:
                section_chunks.append(current)
                prefix = "" if block.is_list else (current[-overlap:] if overlap else "")
                current = f"{prefix}\n{block.content}" if prefix else block.content
                if len(current) > target_max:
                    split = _split_long_block(current, target_max, overlap)
                    section_chunks.extend(split[:-1])
                    current = split[-1]
            else:
                current = f"{current}{separator}{block.content}"
        if current:
            section_chunks.append(current)
        for content in section_chunks:
            if content:
                output.append(Chunk(section.scope_id, heading, content, len(output)))
    return tuple(output)
