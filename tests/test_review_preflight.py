import json

import pytest
from test_corpus import db as db
from test_dataset import example as example
from test_dataset import review_fixture

from finreg.dataset import record_review, register_draft
from finreg.review_preflight import preflight_reviews

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({}, "reviewed"),
        ({"official_cross_checks": []}, "needs_review"),
        ({"meaning": {"result": "uncertain", "reason": "synthetic"}}, "needs_review"),
        ({"reviewer_role": "external", "official_cross_checks": []}, "reviewed"),
        ({"excluded_reason": "synthetic exclusion"}, "excluded"),
    ],
)
def test_prediction_matches_database_without_writing(db, example, overrides, expected):
    register_draft(db, example)
    item = review_fixture(example, **overrides)
    report = preflight_reviews(db, item.model_dump(mode="json"))
    assert report["valid"]
    assert report["records"][0]["predicted_status"] == expected
    assert db.execute("SELECT count(*) FROM review_log").fetchone()[0] == 0
    assert db.execute("SELECT review_status FROM current_example").fetchone()[0] == "draft"
    assert record_review(db, item) == expected


def test_reports_all_errors_without_exposing_submitted_text(db, example):
    register_draft(db, example)
    item = review_fixture(example).model_dump(mode="json")
    bad = dict(item, reviewer="private-reviewer", human_attested=False)
    report = preflight_reviews(db, [item, item, bad])
    assert not report["valid"]
    assert [row["errors"] for row in report["records"]] == [
        ["duplicate_example"],
        ["duplicate_example"],
        ["invalid_review_record"],
    ]
    assert "private-reviewer" not in json.dumps(report)
    assert "synthetic-test-reviewer" not in json.dumps(report)
    assert db.execute("SELECT count(*) FROM review_log").fetchone()[0] == 0


def test_stale_and_missing_evidence(db, example):
    register_draft(db, example)
    item = review_fixture(example).model_dump(mode="json")
    report = preflight_reviews(db, dict(item, provision_ids=[]))
    assert report["records"][0]["errors"] == ["evidence_ids_mismatch"]
    register_draft(db, example.model_copy(update={"question": "changed fictional question"}))
    assert preflight_reviews(db, item)["records"][0]["errors"] == ["stale_review"]


def test_test_split_is_rejected_before_reading_payload(db, example):
    heldout = example.model_copy(update={"split": "test"})
    register_draft(db, heldout)
    report = preflight_reviews(db, review_fixture(heldout).model_dump(mode="json"))
    assert report["records"][0]["errors"] == ["not_development_or_training"]
    assert db.execute("SELECT count(*) FROM review_log").fetchone()[0] == 0


@pytest.mark.parametrize(
    "entries", [[], None, "private content", {"kind": "finreg-review-progress"}]
)
def test_empty_wrong_format_and_progress_files_rejected(db, entries):
    assert not preflight_reviews(db, entries)["valid"]


def test_changed_source_blocks_existence_pass(db, example):
    register_draft(db, example)
    db.execute("UPDATE provision SET text='changed fictional source'")
    report = preflight_reviews(db, review_fixture(example).model_dump(mode="json"))
    assert report["records"][0]["errors"] == ["evidence_source_mismatch"]
