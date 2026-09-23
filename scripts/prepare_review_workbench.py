"""Prepare an offline review screen for explicitly selected development datasets."""

import argparse
import json
from pathlib import Path

import psycopg

from finreg.config import Settings
from finreg.migrate import connect_database
from finreg.review_workbench import collect_bundle, write_workbench


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        if args.output.exists():
            raise FileExistsError("Output already exists")
        with connect_database(Settings()) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            bundle, assets = collect_bundle(connection, root, args.dataset)
        result = write_workbench(root, args.output, bundle, assets)
        print(json.dumps({key: value for key, value in result.items() if key != "files"}))
        return 0
    except (OSError, ValueError, psycopg.Error) as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "hint": "Check datasets, original source integrity and a new output folder.",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
