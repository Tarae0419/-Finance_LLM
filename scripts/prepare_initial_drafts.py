"""Create 20 development drafts from pinned corpus evidence; never human-review them."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import UUID

from finreg.config import Settings
from finreg.dataset import Example, content_hash, register_draft, review_template
from finreg.migrate import connect_database, migrate

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = UUID("309b08b6-8a66-5c9a-b7eb-23a65cd783ea")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data/review/initial-20")
    args = parser.parse_args()
    directory = args.output.resolve()
    if directory.exists():
        raise FileExistsError(
            "Review folder exists; choose a new --output folder to preserve edits"
        )
    started = perf_counter()
    seeds = json.loads((ROOT / "configs/initial_questions.json").read_text(encoding="utf-8"))
    entries = []
    packets = []
    with connect_database(Settings()) as connection:
        migrate(connection, ROOT / "migrations")
        with connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(72401003)")
            for seed in seeds:
                evidence = []
                sources = []
                for document_type, article in seed["sources"]:
                    rows = connection.execute(
                        "SELECT p.provision_id,p.text,s.title,v.source_url,v.version_id "
                        "FROM provision p JOIN document_version v USING(version_id) "
                        "JOIN source_document s USING(source_id) "
                        "JOIN snapshot_version sv ON sv.version_id=v.version_id "
                        "WHERE sv.snapshot_id=%s AND s.document_type=%s AND p.article=%s "
                        "AND p.provision_kind='article'",
                        (SNAPSHOT, document_type, article),
                    ).fetchall()
                    if len(rows) != 1:
                        raise ValueError(
                            f"Expected exactly one pinned article: {document_type}/{article}"
                        )
                    row = rows[0]
                    evidence.append({"provision_id": str(row[0]), "quote": row[1]})
                    sources.append(
                        {
                            "provision_id": str(row[0]),
                            "version_id": str(row[4]),
                            "title": row[2],
                            "article": article,
                            "source_url": row[3],
                        }
                    )
                example = Example(
                    example_id="s1-dev-" + seed["id"],
                    dataset_id="s1-initial-20-v1",
                    group_id="loan-explanation-core-dev",
                    split="dev",
                    snapshot_id=SNAPSHOT,
                    question=seed["question"],
                    as_of_date=seed.get("as_of_date", "2026-09-22"),
                    facts={
                        "origin": "public_hypothetical",
                        "scenario": "질문에 명시한 사실만 가정",
                    },
                    category=seed["category"],
                    evidence=evidence,
                    expected_answer=seed["answer"],
                    expected_status=seed["status"],
                    limitations=[
                        "미검수 답변 후보",
                        "조문별 시행·경과조치 미검수",
                        "정의·위임·예외·별표 완전성 미검수",
                        "공식 FAQ/해석 교차 대조 필요",
                    ],
                )
                existing = connection.execute(
                    "SELECT content_hash,review_status FROM current_example WHERE example_id=%s",
                    (example.example_id,),
                ).fetchone()
                if existing and existing != (content_hash(example), "draft"):
                    raise ValueError("Existing draft was edited or reviewed; refusing seed reset")
                revision = register_draft(connection, example)
                record = example.model_dump(mode="json")
                entries.append(record)
                packets.append(review_template(example, revision))
                record["source_metadata"] = sources
    directory.mkdir(parents=True, exist_ok=False)
    # Draft outputs can be reproduced. Never overwrite a person's filled review input.
    (directory / "examples.json").write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    template = directory / "review-template.json"
    if not template.exists():
        template.write_text(
            json.dumps(packets, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    manifest = {
        "dataset_id": "s1-initial-20-v1",
        "snapshot_id": str(SNAPSHOT),
        "split": "dev",
        "group_id": "loan-explanation-core-dev",
        "count": len(entries),
        "review_status": "draft",
        "created_at": datetime.now(UTC).isoformat(),
        "generation_runtime_seconds": round(perf_counter() - started, 3),
        "human_review_seconds": None,
        "human_revision_seconds": None,
        "manual_authoring_seconds": None,
        "hashes": {entry["example_id"]: entry["content_hash"] for entry in packets},
        "notes": [
            "기준일은 고정한 가상 질문 조건이며 적용 가능 판정이 아니다.",
            "실행 시간은 사람 작성·검수 시간 추정에 사용할 수 없다.",
            "20건 모두 개발용; 최종 평가 문항으로 재사용 금지.",
        ],
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# 초기 20문항 검수 목록",
        "",
        "모두 자동 작성한 **draft**이며 정답이 아닙니다.",
        "근거 원문과 버전은 examples.json, 작성할 체크리스트는 review-template.json에 있습니다.",
        "",
        "| ID | 유형 | 질문 | 기대 상태 후보 |",
        "| --- | --- | --- | --- |",
    ]
    for entry in entries:
        lines.append(
            f"| {entry['example_id']} | {entry['category']} | {entry['question']} | "
            f"{entry['expected_status']} |"
        )
    (directory / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Prepared {len(entries)} development drafts at {directory}")


if __name__ == "__main__":
    main()
