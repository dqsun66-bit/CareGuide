"""Independent approvals for immutable hospital source and record bundles."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

from careguide.hospital_corpus.review import apply_review_manifest


CN_TZ = timezone(timedelta(hours=8))
COLLECTED = datetime(2026, 11, 6, 9, 0, tzinfo=CN_TZ)
REVIEWED = datetime(2026, 11, 6, 15, 0, tzinfo=CN_TZ)


@dataclass
class ReviewBundle:
    root: Path
    sources: list[dict]
    records: list[dict]
    manifest: dict

    def save(self) -> None:
        (self.root / "review_manifest.json").write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


@pytest.fixture
def review_bundle(tmp_path) -> ReviewBundle:
    snapshots = {
        "primary": "<html><body>医院主院区提供官方联系信息。</body></html>".encode(),
        "supporting": "<html><body>主管部门确认医院为三级甲等。</body></html>".encode(),
    }
    sources = []
    source_reviews = []
    for source_id, raw in snapshots.items():
        path = tmp_path / f"snapshots/js_nanjing_001/{source_id}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        sources.append({
            "source_id": source_id,
            "snapshot_path": f"snapshots/js_nanjing_001/{source_id}.html",
            "retrieved_sha256": digest,
            "collected_at": COLLECTED.isoformat(),
            "reviewed_at": None,
            "review_status": "needs_review",
        })
        source_reviews.append({
            "source_id": source_id,
            "retrieved_sha256": digest,
            "reviewer": "独立资料复核员",
            "reviewed_at": REVIEWED.isoformat(),
            "decision": "approved",
            "note": "冻结快照与来源元数据一致。",
        })
    content_hash = hashlib.sha256("reviewed record".encode()).hexdigest()
    records = [{
        "document_id": "record-1",
        "source_id": "primary",
        "supporting_source_ids": ["supporting"],
        "content_hash": content_hash,
        "reviewed_at": None,
        "review_status": "needs_review",
    }]
    manifest = {
        "version": "jiangsu-hospital-review-v1",
        "source_reviews": source_reviews,
        "record_reviews": [{
            "document_id": "record-1",
            "source_ids": ["primary", "supporting"],
            "content_hash": content_hash,
            "required_terms": ["主院区", "三级甲等"],
            "reviewer": "独立资料复核员",
            "reviewed_at": REVIEWED.isoformat(),
            "decision": "approved",
        }],
        "gap_reviews": [],
    }
    bundle = ReviewBundle(tmp_path, sources, records, manifest)
    bundle.save()
    return bundle


def test_review_returns_copies_and_upgrades_approved_items(review_bundle):
    original_sources = deepcopy(review_bundle.sources)
    original_records = deepcopy(review_bundle.records)

    result = apply_review_manifest(
        review_bundle.root,
        review_bundle.sources,
        review_bundle.records,
    )

    assert review_bundle.sources == original_sources
    assert review_bundle.records == original_records
    assert all(item["review_status"] == "verified_official" for item in result.sources)
    assert result.records[0]["review_status"] == "verified_official"
    assert {entry["verification_id"] for entry in result.verification} == {
        "verify_primary",
        "verify_supporting",
        "verify_record-1",
    }


@pytest.mark.parametrize(
    "mutation, message",
    [
        (
            lambda bundle: bundle.manifest["source_reviews"][0].update(
                retrieved_sha256="0" * 64
            ),
            "invalid or stale source review",
        ),
        (
            lambda bundle: bundle.manifest["source_reviews"][0].update(decision="rejected"),
            "invalid or stale source review",
        ),
        (
            lambda bundle: bundle.manifest["source_reviews"][0].update(reviewer="  "),
            "invalid or stale source review",
        ),
        (
            lambda bundle: bundle.manifest["source_reviews"][0].update(
                reviewed_at=(COLLECTED - timedelta(seconds=1)).isoformat()
            ),
            "invalid or stale source review",
        ),
        (
            lambda bundle: bundle.manifest["record_reviews"][0].update(
                content_hash="0" * 64
            ),
            "invalid or stale record review",
        ),
        (
            lambda bundle: bundle.manifest["record_reviews"][0].update(
                required_terms=["主院区", "快照中不存在的词"]
            ),
            "lacks reviewed evidence terms",
        ),
    ],
)
def test_review_rejects_stale_or_unsupported_approval(review_bundle, mutation, message):
    mutation(review_bundle)
    review_bundle.save()
    with pytest.raises(ValueError, match=message):
        apply_review_manifest(
            review_bundle.root,
            review_bundle.sources,
            review_bundle.records,
        )


def test_supporting_source_requires_its_own_approval(review_bundle):
    review_bundle.manifest["source_reviews"] = [review_bundle.manifest["source_reviews"][0]]
    review_bundle.save()
    with pytest.raises(ValueError, match="source review approvals do not match"):
        apply_review_manifest(
            review_bundle.root,
            review_bundle.sources,
            review_bundle.records,
        )


def test_record_review_must_bind_all_record_sources(review_bundle):
    review_bundle.manifest["record_reviews"][0]["source_ids"] = ["primary"]
    review_bundle.save()
    with pytest.raises(ValueError, match="record review source set mismatch"):
        apply_review_manifest(
            review_bundle.root,
            review_bundle.sources,
            review_bundle.records,
        )
