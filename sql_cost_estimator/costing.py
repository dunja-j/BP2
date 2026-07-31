from __future__ import annotations

import math
from dataclasses import dataclass

from .models import AttributeRef, Literal, Predicate


@dataclass(frozen=True)
class SortEstimate:
    cost: int | None
    initial_runs: int
    merge_passes: int | None
    formula: str
    reason: str = ""


def ceil_div(numerator: float, denominator: float) -> int:
    if numerator <= 0:
        return 0
    return math.ceil(numerator / denominator)


def relation_blocks(rows: float, row_width_bytes: int, block_payload_bytes: int) -> int:
    if rows <= 0 or row_width_bytes <= 0:
        return 0
    return math.ceil(rows * row_width_bytes / block_payload_bytes)


def predicate_selectivity(
    predicate: Predicate, distinct_values: dict[AttributeRef, float]
) -> float:
    left = predicate.left
    right = predicate.right
    operator = "!=" if predicate.operator == "<>" else predicate.operator

    if isinstance(left, AttributeRef) and isinstance(right, Literal):
        distinct = max(1.0, distinct_values.get(left, 1.0))
        if operator == "=":
            return 1.0 / distinct
        if operator == "!=":
            return max(0.0, 1.0 - 1.0 / distinct)
        return 1.0 / 3.0

    if isinstance(left, AttributeRef) and isinstance(right, AttributeRef):
        left_distinct = max(1.0, distinct_values.get(left, 1.0))
        right_distinct = max(1.0, distinct_values.get(right, 1.0))
        equality_selectivity = 1.0 / max(left_distinct, right_distinct)
        if operator == "=":
            return equality_selectivity
        if operator == "!=":
            return max(0.0, 1.0 - equality_selectivity)
        return 1.0 / 3.0

    return 1.0


def estimate_predicates(
    input_rows: float,
    distinct_values: dict[AttributeRef, float],
    predicates: tuple[Predicate, ...] | list[Predicate],
) -> tuple[float, dict[AttributeRef, float], float]:
    selectivity = 1.0
    for predicate in predicates:
        selectivity *= predicate_selectivity(predicate, distinct_values)
    selectivity = min(1.0, max(0.0, selectivity))

    if input_rows <= 0 or selectivity == 0:
        output_rows = 0.0
    else:
        output_rows = min(input_rows, max(1.0, input_rows * selectivity))

    output_distinct = dict(distinct_values)
    for predicate in predicates:
        left = predicate.left
        right = predicate.right
        normalized_operator = "!=" if predicate.operator == "<>" else predicate.operator
        if normalized_operator == "=" and isinstance(left, AttributeRef):
            if isinstance(right, Literal):
                output_distinct[left] = 0.0 if output_rows == 0 else 1.0
            elif isinstance(right, AttributeRef):
                shared = min(
                    output_distinct.get(left, output_rows),
                    output_distinct.get(right, output_rows),
                    output_rows,
                )
                output_distinct[left] = shared
                output_distinct[right] = shared

    for attribute, distinct in tuple(output_distinct.items()):
        if output_rows == 0:
            output_distinct[attribute] = 0.0
        else:
            output_distinct[attribute] = max(1.0, min(distinct, output_rows))
    return output_rows, output_distinct, selectivity


def external_sort_cost(blocks: int, buffer_blocks: int) -> SortEstimate:
    if blocks == 0:
        return SortEstimate(
            cost=0,
            initial_runs=0,
            merge_passes=0,
            formula="0 (prazan ulaz)",
        )

    if blocks <= buffer_blocks:
        return SortEstimate(
            cost=2 * blocks,
            initial_runs=1,
            merge_passes=0,
            formula=f"2 * {blocks}",
        )

    initial_runs = ceil_div(blocks, buffer_blocks) if buffer_blocks > 0 else blocks
    if buffer_blocks < 3:
        return SortEstimate(
            cost=None,
            initial_runs=initial_runs,
            merge_passes=None,
            formula="nije primenljivo",
            reason=(
                "Za spoljno objedinjeno sortiranje ulaza veceg od bafera "
                "potrebna su najmanje 3 bafer bloka."
            ),
        )

    merge_passes = 0
    remaining_runs = initial_runs
    fan_in = buffer_blocks - 1
    while remaining_runs > 1:
        remaining_runs = ceil_div(remaining_runs, fan_in)
        merge_passes += 1
    cost = 2 * blocks * (1 + merge_passes)
    return SortEstimate(
        cost=cost,
        initial_runs=initial_runs,
        merge_passes=merge_passes,
        formula=f"2 * {blocks} * (1 + {merge_passes})",
    )
