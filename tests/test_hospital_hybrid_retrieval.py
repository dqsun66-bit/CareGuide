from pathlib import Path

from careguide.hospital_corpus.retrieval import (
    corpus_fingerprint,
    hospital_collection_name,
    hospital_evidence,
    hospital_retrieval_text,
)
from careguide.hospital_corpus.store import HospitalCorpus


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "jiangsu_hospitals_v2"


def test_projection_contains_scope_and_official_source():
    corpus = HospitalCorpus.load(CORPUS)
    record = corpus.records[0]
    text = hospital_retrieval_text(corpus, record)
    evidence = hospital_evidence(corpus, record)
    hospital = next(
        item for item in corpus.hospitals
        if item.hospital_id == record.hospital_id
    )
    campus = next(
        item for item in hospital.campuses
        if item.campus_id == record.campus_id
    )

    assert hospital.official_name in text
    assert campus.official_name in text
    assert record.content in text
    assert evidence["scope"] == {
        "hospital_id": hospital.hospital_id,
        "hospital": hospital.official_name,
        "city": hospital.city,
        "campus_id": campus.campus_id,
        "campus": campus.official_name,
        "category": record.category.value,
    }
    assert evidence["source"]["url"] == record.source_url
    assert evidence["source"]["review_status"] == "verified_official"


def test_collection_name_is_manifest_bound():
    corpus = HospitalCorpus.load(CORPUS)

    assert len(corpus_fingerprint(corpus)) == 64
    name = hospital_collection_name(corpus)
    assert name.startswith("careguide_jiangsu_hospitals_v2_")
    assert len(name.rsplit("_", 1)[1]) == 12
