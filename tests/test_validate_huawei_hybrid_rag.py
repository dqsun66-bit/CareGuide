from pathlib import Path

import pytest

from scripts.validate_huawei_hybrid_rag import main, run_validation


CORPUS = Path(__file__).resolve().parents[1] / "data" / "medical_v1"


def test_validator_dry_run_makes_no_provider_calls(monkeypatch):
    monkeypatch.setattr(
        "careguide.runtime.semantic_embedding",
        lambda: (_ for _ in ()).throw(AssertionError("provider called")),
    )

    result = run_validation(config_file=None, apply=False, corpus_dir=CORPUS)

    assert result.status == "dry_run"
    assert result.external_calls == 0


def test_validator_output_never_contains_key(monkeypatch, capsys):
    monkeypatch.setenv("CAREGUIDE_EMBEDDING_API_KEY", "secret-test-value")

    assert main(["--dry-run", "--corpus-dir", str(CORPUS)]) == 0

    assert "secret-test-value" not in capsys.readouterr().out


def test_real_validation_requires_explicit_config_file():
    with pytest.raises(ValueError, match="config-file"):
        run_validation(config_file=None, apply=True, corpus_dir=CORPUS)


def test_dry_run_reports_fixed_safety_cases_without_building_index():
    result = run_validation(config_file=None, apply=False, corpus_dir=CORPUS)

    assert result.cases == 4
    assert result.corpus_records > 0
    assert result.collection.startswith("careguide_medical_v1_")
