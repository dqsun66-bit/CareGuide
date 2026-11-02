"""Build a versioned medical index only after explicit Huawei configuration."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from careguide.retrieval.knowledge_base import COLLECTION_NAME, index_collection
from careguide.runtime import chroma_client, load_runtime_corpus, semantic_embedding


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--versioned', action='store_true')
    parser.add_argument('--config-file')
    parser.add_argument('--corpus-dir', help='Explicit corpus directory; defaults to the frozen medical_v1 corpus')
    args = parser.parse_args(argv)
    if args.apply and not args.config_file:
        raise ValueError('--config-file is required with --apply')
    if args.apply and not args.versioned:
        raise ValueError('--versioned is required with --apply')
    from scripts.private_config import load_config
    load_config(args.config_file)
    corpus = load_runtime_corpus(args.corpus_dir)
    from careguide.retrieval.knowledge_base import versioned_collection_name
    target = versioned_collection_name(corpus) if args.versioned else COLLECTION_NAME
    summary = {'status': 'dry_run' if not args.apply else 'ready',
               'records': len(corpus.records), 'collection': target,
               'corpus_fingerprint': corpus.fingerprint[:12]}
    if not args.apply:
        print(json.dumps({**summary, 'external_calls': 0}, sort_keys=True))
        return 0
    embedding = semantic_embedding()
    summary.update(provider=embedding.provider_name, model=embedding.model)
    print(json.dumps(summary, sort_keys=True))
    collection = index_collection(chroma_client(), corpus, embedding, versioned=args.versioned)
    print(json.dumps({'status': 'indexed', 'records': collection.count(),
                      'collection': collection.name}, sort_keys=True))
    return 0


if __name__ == '__main__':
    main()
