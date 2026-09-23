"""Pinned evaluation inventories and content-free split audit reports."""

import hashlib
import json
import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path

from finreg.dataset import Example, content_hash, register_draft, review_template

CATEGORIES = (
    "single",
    "multiple_exception",
    "temporal",
    "missing_facts",
    "out_of_scope_no_evidence",
)
TARGETS = {"dev": 50, "test": 100}
TEST_CATEGORIES = dict(zip(CATEGORIES, (30, 25, 15, 15, 15), strict=True))


def normalized_question(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"\d+", "#", text)
    return "".join(character for character in text if character.isalnum() or character == "#")


def audit_entries(entries: list[dict]) -> dict:
    """Similarity is a warning, never proof of semantic independence or legal accuracy."""
    findings = []
    finding_counts = Counter()

    def emit(finding):
        finding_counts[finding["kind"]] += 1
        if finding_counts[finding["kind"]] <= 20:
            findings.append(finding)

    seen = set()
    for row in entries:
        if row["example_id"] in seen:
            emit({"kind": "duplicate_id", "severity": "block", "ids": [row["example_id"]]})
        seen.add(row["example_id"])
        if not row.get("case_family") or not row.get("template_key") or not row.get("issue_keys"):
            emit(
                {"kind": "missing_split_metadata", "severity": "block", "ids": [row["example_id"]]}
            )
        if row["category"] not in CATEGORIES:
            raise ValueError("Unknown evaluation category")
    for left, right in combinations(entries, 2):
        if left["split"] == right["split"]:
            continue
        pair = [left["example_id"], right["example_id"]]
        for field in ("group_id", "case_family", "template_key"):
            if left.get(field) and left[field] == right.get(field):
                emit({"kind": field + "_cross_split", "severity": "block", "ids": pair})
        a, b = normalized_question(left["question"]), normalized_question(right["question"])
        if a == b:
            emit({"kind": "normalized_duplicate", "severity": "block", "ids": pair})
        elif SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.82:
            emit({"kind": "similar_question", "severity": "review", "ids": pair})
        if set(left["issue_keys"]) & set(right["issue_keys"]):
            emit({"kind": "shared_issue", "severity": "review", "ids": pair})
        if set(left["evidence_ids"]) & set(right["evidence_ids"]):
            emit({"kind": "shared_evidence", "severity": "review", "ids": pair})
    counts = dict(Counter(row["split"] for row in entries))
    test_counts = dict(Counter(row["category"] for row in entries if row["split"] == "test"))
    reviewed = sum(row["review_status"] == "reviewed" for row in entries)
    return {
        "counts": counts,
        "test_category_counts": test_counts,
        "targets": TARGETS,
        "test_category_targets": TEST_CATEGORIES,
        "target_counts_match": counts == TARGETS and test_counts == TEST_CATEGORIES,
        "unique_examples": len(seen),
        "reviewed": reviewed,
        "pending_review": len(entries) - reviewed,
        "groups": {
            split: len({row["group_id"] for row in entries if row["split"] == split})
            for split in TARGETS
        },
        "distinct_normalized_questions": {
            split: len(
                {normalized_question(row["question"]) for row in entries if row["split"] == split}
            )
            for split in TARGETS
        },
        "findings": findings,
        "finding_counts": dict(finding_counts),
        "finding_sample_limit_per_kind": 20,
        "split_review_status": "needs_review" if findings else "pending_human_review",
        "ready_for_evaluation": False,
        "sealed": False,
        "human_review_time_estimate_seconds": None,
    }


def prepare_inventory(
    connection, candidates: list[dict], dev_directory: Path, test_directory: Path
):
    """Write separate draft packets without overwriting files or prior human revisions."""
    if dev_directory.resolve() == test_directory.resolve():
        raise ValueError("Development and holdout directories must differ")
    if dev_directory.exists() or test_directory.exists():
        raise FileExistsError("Choose new packet directories; existing review work is preserved")
    entries, packets, examples = [], {"dev": [], "test": []}, {"dev": [], "test": []}
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(72401003)")
        for candidate in candidates:
            example = Example.model_validate(candidate["example"])
            if example.split not in TARGETS:
                raise ValueError("Only development and holdout candidates belong in this inventory")
            metadata = candidate["split_metadata"]
            if set(metadata) != {"category", "case_family", "template_key", "issue_keys"}:
                raise ValueError("Unexpected split metadata fields")
            if metadata["category"] not in CATEGORIES:
                raise ValueError("Unknown evaluation category")
            latest = connection.execute(
                "SELECT revision,content_hash FROM current_example WHERE example_id=%s",
                (example.example_id,),
            ).fetchone()
            if latest and latest[1] != content_hash(example):
                raise ValueError(
                    "Candidate conflicts with an existing revision; import revision explicitly"
                )
            revision = register_draft(connection, example)
            status = connection.execute(
                "SELECT review_status FROM current_example WHERE example_id=%s",
                (example.example_id,),
            ).fetchone()[0]
            entry = {
                "example_id": example.example_id,
                "revision": revision,
                "content_hash": content_hash(example),
                "group_id": example.group_id,
                "split": example.split,
                "question": example.question,
                "evidence_ids": [str(item.provision_id) for item in example.evidence],
                "review_status": status,
                **metadata,
            }
            entries.append(entry)
            packets[example.split].append(review_template(example, revision))
            examples[example.split].append(example.model_dump(mode="json"))
        report = audit_entries(entries)
        if not report["target_counts_match"] or report["unique_examples"] != 150:
            raise ValueError("Inventory must contain 50 unique dev and 100 unique test candidates")
        # Draft candidates with findings are retained for human correction, never approved.
        for split, directory in (("dev", dev_directory), ("test", test_directory)):
            directory.mkdir(parents=True, exist_ok=False)
            for filename, value in (
                ("examples.json", examples[split]),
                ("review-template.json", packets[split]),
            ):
                (directory / filename).write_text(
                    json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
    public_entries = [
        {key: value for key, value in entry.items() if key not in {"question", "evidence_ids"}}
        for entry in entries
    ]
    return {
        "schema_version": 1,
        "members": public_entries,
        "artifacts": {
            split: {
                "path": directory.as_posix(),
                "examples_sha256": hashlib.sha256(
                    (directory / "examples.json").read_bytes()
                ).hexdigest(),
            }
            for split, directory in (("dev", dev_directory), ("test", test_directory))
        },
        "audit": report,
    }


def refresh_audit(connection, manifest: dict) -> dict:
    """Explicit split-audit path: emits only IDs, counts and findings, not test text."""
    entries = []
    for member in manifest["members"]:
        row = connection.execute(
            "SELECT revision,content_hash,payload,review_status,split FROM current_example "
            "WHERE example_id=%s",
            (member["example_id"],),
        ).fetchone()
        if not row or row[:2] != (member["revision"], member["content_hash"]):
            raise ValueError("Inventory contains a stale or missing revision; rebuild membership")
        payload = row[2]
        if member["split"] != row[4] or member["group_id"] != payload["group_id"]:
            raise ValueError("Inventory split/group does not match database")
        entries.append(
            {
                **member,
                "review_status": row[3],
                "question": payload["question"],
                "evidence_ids": [item["provision_id"] for item in payload["evidence"]],
            }
        )
    return audit_entries(entries)
