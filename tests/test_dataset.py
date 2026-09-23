from datetime import UTC, datetime

import psycopg
import pytest
from pydantic import ValidationError
from test_corpus import db as db
from test_corpus import source_fixture

from finreg.corpus import create_snapshot, ingest_source
from finreg.dataset import (
    CHECKS,
    Example,
    HumanReview,
    content_hash,
    record_review,
    register_draft,
    reviewed_examples,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def example(db, tmp_path):
    version = ingest_source(db, tmp_path, source_fixture(tmp_path))
    snapshot = create_snapshot(db, [version])
    provision_id, text = db.execute(
        "SELECT provision_id,text FROM provision WHERE provision_kind='article'"
    ).fetchone()
    return Example(
        example_id="fictional-01",
        dataset_id="fixture",
        group_id="fixture-group",
        split="dev",
        snapshot_id=snapshot,
        question="가상 규정의 수수료는?",
        as_of_date="2000-01-02",
        facts={"synthetic": True},
        category="single",
        evidence=[{"provision_id": provision_id, "quote": text}],
        expected_answer="가상 규정 원문 참조",
        expected_status="answered",
        limitations=[],
    )


def review_fixture(example, revision=1, **overrides):
    values = {
        "example_id": example.example_id,
        "revision": revision,
        "content_hash": content_hash(example),
        "provision_ids": [item.provision_id for item in example.evidence],
        "reviewer": "synthetic-test-reviewer",
        "reviewer_role": "developer",
        "reviewer_role_detail": "fictional review fixture",
        "review_date": datetime.now(UTC),
        "method": "synthetic source comparison; not a real review",
        "human_attested": True,
        **{key: {"result": "pass", "reason": "synthetic fixture only"} for key in CHECKS},
        "official_cross_checks": [
            {
                "source_url": "https://www.fsc.go.kr/fixture-only",
                "locator": "test",
                "note": "fictional URL, never fetched",
            }
        ],
        "duration_seconds": 10,
    }
    values.update(overrides)
    return HumanReview.model_validate(values)


def test_drafts_do_not_export_and_changed_content_requires_new_review(db, example):
    assert register_draft(db, example) == 1
    assert register_draft(db, example) == 1
    assert reviewed_examples(db, "fixture", "dev") == []
    assert record_review(db, review_fixture(example)) == "reviewed"
    assert reviewed_examples(db, "fixture", "dev")[0]["payload"]["review_status"] == "reviewed"
    revised = example.model_copy(update={"question": "가상 질문 수정"})
    assert register_draft(db, revised) == 2
    assert reviewed_examples(db, "fixture", "dev") == []
    with pytest.raises(ValueError, match="Stale review"):
        record_review(db, review_fixture(example))
    assert db.execute("SELECT review_status FROM current_example").fetchone()[0] == "draft"
    assert db.execute("SELECT count(*) FROM review_log").fetchone()[0] == 1


def test_missing_cross_check_and_uncertain_meaning_never_pass(db, example):
    register_draft(db, example)
    assert record_review(db, review_fixture(example, official_cross_checks=[])) == "needs_review"
    assert (
        record_review(
            db,
            review_fixture(example, meaning={"result": "uncertain", "reason": "Unclear support"}),
        )
        == "needs_review"
    )
    assert (
        record_review(db, review_fixture(example, excluded_reason="Cannot establish the answer"))
        == "excluded"
    )
    assert reviewed_examples(db, "fixture", "dev") == []


def test_external_review_is_separate_and_requires_all_checks(db, example):
    register_draft(db, example)
    assert (
        record_review(
            db, review_fixture(example, reviewer_role="external", official_cross_checks=[])
        )
        == "reviewed"
    )
    assert reviewed_examples(db, "fixture", "dev")[0]["external_review"] is True
    assert (
        record_review(
            db,
            review_fixture(
                example,
                reviewer_role="external",
                official_cross_checks=[],
                temporal={"result": "fail", "reason": "Wrong period"},
            ),
        )
        == "needs_review"
    )


def test_evidence_mismatch_and_partial_review_are_rejected(db, example):
    wrong = example.model_dump(mode="json")
    wrong["evidence"][0]["quote"] = "not in source"
    with pytest.raises(ValueError, match="mismatch"):
        register_draft(db, Example.model_validate(wrong))
    register_draft(db, example)
    with pytest.raises(ValueError, match="every evidence"):
        record_review(db, review_fixture(example, provision_ids=[]))


def test_split_leakage_and_test_export_are_rejected(db, example):
    register_draft(db, example)
    with pytest.raises(ValueError, match="cross dataset splits"):
        register_draft(db, example.model_copy(update={"example_id": "related", "split": "test"}))
    with pytest.raises(ValueError, match="Test data"):
        reviewed_examples(db, "fixture", "test")


def test_history_cannot_be_overwritten(db, example):
    register_draft(db, example)
    record_review(db, review_fixture(example))
    with pytest.raises(psycopg.errors.RaiseException, match="Append-only"), db.transaction():
        db.execute("DELETE FROM review_log")
    with pytest.raises(psycopg.errors.RaiseException, match="Append-only"), db.transaction():
        db.execute("UPDATE example_revision SET payload='{}'")


def test_unfilled_or_automated_review_cannot_be_imported(example):
    with pytest.raises(ValidationError):
        review_fixture(example, human_attested=False)
    with pytest.raises(ValidationError):
        review_fixture(example, reviewer=" ")
    with pytest.raises(ValidationError):
        review_fixture(example, review_date=datetime(2000, 1, 1))
