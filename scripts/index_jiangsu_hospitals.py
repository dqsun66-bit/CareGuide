"""Safely validate or explicitly index the verified Jiangsu hospital corpus."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from careguide.hospital_corpus.retrieval import (
    corpus_fingerprint,
    hospital_collection_name,
    index_hospital_collection,
)
from careguide.hospital_corpus.store import HospitalCorpus
from careguide.runtime import chroma_client, semantic_embedding
from scripts.private_config import load_config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--config-file", type=Path)
    parser.add_argument("--corpus-dir", required=True, type=Path)
    parser.add_argument("--chroma-dir", type=Path)
    parser.add_argument("--today", required=True, type=date.fromisoformat)
    args = parser.parse_args(argv)

    corpus = HospitalCorpus.load(args.corpus_dir)
    report = corpus.validate(today=args.today)
    if not report.valid:
        print(json.dumps({
            "status": "invalid",
            "errors": list(report.errors),
            "external_calls": 0,
        }, ensure_ascii=False, sort_keys=True))
        return 1

    preview = {
        "status": "valid",
        "hospital_count": len(corpus.hospitals),
        "record_count": len(corpus.records),
        "collection": hospital_collection_name(corpus),
        "fingerprint": corpus_fingerprint(corpus),
        "external_calls": 0,
    }
    if not args.apply:
        print(json.dumps(preview, ensure_ascii=False, sort_keys=True))
        return 0

    if args.config_file is None:
        parser.error("--config-file is required with --apply")
    load_config(args.config_file)
    embedding = semantic_embedding()
    try:
        client = chroma_client()
        if args.chroma_dir is not None:
            import chromadb
            client = chromadb.PersistentClient(
                path=str(args.chroma_dir),
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
        collection = index_hospital_collection(
            client, corpus, embedding, today=args.today)
        payload = {
            "status": "indexed",
            "collection": collection.name,
            "count": collection.count(),
            "provider": embedding.provider_name,
            "model": embedding.model,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    finally:
        embedding.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
