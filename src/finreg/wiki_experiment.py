"""Read-only PostgreSQL adapter and development-only retrieval comparison."""

import hashlib
from collections import Counter
from datetime import date
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid5

from psycopg.rows import dict_row

from finreg.corpus import source_path
from finreg.dataset import reviewed_examples
from finreg.legal_html import extract_provisions
from finreg.wiki_retrieval import (
    CandidateBuild,
    Corpus,
    SearchConfig,
    SourceEvidence,
    fingerprint,
    search,
)


def corpus_status(connection) -> dict:
    """Count review states only; never read held-out payloads."""
    return {
        "active_snapshots": connection.execute("SELECT count(*) FROM corpus_state").fetchone()[0],
        "snapshot_statuses": dict(
            connection.execute(
                "SELECT validation_status,count(*) FROM corpus_snapshot GROUP BY validation_status"
            ).fetchall()
        ),
        "temporal_statuses": dict(
            connection.execute(
                "SELECT temporal_review_status,count(*) FROM applicability "
                "GROUP BY temporal_review_status"
            ).fetchall()
        ),
        "development_statuses": dict(
            connection.execute(
                "SELECT review_status,count(*) FROM current_example "
                "WHERE split='dev' GROUP BY review_status"
            ).fetchall()
        ),
    }


def load_corpus(connection, root: Path, snapshot_id: UUID | None = None) -> Corpus:
    """Caller owns a repeatable-read transaction, fixing source/review state for the run."""
    if snapshot_id is None:
        row = connection.execute("SELECT snapshot_id FROM corpus_state").fetchone()
        if row is None:
            raise ValueError("No active verified snapshot; complete corpus review first")
        snapshot_id = row[0]
    row = connection.execute(
        "SELECT validation_status,coverage FROM corpus_snapshot WHERE snapshot_id=%s",
        (snapshot_id,),
    ).fetchone()
    if not row or row[0] != "verified" or row[1].get("supported") is not True:
        raise ValueError("Snapshot has not passed review")
    coverage = row[1]
    since = date.fromisoformat(coverage["valid_from"])
    until = date.fromisoformat(coverage["valid_to_exclusive"])
    ids = sorted({UUID(value) for value in coverage["provision_ids"]}, key=str)
    if not ids:
        raise ValueError("No verified provision coverage")
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT p.*,d.title,d.document_type,v.content_hash AS source_sha256,"
            "v.source_url,v.raw_path,a.valid_from,a.valid_to_exclusive,"
            "a.temporal_review_status,a.reviewer,a.reviewed_at,a.transition_refs "
            "FROM provision p JOIN document_version v USING(version_id) "
            "JOIN source_document d USING(source_id) JOIN applicability a USING(provision_id) "
            "JOIN snapshot_version sv ON sv.version_id=p.version_id "
            "WHERE sv.snapshot_id=%s AND p.provision_id=ANY(%s) ORDER BY p.provision_id",
            (snapshot_id, ids),
        )
        rows = cursor.fetchall()
    if len(rows) != len(ids):
        raise ValueError("Snapshot contains missing provision references")
    relations = connection.execute(
        "SELECT from_id,to_id,verified FROM provision_relation WHERE from_id=ANY(%s)",
        (ids,),
    ).fetchall()
    related, unresolved = {}, set()
    for source, target, verified in relations:
        if not verified or target not in ids:
            unresolved.add(source)
        else:
            related.setdefault(source, set()).add(target)
    parsed = {}
    evidence = []
    for item in rows:
        if (
            item["temporal_review_status"] != "verified"
            or not item["reviewer"]
            or not item["reviewed_at"]
            or item["valid_from"] is None
            or item["valid_from"] > since
            or (item["valid_to_exclusive"] is not None and item["valid_to_exclusive"] < until)
        ):
            raise ValueError("Snapshot coverage includes unverified applicability")
        version = item["version_id"]
        if version not in parsed:
            raw = source_path(root, item["raw_path"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != item["source_sha256"]:
                raise ValueError("Raw source hash mismatch")
            parsed[version] = {
                uuid5(version, provision["local_id"]): provision
                for provision in extract_provisions(raw.decode("utf-8"))
            }
        original = parsed[version].get(item["provision_id"])
        if not original or any(
            original[field] != item[field] for field in ("text", "source_offsets", "article")
        ):
            raise ValueError("Stored provision no longer matches original source")
        parent = original["parent_local_id"]
        if item["parent_id"] != (uuid5(version, parent) if parent else None):
            raise ValueError("Stored provision parent differs from source")
        evidence.append(
            SourceEvidence(
                **{
                    key: item[key]
                    for key in (
                        "provision_id",
                        "version_id",
                        "source_sha256",
                        "title",
                        "article",
                        "document_type",
                        "source_url",
                        "text",
                        "source_offsets",
                        "valid_from",
                        "valid_to_exclusive",
                        "parent_id",
                    )
                },
                temporal_verified=True,
                related_ids=tuple(sorted(related.get(item["provision_id"], ()), key=str)),
                unresolved_references=item["provision_id"] in unresolved,
                transition_requires_facts=bool(item["transition_refs"]),
            )
        )
    return Corpus(
        snapshot_id=snapshot_id,
        validation_status="verified",
        valid_from=since,
        valid_to_exclusive=until,
        evidence=tuple(evidence),
    )


def load_build(root: Path, path: Path) -> CandidateBuild:
    """Only explicit experiment artifacts are inputs; no project wiki/dataset directory scan."""
    allowed = root.resolve() / "artifacts" / "legal-wiki"
    resolved = path.resolve()
    if not resolved.is_relative_to(allowed) or resolved.suffix != ".json":
        raise ValueError("Wiki input must be a JSON artifact under artifacts/legal-wiki")
    if resolved.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Wiki artifact is too large")
    return CandidateBuild.model_validate_json(resolved.read_text(encoding="utf-8"))


def compare_development(
    connection,
    corpus: Corpus,
    dataset_id: str,
    config: SearchConfig,
    wiki: CandidateBuild | None,
    *,
    wiki_unavailable=False,
) -> dict:
    # This exporter constrains SQL to split=dev; there is intentionally no split parameter.
    rows = reviewed_examples(connection, dataset_id, "dev")
    if not rows:
        raise ValueError("No human-reviewed development examples for this dataset")
    results, skipped = [], Counter()
    for row in rows:
        payload = row["payload"]
        if payload["split"] != "dev":
            raise ValueError("Development inventory contains a non-development example")
        if payload["expected_status"] != "answered":
            skipped[payload["expected_status"]] += 1
            continue  # Retrieval recall does not evaluate abstention or legal answers.
        if UUID(payload["snapshot_id"]) != corpus.snapshot_id:
            raise ValueError("Development example uses a different snapshot")
        gold = {UUID(item["provision_id"]) for item in payload["evidence"]}
        if not gold or not gold <= {item.provision_id for item in corpus.evidence}:
            raise ValueError("Missing gold evidence in development example")
        pair = {}
        for name, build in (("lexical", None), ("lexical_wiki", wiki)):
            start = perf_counter()
            result = search(
                corpus,
                payload["question"],
                date.fromisoformat(payload["as_of_date"]),
                config=config,
                wiki=build,
                wiki_unavailable=wiki_unavailable and name == "lexical_wiki",
            )
            pair[name] = {
                "recall_at_k": len(gold & set(result.ranked_ids)) / len(gold),
                "context_recall": len(gold & {item.provision_id for item in result.evidence})
                / len(gold),
                "latency_ms": (perf_counter() - start) * 1000,
                "context_chars": sum(len(item.text) for item in result.evidence),
                "ranked_ids": list(map(str, result.ranked_ids)),
                "omitted_ids": list(map(str, result.omitted_ids)),
                "unresolved_ids": list(map(str, result.unresolved_ids)),
                "wiki_status": result.wiki_status,
                "selected_page_ids": result.selected_page_ids,
                "rejected_pages": result.rejected_pages,
            }
        results.append(
            {
                "example_id": row["example_id"],
                "revision": row["revision"],
                "content_hash": row["content_hash"],
                **pair,
            }
        )
    if not results:
        raise ValueError("No reviewed answerable development examples")
    return {
        "experiment": "lexical-vs-lexical-wiki-v1",
        "dataset_id": dataset_id,
        "snapshot_id": str(corpus.snapshot_id),
        "corpus_sha256": fingerprint(corpus),
        "wiki_build_id": fingerprint(wiki) if wiki else None,
        "config": config.model_dump(),
        "config_sha256": fingerprint(config),
        "answerable_count": len(results),
        "excluded_status_counts": dict(skipped),
        "means": {
            name: {
                metric: sum(row[name][metric] for row in results) / len(results)
                for metric in ("recall_at_k", "context_recall", "latency_ms", "context_chars")
            }
            for name in ("lexical", "lexical_wiki")
        },
        "cases": results,
        "service_adoption": "not_evaluated",
        "limitations": [
            "Lexical retrieval only; no embeddings, generation or legal quality scores.",
            "Character budget is not a model token budget.",
            "No user questions, evaluation questions, answers or source text in report.",
        ],
    }
