"""Evaluate the reviewed Jiangsu hospital pilot with deterministic BM25 retrieval."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from careguide.hospital_corpus.evaluation import evaluate_cases, load_cases
from careguide.hospital_corpus.store import HospitalCorpus


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-dir", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args(argv)

    corpus = HospitalCorpus.load(args.corpus_dir)
    cases = load_cases(args.cases)
    report = evaluate_cases(corpus, cases, top_k=args.top_k)
    payload = {
        "corpus_version": corpus.manifest.version,
        "corpus_stage": corpus.manifest.stage,
        "corpus_manifest_sha256": hashlib.sha256(
            (args.corpus_dir / "manifest.json").read_bytes()
        ).hexdigest(),
        "case_file_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "case_count": len(cases),
        "top_k": args.top_k,
        **asdict(report),
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
