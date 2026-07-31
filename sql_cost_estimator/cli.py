from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .errors import EstimatorError
from .estimator import estimate_file
from .reporting import render_text

DEFAULT_SCHEMA_PATH = Path("schema.json")
DEFAULT_QUERY_PATH = Path("query.sql")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sql-cost-estimator",
        description=(
            "Bira fizicki plan SQL upita i procenjuje cenu u blok transferima."
        ),
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA_PATH,
        help="JSON sa semom i statistikama (podrazumevano: schema.json).",
    )
    parser.add_argument(
        "--query",
        type=Path,
        default=DEFAULT_QUERY_PATH,
        help="Fajl sa SQL upitom (podrazumevano: query.sql).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        query = args.query.read_text(encoding="utf-8").strip()
        result = estimate_file(args.schema, query=query)
        print(render_text(result))
        return 0
    except EstimatorError as error:
        print(f"Greska: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"Greska pri radu sa fajlom: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
