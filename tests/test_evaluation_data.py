import pytest
from test_corpus import db as db
from test_dataset import example as example

from finreg.dataset import content_hash, register_draft
from finreg.evaluation_data import audit_entries, normalized_question, refresh_audit


def row(identifier, split, **changes):
    return {
        "example_id": identifier,
        "split": split,
        "group_id": identifier,
        "case_family": identifier,
        "template_key": identifier,
        "issue_keys": [identifier],
        "category": "single",
        "question": identifier,
        "evidence_ids": [],
        "review_status": "draft",
        **changes,
    }


def test_renamed_group_cannot_hide_same_case_or_template():
    report = audit_entries(
        [
            row("dev", "dev", case_family="shared", template_key="same"),
            row("test", "test", case_family="shared", template_key="same"),
        ]
    )
    assert report["finding_counts"]["case_family_cross_split"] == 1
    assert report["finding_counts"]["template_key_cross_split"] == 1
    assert not report["ready_for_evaluation"]


def test_spacing_and_number_changes_do_not_hide_duplicates():
    assert normalized_question("대출 123원?") == normalized_question("대출 ９９９ 원!")
    report = audit_entries(
        [
            row("dev", "dev", question="가상 비용 100원?"),
            row("test", "test", question="가상비용 200원!"),
        ]
    )
    assert report["finding_counts"]["normalized_duplicate"] == 1
    assert "가상" not in str(report)


def test_shared_evidence_is_review_signal_not_semantic_approval():
    report = audit_entries(
        [
            row("dev", "dev", evidence_ids=["source"], issue_keys=["issue"]),
            row("test", "test", evidence_ids=["source"], issue_keys=["issue"]),
        ]
    )
    assert report["finding_counts"] == {"shared_issue": 1, "shared_evidence": 1}
    assert report["split_review_status"] == "needs_review"


def test_duplicate_ids_cannot_satisfy_target_counts():
    report = audit_entries([row("a", "dev"), row("a", "dev")])
    assert report["unique_examples"] == 1
    assert report["finding_counts"]["duplicate_id"] == 1
    assert not report["target_counts_match"]


@pytest.mark.integration
def test_audit_refuses_stale_manifest_and_preserves_content_privacy(db, example):
    register_draft(db, example)
    member = {
        "example_id": example.example_id,
        "revision": 1,
        "content_hash": content_hash(example),
        "group_id": example.group_id,
        "split": "dev",
        "case_family": "fixture",
        "template_key": "fixture",
        "issue_keys": ["fixture"],
        "category": "single",
    }
    result = refresh_audit(db, {"members": [member]})
    assert "question" not in result and "payload" not in result
    revised = example.model_copy(update={"question": "changed"})
    register_draft(db, revised)
    with pytest.raises(ValueError, match="stale"):
        refresh_audit(db, {"members": [member]})
