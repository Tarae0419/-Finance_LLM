import json
import runpy
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb
from test_corpus import db, source_fixture  # noqa: F401

from finreg.corpus import create_snapshot, ingest_source
from finreg.wiki_experiment import compare_development, load_build, load_corpus
from finreg.wiki_retrieval import (
    CandidateBuild,
    CandidatePage,
    Corpus,
    EvidenceRef,
    Generator,
    PageReview,
    SearchConfig,
    SourceEvidence,
    SourceOffsets,
    draft_build,
    fingerprint,
    page_hash,
    search,
)


@pytest.fixture
def corpus():
    def provision(number, text, article):
        return SourceEvidence(
            provision_id=UUID(int=number),
            version_id=UUID(int=100),
            source_sha256="a" * 64,
            title="가상 규정",
            article=article,
            document_type="law",
            source_url="https://example.invalid/fictional",
            text=text,
            source_offsets=SourceOffsets(format="html", unit="unicode_codepoint", start=0, end=20),
            valid_from=date(2000, 1, 1),
            valid_to_exclusive=date(2001, 1, 1),
            temporal_verified=True,
        )

    return Corpus(
        snapshot_id=UUID(int=200),
        validation_status="verified",
        valid_from=date(2000, 1, 1),
        valid_to_exclusive=date(2001, 1, 1),
        evidence=(
            provision(1, "만기 이전 반환 시 금액을 설명한다. 다만, 가상 면제는 제외한다.", "제1조"),
            provision(2, "가상 우편물의 색상을 설명한다.", "제10조"),
        ),
    )


def synthetic_review(page):
    # Fixture-only attestation. Never written into the real corpus or review artifacts.
    passed = {"result": "pass", "reason": "Fictional unit-test state only"}
    return page.model_copy(
        update={
            "review": PageReview(
                page_sha256=page_hash(page),
                reviewer="synthetic-test-only",
                reviewer_role="external",
                method="synthetic fixture",
                reviewed_at=datetime(2000, 1, 1, tzinfo=UTC),
                human_attested=True,
                existence=passed,
                meaning=passed,
                temporal=passed,
                completeness=passed,
            )
        }
    )


def build_for(corpus, *, ref=None):
    item = corpus.evidence[0]
    page = synthetic_review(
        CandidatePage(
            page_id="fictional-topic",
            title="조기상환 안내",
            summary="가상 조기상환 조건의 탐색용 요약",
            refs=(
                ref
                or EvidenceRef(
                    provision_id=item.provision_id,
                    version_id=item.version_id,
                    source_sha256=item.source_sha256,
                    evidence_sha256=fingerprint(item),
                ),
            ),
        )
    )
    return CandidateBuild(
        snapshot_id=corpus.snapshot_id,
        corpus_sha256=fingerprint(corpus),
        generator=Generator(
            model_id="synthetic",
            revision="fixture-v1",
            prompt_version="none",
            settings="Not a model run",
        ),
        pages=(page,),
    )


def test_wiki_can_add_source_candidate_but_never_its_summary(corpus):
    baseline = search(corpus, "조기상환", date(2000, 6, 1))
    result = search(corpus, "조기상환", date(2000, 6, 1), wiki=build_for(corpus))
    assert baseline.ranked_ids == ()
    assert result.ranked_ids == (UUID(int=1),)
    assert result.evidence == (corpus.evidence[0],)
    assert "다만" in result.evidence[0].text
    assert result.wiki_status == "used"
    assert result.selected_page_ids == ("fictional-topic",)


def test_exact_article_title_and_type_filters_are_not_bypassed_by_wiki(corpus):
    result = search(corpus, "제 1 조", date(2000, 1, 1), wiki=build_for(corpus))
    assert result.ranked_ids == (UUID(int=1),)
    assert not search(
        corpus, "조기상환", date(2000, 6, 1), title="다른 규정", wiki=build_for(corpus)
    ).evidence
    assert not search(
        corpus,
        "조기상환",
        date(2000, 6, 1),
        document_types=("official_faq",),
        wiki=build_for(corpus),
    ).evidence


@pytest.mark.parametrize(
    "change",
    [
        {"temporal_verified": False},
        {"valid_from": None},
        {"valid_from": date(2000, 7, 1)},
        {"valid_to_exclusive": date(2000, 6, 1)},
        {"transition_requires_facts": True},
    ],
)
def test_unreviewed_future_expired_and_transition_evidence_is_excluded(corpus, change):
    changed = corpus.model_copy(
        update={
            "evidence": (
                corpus.evidence[0].model_copy(update=change),
                corpus.evidence[1],
            )
        }
    )
    result = search(changed, "제1조 조기상환", date(2000, 6, 1), wiki=build_for(changed))
    assert result.ranked_ids == ()


def test_unknown_end_is_bounded_by_verified_snapshot_period(corpus):
    changed = corpus.model_copy(
        update={"evidence": (corpus.evidence[0].model_copy(update={"valid_to_exclusive": None}),)}
    )
    assert search(changed, "제1조", date(2000, 1, 1)).evidence
    for day in (date(1999, 12, 31), date(2001, 1, 1)):
        with pytest.raises(ValueError, match="outside"):
            search(changed, "제1조", day, wiki=build_for(changed))


def test_old_snapshot_or_changed_corpus_falls_back_exactly(corpus):
    build = build_for(corpus)
    for changed in (
        build.model_copy(update={"snapshot_id": uuid4()}),
        build.model_copy(update={"corpus_sha256": "b" * 64}),
    ):
        baseline = search(corpus, "설명", date(2000, 6, 1))
        result = search(corpus, "설명", date(2000, 6, 1), wiki=changed)
        assert result.evidence == baseline.evidence
        assert result.ranked_ids == baseline.ranked_ids
        assert result.wiki_status == "stale_build"


@pytest.mark.parametrize(
    "field,value",
    [
        ("provision_id", UUID(int=999)),
        ("version_id", UUID(int=999)),
        ("source_sha256", "b" * 64),
        ("evidence_sha256", "b" * 64),
    ],
)
def test_false_or_stale_reference_is_rejected(corpus, field, value):
    original = build_for(corpus).pages[0].refs[0]
    result = search(
        corpus,
        "조기상환",
        date(2000, 6, 1),
        wiki=build_for(corpus, ref=original.model_copy(update={field: value})),
    )
    assert not result.evidence
    assert result.rejected_pages


def test_draft_changed_summary_and_incomplete_review_cannot_supply_candidates(corpus):
    build = build_for(corpus)
    page = build.pages[0]
    for changed in (
        page.model_copy(update={"review": None}),
        page.model_copy(update={"summary": "Edited after review"}),
        page.model_copy(
            update={
                "review": page.review.model_copy(
                    update={
                        "reviewer_role": "developer",
                        "official_cross_checks": (),
                    }
                )
            }
        ),
    ):
        result = search(
            corpus,
            "조기상환",
            date(2000, 6, 1),
            wiki=build.model_copy(update={"pages": (changed,)}),
        )
        assert result.wiki_status == "no_usable_match"
        assert not result.evidence
    draft = draft_build(corpus)
    assert all(page.review is None and page.summary == "" for page in draft.pages)
    assert draft.generator.model_id == "none"


def test_unresolved_references_prevent_wiki_promotion_but_mark_raw_context(corpus):
    changed = corpus.model_copy(
        update={
            "evidence": (corpus.evidence[0].model_copy(update={"unresolved_references": True}),)
        }
    )
    result = search(changed, "제1조 조기상환", date(2000, 6, 1), wiki=build_for(changed))
    assert result.wiki_status == "no_usable_match"
    assert result.unresolved_ids == (UUID(int=1),)
    assert result.evidence == changed.evidence


def test_related_context_cycles_and_budget_keep_entire_provisions(corpus):
    first, second = corpus.evidence
    changed = corpus.model_copy(
        update={
            "evidence": (
                first.model_copy(update={"related_ids": (second.provision_id,)}),
                second.model_copy(update={"parent_id": first.provision_id}),
            )
        }
    )
    result = search(
        changed, "제1조", date(2000, 1, 1), config=SearchConfig(max_context_chars=len(first.text))
    )
    assert result.ranked_ids == (first.provision_id,)
    assert result.evidence == (changed.evidence[0],)
    assert result.omitted_ids == (second.provision_id,)
    assert result.evidence[0].text == first.text


def test_transitive_unresolved_context_also_excludes_wiki(corpus):
    first, second = corpus.evidence
    changed = corpus.model_copy(
        update={
            "evidence": (
                first.model_copy(update={"related_ids": (second.provision_id,)}),
                second.model_copy(update={"unresolved_references": True}),
            )
        }
    )
    result = search(changed, "조기상환", date(2000, 6, 1), wiki=build_for(changed))
    assert result.wiki_status == "no_usable_match"
    assert result.rejected_pages == (("fictional-topic", "unresolved_source_context"),)


def test_unavailable_wiki_and_invalid_artifact_preserve_baseline(corpus, tmp_path):
    baseline = search(corpus, "제1조", date(2000, 6, 1))
    result = search(corpus, "제1조", date(2000, 6, 1), wiki_unavailable=True)
    assert result.evidence == baseline.evidence
    assert result.wiki_status == "unavailable"
    forbidden = tmp_path / "data/sealed/answers.json"
    with pytest.raises(ValueError, match="artifacts/legal-wiki"):
        load_build(tmp_path, forbidden)  # Rejected before any file is opened.
    allowed = tmp_path / "artifacts/legal-wiki/broken.json"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("{bad", encoding="utf-8")
    with pytest.raises(ValueError):
        load_build(tmp_path, allowed)


def test_development_comparison_uses_dev_only_and_does_not_export_text(corpus, monkeypatch):
    def reviewed(connection, dataset_id, split):
        assert split == "dev"
        return [
            {
                "example_id": "fictional-dev",
                "revision": 1,
                "content_hash": "c" * 64,
                "payload": {
                    "split": "dev",
                    "snapshot_id": str(corpus.snapshot_id),
                    "expected_status": "answered",
                    "question": "조기상환",
                    "as_of_date": "2000-06-01",
                    "evidence": [{"provision_id": str(UUID(int=1))}],
                },
            }
        ]

    monkeypatch.setattr("finreg.wiki_experiment.reviewed_examples", reviewed)
    report = compare_development(None, corpus, "synthetic", SearchConfig(), build_for(corpus))
    assert report["means"]["lexical"]["recall_at_k"] == 0
    assert report["means"]["lexical_wiki"]["recall_at_k"] == 1
    assert report["service_adoption"] == "not_evaluated"
    raw = json.dumps(report, ensure_ascii=False)
    assert "조기상환" not in raw and corpus.evidence[0].text not in raw
    assert "question" not in report["cases"][0]


def test_empty_reviewed_development_set_is_not_a_zero_score(corpus, monkeypatch):
    monkeypatch.setattr("finreg.wiki_experiment.reviewed_examples", lambda *args: [])
    with pytest.raises(ValueError, match="No human-reviewed"):
        compare_development(None, corpus, "synthetic", SearchConfig(), build_for(corpus))


def test_artifact_roundtrip_inspection_and_no_overwrite(corpus, tmp_path, monkeypatch, capsys):
    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/experiment_wiki.py")
    )
    write = namespace["write_artifact"]
    monkeypatch.setitem(write.__globals__, "ROOT", tmp_path)
    output = tmp_path / "artifacts/legal-wiki/fictional.json"
    build = build_for(corpus)
    write(output, build.model_dump(mode="json"), "legal-wiki")
    assert load_build(tmp_path, output) == build
    with pytest.raises(FileExistsError):
        write(output, {}, "legal-wiki")
    with pytest.raises(ValueError, match="Output"):
        write(tmp_path / "data/sealed/forbidden.json", {}, "legal-wiki")
    monkeypatch.setattr(sys, "argv", ["experiment_wiki.py", "inspect", "--wiki", str(output)])
    assert namespace["main"]() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["pages"][0]["page_sha256"] == page_hash(build.pages[0])
    assert "summary" not in report["pages"][0]


def verified_fixture(connection, tmp_path):
    version = ingest_source(connection, tmp_path, source_fixture(tmp_path))
    snapshot = create_snapshot(connection, [version])
    ids = [
        str(row[0]) for row in connection.execute("SELECT provision_id FROM provision").fetchall()
    ]
    # Only the test transaction receives these artificial review values.
    connection.execute(
        "UPDATE applicability SET valid_from='2000-01-02', valid_to_exclusive='2001-01-01', "
        "temporal_review_status='verified', reviewer='synthetic-test', reviewed_at=now()"
    )
    connection.execute(
        "UPDATE corpus_snapshot SET validation_status='verified',coverage=%s",
        (
            Jsonb(
                {
                    "supported": True,
                    "provision_ids": ids,
                    "valid_from": "2000-01-02",
                    "valid_to_exclusive": "2001-01-01",
                }
            ),
        ),
    )
    return snapshot


@pytest.mark.integration
def test_db_adapter_rejects_pending_snapshot_without_changing_it(db, tmp_path):  # noqa: F811
    version = ingest_source(db, tmp_path, source_fixture(tmp_path))
    snapshot = create_snapshot(db, [version])
    with pytest.raises(ValueError, match="passed review"):
        load_corpus(db, tmp_path, snapshot)
    assert db.execute("SELECT validation_status FROM corpus_snapshot").fetchone()[0] == "pending"


@pytest.mark.integration
def test_db_adapter_replays_source_and_preserves_unresolved_context(db, tmp_path):  # noqa: F811
    snapshot = verified_fixture(db, tmp_path)
    corpus = load_corpus(db, tmp_path, snapshot)
    assert corpus.evidence
    assert any(item.unresolved_references for item in corpus.evidence)
    db.execute("UPDATE provision SET text='altered stored text'")
    with pytest.raises(ValueError, match="original source"):
        load_corpus(db, tmp_path, snapshot)


@pytest.mark.integration
def test_db_adapter_rejects_changed_raw_bytes(db, tmp_path):  # noqa: F811
    snapshot = verified_fixture(db, tmp_path)
    raw_path = db.execute("SELECT raw_path FROM document_version").fetchone()[0]
    (tmp_path / raw_path).write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_corpus(db, tmp_path, snapshot)
