"""Preview or fetch one allowlisted hospital source from a reviewed registry."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from careguide.hospital_corpus.models import Source
from careguide.hospital_corpus.normalize import canonical_url
from careguide.hospital_corpus.snapshot import SnapshotFetchError, default_snapshot_limit, fetch_snapshot


def _load_source(registry: Path, source_id: str) -> Source:
    payload = json.loads(registry.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("source registry must be a list")
    matches = [Source.model_validate(item) for item in payload if isinstance(item, dict) and item.get("source_id") == source_id]
    if len(matches) != 1:
        raise ValueError("source id must match exactly one reviewed registry entry")
    return matches[0]


def _target_path(output_dir: Path, source: Source) -> Path:
    relative = PurePosixPath(source.snapshot_path)
    if not relative.parts or relative.parts[0] != "snapshots":
        raise ValueError("snapshot path must be rooted under snapshots")
    target = output_dir.joinpath(*relative.parts[1:])
    _assert_safe_output_chain(output_dir, target.parent, create=True)
    root = output_dir.resolve(strict=True)
    try:
        target.resolve().relative_to(root)
    except ValueError as exc:
        raise ValueError("snapshot target escapes output directory") from exc
    return target


def _is_reparse_point(path: Path) -> bool:
    metadata = os.lstat(path)
    attributes = getattr(metadata, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    return path.is_symlink() or bool(attributes & marker) or bool(getattr(metadata, "st_reparse_tag", 0))


def _assert_safe_output_chain(output_dir: Path, parent: Path, *, create: bool) -> None:
    if create:
        output_dir.mkdir(parents=True, exist_ok=True)
    if _is_reparse_point(output_dir):
        raise ValueError("snapshot output directory cannot be a link or junction")
    root = output_dir.resolve(strict=True)
    try:
        relative = parent.relative_to(output_dir)
    except ValueError as exc:
        raise ValueError("snapshot target escapes output directory") from exc
    current = output_dir
    for part in relative.parts:
        current /= part
        if create:
            current.mkdir(exist_ok=True)
        if _is_reparse_point(current):
            raise ValueError("snapshot output path cannot contain a link or junction")
        try:
            current.resolve(strict=True).relative_to(root)
        except ValueError as exc:
            raise ValueError("snapshot target escapes output directory") from exc


def _atomic_write(path: Path, body: bytes, *, output_root: Path | None = None) -> None:
    if output_root is not None:
        _assert_safe_output_chain(output_root, path.parent, create=False)
    elif _is_reparse_point(path.parent):
        raise ValueError("snapshot output parent cannot be a link or junction")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".part", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        source = _load_source(args.registry, args.source_id)
        safe_url = canonical_url(source.url)
        relative = PurePosixPath(source.snapshot_path)
        target = args.output_dir.joinpath(*relative.parts[1:])
        if not args.apply:
            print(json.dumps({
                "status": "dry_run",
                "source_id": source.source_id,
                "canonical_url": safe_url,
                "target": target.as_posix(),
                "external_calls": 0,
            }, ensure_ascii=False, sort_keys=True))
            return 0
        target = _target_path(args.output_dir, source)
        metadata_path = target.with_suffix(target.suffix + ".metadata.json")
        if target.exists():
            raise FileExistsError("snapshot target already exists")
        if metadata_path.exists():
            raise FileExistsError("snapshot metadata already exists")
        host = urlparse(safe_url).hostname
        if host is None:
            raise ValueError("reviewed source is missing a host")
        result = fetch_snapshot(
            safe_url,
            allowed_hosts={host},
            max_bytes=default_snapshot_limit(safe_url),
        )
        body_written = False
        try:
            _atomic_write(target, result.body, output_root=args.output_dir)
            body_written = True
            _atomic_write(
                metadata_path,
                (json.dumps(result.metadata(), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                output_root=args.output_dir,
            )
        except Exception:
            if body_written:
                target.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            raise
        print(json.dumps({
            "status": "fetched",
            "source_id": source.source_id,
            "canonical_url": result.canonical_url,
            "target": target.as_posix(),
            "sha256": result.sha256,
            "external_calls": 1,
        }, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, SnapshotFetchError):
        print(json.dumps({"status": "error", "error_type": "snapshot_fetch_failed"}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
