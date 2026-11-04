"""Validate a versioned Jiangsu hospital corpus without printing record bodies."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from careguide.hospital_corpus.store import HospitalCorpus


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-dir", required=True)
    parser.add_argument("--today", required=True, type=date.fromisoformat)
    args = parser.parse_args(argv)
    try:
        corpus = HospitalCorpus.load(args.corpus_dir)
        report = corpus.validate(today=args.today)
    except (OSError, ValueError, json.JSONDecodeError):
        print(json.dumps({"status": "invalid", "error_type": "corpus_load_failed"}, sort_keys=True))
        return 1
    payload = {
        "status": "valid" if report.valid else "invalid",
        "hospital_count": len(corpus.hospitals),
        "source_count": len(corpus.sources),
        "record_count": len(corpus.records),
        "errors": list(report.errors),
        "warnings": list(report.warnings),
        "expired_source_ids": list(report.expired_source_ids),
        "conflict_ids": list(report.conflict_ids),
        "duplicate_groups": [list(group) for group in report.duplicate_groups],
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if report.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
