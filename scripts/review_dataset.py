"""Import human-authored review records and export reviewed development/training examples."""

import argparse
import json
from pathlib import Path

from finreg.config import Settings
from finreg.dataset import (
    Example,
    HumanReview,
    record_review,
    register_draft,
    review_template,
    reviewed_examples,
)
from finreg.migrate import connect_database, migrate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    draft = commands.add_parser("import-drafts")
    draft.add_argument("path", type=Path)
    review = commands.add_parser("import-reviews")
    review.add_argument("path", type=Path)
    review.add_argument("--attest-human-review", action="store_true", required=True)
    export = commands.add_parser("export-reviewed")
    export.add_argument("--dataset", required=True)
    export.add_argument("--split", choices=["dev", "train"], required=True)
    export.add_argument("--output", type=Path, required=True)
    template = commands.add_parser("prepare-review")
    template.add_argument("--example", required=True)
    template.add_argument("--output", type=Path, required=True)
    commands.add_parser("status")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with connect_database(Settings()) as connection:
        migrate(connection, root / "migrations")
        with connection.transaction():
            if args.command in {"import-drafts", "import-reviews"}:
                entries = json.loads(args.path.read_text(encoding="utf-8"))
                if isinstance(entries, dict):
                    entries = [entries]
                results = []
                for entry in entries:
                    if args.command == "import-drafts":
                        # Display-only metadata is resolved from DB, not trusted input.
                        entry.pop("source_metadata", None)
                        example = Example.model_validate(entry)
                        results.append((example.example_id, register_draft(connection, example)))
                    else:
                        item = HumanReview.model_validate(entry)
                        results.append((item.example_id, record_review(connection, item)))
                print(json.dumps(results, ensure_ascii=False))
            elif args.command == "export-reviewed":
                rows = reviewed_examples(connection, args.dataset, args.split)
                if not rows:
                    raise ValueError("No reviewed examples eligible for export")
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8") as output:
                    json.dump(rows, output, ensure_ascii=False, indent=2)
                    output.write("\n")
                print(f"Exported {len(rows)} reviewed {args.split} examples")
            elif args.command == "prepare-review":
                row = connection.execute(
                    "SELECT payload,revision FROM current_example "
                    "WHERE example_id=%s AND split IN ('train','dev')",
                    (args.example,),
                ).fetchone()
                if not row:
                    raise ValueError("No development/training example with that ID")
                packet = review_template(Example.model_validate(row[0]), row[1])
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8") as output:
                    json.dump(packet, output, ensure_ascii=False, indent=2)
                    output.write("\n")
                print(f"Prepared blank checklist for revision {row[1]}")
            else:
                for row in connection.execute(
                    "SELECT dataset_id,split,review_status,count(*),"
                    "count(*) FILTER (WHERE external_review) "
                    "FROM current_example GROUP BY dataset_id,split,review_status "
                    "ORDER BY dataset_id,split,review_status"
                ):
                    print(row)


if __name__ == "__main__":
    main()
