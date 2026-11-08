"""Build the reviewed Jiangsu 15-hospital v2 corpus entirely offline."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from careguide.hospital_corpus.review import apply_review_manifest
from careguide.hospital_corpus.store import DATA_FILES, HospitalCorpus, write_corpus


EXPECTED_HOSPITALS = 15
EXPECTED_RECORDS = 45
EXPECTED_NEW_RECORDS = 36


def _expanded_hospitals(hospitals, reviewed_sources):
    """Extend only the v2 registry with domains from approved hospital sources."""
    approved_hosts: dict[str, set[str]] = {}
    for source in reviewed_sources:
        if source.get("source_tier") != "hospital_official":
            continue
        host = (urlparse(source.get("url", "")).hostname or "").lower().rstrip(".")
        if host:
            approved_hosts.setdefault(source["hospital_id"], set()).add(host)

    expanded = []
    for hospital in hospitals:
        additions = approved_hosts.get(hospital.hospital_id, set())
        domains = tuple(dict.fromkeys((*hospital.official_domains, *sorted(additions))))
        expanded.append(hospital.model_copy(update={"official_domains": domains}))
    return tuple(expanded)


def _read_list(path: Path, label: str) -> list[dict]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{label} must be a JSON list of objects")
    return value


def _v1_hashes(root: Path) -> dict[str, str]:
    paths = [root / "manifest.json", *(root / filename for filename in DATA_FILES)]
    snapshot_root = root / "snapshots"
    paths.extend(path for path in snapshot_root.rglob("*") if path.is_file())
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
    }


def _verify_v1_integrity(v1_corpus: Path, source_dir: Path) -> None:
    integrity = json.loads((source_dir / "v1_integrity.json").read_text(encoding="utf-8"))
    expected = integrity.get("files")
    if integrity.get("version") != "jiangsu-v1-integrity-v1" or not isinstance(expected, dict):
        raise ValueError("invalid v1 integrity manifest")
    actual = _v1_hashes(v1_corpus)
    if expected != actual:
        raise ValueError("v1 integrity mismatch")


def _validate_plan(v1: HospitalCorpus, new_sources: list[dict], candidates: list[dict]) -> None:
    if len(v1.hospitals) != EXPECTED_HOSPITALS:
        raise ValueError("v1 hospital count does not match expansion contract")
    if len(candidates) != EXPECTED_NEW_RECORDS:
        raise ValueError("v2 candidate record count must be 36")
    source_ids = [source.get("source_id") for source in new_sources]
    if None in source_ids or len(source_ids) != len(set(source_ids)):
        raise ValueError("new source identities must be present and unique")
    if set(source_ids) & {source.source_id for source in v1.sources}:
        raise ValueError("new source identity overlaps v1")
    document_ids = [record.get("document_id") for record in candidates]
    if None in document_ids or len(document_ids) != len(set(document_ids)):
        raise ValueError("candidate record identities must be present and unique")
    if set(document_ids) & {record.document_id for record in v1.records}:
        raise ValueError("candidate record identity overlaps v1")

    candidate_counts = Counter(record.get("hospital_id") for record in candidates)
    expected_expansion_ids = {
        hospital.hospital_id for hospital in v1.hospitals if hospital.city != "泰州"
    }
    if set(candidate_counts) != expected_expansion_ids or any(
        count != 3 for count in candidate_counts.values()
    ):
        raise ValueError("every non-Taizhou hospital must have exactly three candidates")
    final_counts = Counter(record.hospital_id for record in v1.records)
    final_counts.update(record["hospital_id"] for record in candidates)
    if set(final_counts) != {hospital.hospital_id for hospital in v1.hospitals} or any(
        count != 3 for count in final_counts.values()
    ):
        raise ValueError("every v2 hospital must have exactly three records")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--v1-corpus", type=Path, default=Path("data/jiangsu_hospitals_v1")
    )
    parser.add_argument(
        "--source-dir", type=Path, default=Path("data/jiangsu_hospital_sources_v2")
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("data/jiangsu_hospitals_v2")
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    if args.apply and args.output_dir.exists():
        raise FileExistsError(f"Refusing to overwrite corpus: {args.output_dir}")
    _verify_v1_integrity(args.v1_corpus, args.source_dir)
    v1 = HospitalCorpus.load(args.v1_corpus)
    new_sources = _read_list(args.source_dir / "sources.json", "sources")
    candidates = _read_list(
        args.source_dir / "record_candidates.json", "record candidates"
    )
    _validate_plan(v1, new_sources, candidates)

    summary = {
        "status": "dry_run" if not args.apply else "built",
        "hospital_count": len(v1.hospitals),
        "source_count": len(v1.sources) + len(new_sources),
        "record_count": len(v1.records) + len(candidates),
        "external_calls": 0,
    }
    if not args.apply:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0

    reviewed = apply_review_manifest(
        args.source_dir,
        new_sources,
        candidates,
        trusted_sources=[source.model_dump(mode="json") for source in v1.sources],
        trusted_source_dir=args.v1_corpus,
    )
    merged_sources = (*v1.sources, *reviewed.sources)
    merged_records = (*v1.records, *reviewed.records)
    merged_verification = (*v1.verification, *reviewed.verification)
    expanded_hospitals = _expanded_hospitals(v1.hospitals, reviewed.sources)
    reviewed_times = [entry.reviewed_at for entry in v1.verification]
    reviewed_times.extend(entry["reviewed_at"] for entry in reviewed.verification)
    built_at = max(reviewed_times)
    corpus = write_corpus(
        args.output_dir,
        hospitals=expanded_hospitals,
        sources=merged_sources,
        records=merged_records,
        verification=merged_verification,
        built_at=built_at,
        snapshot_roots=(args.v1_corpus / "snapshots", args.source_dir / "snapshots"),
        version="jiangsu-hospitals-v2",
        stage="expanded_core_15_of_15",
    )
    if len(corpus.records) != EXPECTED_RECORDS:
        raise RuntimeError("published v2 record count changed after validation")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
