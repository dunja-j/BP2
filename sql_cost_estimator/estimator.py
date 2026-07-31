from __future__ import annotations

from pathlib import Path

from .models import EstimationResult, EstimatorInput
from .optimizer import optimize_query
from .schema_loader import load_input
from .sql_parser import parse_sql


def estimate(estimator_input: EstimatorInput) -> EstimationResult:
    parsed_query = parse_sql(estimator_input.query, estimator_input.tables)
    return optimize_query(estimator_input, parsed_query)


def estimate_file(path: str | Path, query: str | None = None) -> EstimationResult:
    return estimate(load_input(path, query=query))
