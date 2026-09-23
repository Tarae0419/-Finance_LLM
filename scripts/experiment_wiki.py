"""Read-only corpus status, Wiki draft templates and development retrieval comparison."""

import argparse
import json
from pathlib import Path
from uuid import UUID

import psycopg

from finreg.config import Settings
from finreg.migrate import connect_database
from finreg.wiki_experiment import compare_development, corpus_status, load_build, load_corpus
from finreg.wiki_retrieval import SearchConfig, draft_build, fingerprint, page_hash

ROOT = Path(__file__).resolve().parents[1]


def write_artifact(path: Path, payload: dict, folder: str):
    allowed = ROOT / "artifacts" / folder
    path = path.resolve()
    if not path.is_relative_to(allowed) or path.suffix != ".json":
        raise ValueError("Output must be a JSON file in the specified experiment artifact folder")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, indent=2)
        output.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--wiki", type=Path, required=True)
    draft = commands.add_parser("draft")
    draft.add_argument("--snapshot", type=UUID)
    draft.add_argument("--output", type=Path, required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("--snapshot", type=UUID)
    compare.add_argument("--dataset", required=True)
    compare.add_argument("--wiki", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            build = load_build(ROOT, args.wiki)
            print(
                json.dumps(
                    {
                        "build_id": fingerprint(build),
                        "pages": [
                            {
                                "page_id": page.page_id,
                                "page_sha256": page_hash(page),
                                "review_record_present": page.review is not None,
                            }
                            for page in build.pages
                        ],
                        "note": "Hashes only; this is not approval or source validation.",
                    }
                )
            )
            return 0
        with connect_database(Settings()) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            if args.command == "status":
                print(json.dumps(corpus_status(connection)))
                return 0
            corpus = load_corpus(connection, ROOT, args.snapshot)
            if args.command == "draft":
                payload = draft_build(corpus).model_dump(mode="json")
                write_artifact(args.output, payload, "legal-wiki")
            else:
                config = SearchConfig.model_validate_json(
                    (ROOT / "configs/wiki_retrieval.json").read_text(encoding="utf-8")
                )
                unavailable = False
                try:
                    wiki = load_build(ROOT, args.wiki)
                except (OSError, ValueError):
                    wiki, unavailable = None, True
                payload = compare_development(
                    connection, corpus, args.dataset, config, wiki, wiki_unavailable=unavailable
                )
                write_artifact(args.output, payload, "wiki-retrieval")
        print("Artifact written. This experiment does not enable service retrieval.")
        return 0
    except (OSError, ValueError, KeyError, psycopg.Error) as error:
        # Do not print database exceptions, credentials, input documents or questions.
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error_type": type(error).__name__,
                    "next_step": "Check status, verified coverage, reviewed dev data and paths.",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
