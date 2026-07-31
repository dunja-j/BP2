from __future__ import annotations

import math
from dataclasses import dataclass

from .costing import ceil_div, estimate_predicates, predicate_selectivity
from .models import (
    AttributeRef,
    FromItem,
    IndexStats,
    Literal,
    PlanAlternative,
    PlanNode,
    PlanState,
    Predicate,
    RelationStats,
    TableStats,
)


@dataclass(frozen=True)
class _AccessOption:
    algorithm: str
    cost: int
    sorted_on: AttributeRef | None
    details: dict[str, object]


class AccessPathPlanner:
    def __init__(self, table_by_alias: dict[str, TableStats]) -> None:
        self.table_by_alias = table_by_alias

    def logical_stats(
        self, item: FromItem, predicates: tuple[Predicate, ...]
    ) -> RelationStats:
        table = self.table_by_alias[item.alias]
        widths = {
            AttributeRef(item.alias, attribute.name): attribute.width_bytes
            for attribute in table.attributes
        }
        distinct = {
            AttributeRef(item.alias, attribute.name): float(attribute.distinct_values)
            for attribute in table.attributes
        }
        rows, filtered_distinct, _ = estimate_predicates(
            float(table.row_count), distinct, predicates
        )
        blocks = ceil_div(rows, table.rows_per_block)
        return RelationStats(
            tables=frozenset((item.alias,)),
            rows=rows,
            blocks=blocks,
            column_widths=widths,
            distinct_values=filtered_distinct,
        )

    def plan(
        self, item: FromItem, predicates: tuple[Predicate, ...]
    ) -> list[PlanState]:
        table = self.table_by_alias[item.alias]
        logical_stats = self.logical_stats(item, predicates)
        physical_order = self._physical_order(table, item.alias)

        if not predicates:
            return self._base_relation_states(table, logical_stats, physical_order)

        options = [
            self._sequential_option(table, logical_stats, predicates, physical_order)
        ]
        options.extend(
            self._index_options(table, item.alias, logical_stats, predicates)
        )
        alternatives = [
            PlanAlternative(
                algorithm=option.algorithm,
                cost=option.cost,
                note=str(option.details.get("formula", "")),
            )
            for option in sorted(options, key=lambda option: option.cost)
        ]

        states: list[PlanState] = []
        for option in options:
            directions: tuple[str | None, ...]
            if option.sorted_on is None:
                directions = (None,)
            else:
                directions = ("ASC", "DESC")
            for direction in directions:
                details = dict(option.details)
                details.update(
                    {
                        "predicates": [str(predicate) for predicate in predicates],
                        "selectivity": round(
                            logical_stats.rows / table.row_count
                            if table.row_count > 0
                            else 0.0,
                            8,
                        ),
                        "materialized_output_blocks": logical_stats.blocks,
                    }
                )
                if direction is not None:
                    details["scan_direction"] = direction
                node = PlanNode(
                    operation="SELECTION",
                    algorithm=option.algorithm,
                    relations=(item.alias,),
                    estimated_rows=logical_stats.rows,
                    estimated_blocks=logical_stats.blocks,
                    operation_cost=option.cost,
                    details=details,
                    alternatives=list(alternatives),
                )
                stats = RelationStats(
                    tables=logical_stats.tables,
                    rows=logical_stats.rows,
                    blocks=logical_stats.blocks,
                    column_widths=logical_stats.column_widths,
                    distinct_values=logical_stats.distinct_values,
                    sorted_on=option.sorted_on,
                    sort_direction=direction,
                )
                states.append(PlanState(stats=stats, cost=option.cost, node=node))
        return self._prune_by_order(states)

    def _base_relation_states(
        self,
        table: TableStats,
        logical_stats: RelationStats,
        physical_order: AttributeRef | None,
    ) -> list[PlanState]:
        directions: tuple[str | None, ...] = (
            (None,) if physical_order is None else ("ASC", "DESC")
        )
        states: list[PlanState] = []
        for direction in directions:
            details: dict[str, object] = {
                "table_rows": table.row_count,
                "table_blocks": table.block_count,
                "note": "Bazna relacija je vec materijalizovana na disku.",
            }
            if physical_order is not None:
                details["physical_order"] = str(physical_order)
                details["scan_direction"] = direction
            node = PlanNode(
                operation="BASE RELATION",
                algorithm="Existing disk relation",
                relations=tuple(logical_stats.tables),
                estimated_rows=logical_stats.rows,
                estimated_blocks=logical_stats.blocks,
                operation_cost=0,
                details=details,
            )
            stats = RelationStats(
                tables=logical_stats.tables,
                rows=logical_stats.rows,
                blocks=logical_stats.blocks,
                column_widths=logical_stats.column_widths,
                distinct_values=logical_stats.distinct_values,
                sorted_on=physical_order,
                sort_direction=direction,
            )
            states.append(PlanState(stats=stats, cost=0, node=node))
        return states

    def _sequential_option(
        self,
        table: TableStats,
        output: RelationStats,
        predicates: tuple[Predicate, ...],
        physical_order: AttributeRef | None,
    ) -> _AccessOption:
        cost = table.block_count + output.blocks
        predicate_word = "conjunctive " if len(predicates) > 1 else ""
        return _AccessOption(
            algorithm=f"Sequential scan with {predicate_word}filter",
            cost=cost,
            sorted_on=physical_order,
            details={
                "input_read_blocks": table.block_count,
                "formula": f"{table.block_count} + {output.blocks}",
            },
        )

    def _index_options(
        self,
        table: TableStats,
        alias: str,
        output: RelationStats,
        predicates: tuple[Predicate, ...],
    ) -> list[_AccessOption]:
        options: list[_AccessOption] = []
        base_distinct = {
            AttributeRef(alias, attribute.name): float(attribute.distinct_values)
            for attribute in table.attributes
        }
        for index in table.indexes:
            used_predicates = self._usable_predicates(index, alias, predicates)
            if used_predicates is None:
                continue
            index_selectivity = math.prod(
                predicate_selectivity(predicate, base_distinct)
                for predicate in used_predicates
            )
            if table.row_count == 0 or index_selectivity == 0:
                fetched_rows = 0.0
            else:
                fetched_rows = min(
                    float(table.row_count),
                    max(1.0, table.row_count * index_selectivity),
                )
            fetched_blocks = self._index_data_fetch_blocks(table, index, fetched_rows)
            traversal_cost = index.height if index.kind == "btree" else 1
            assert traversal_cost is not None
            cost = traversal_cost + fetched_blocks + output.blocks
            used_set = set(used_predicates)
            residual = [
                predicate for predicate in predicates if predicate not in used_set
            ]
            index_label = "B+ tree" if index.kind == "btree" else "Hash"
            sorted_on = None
            if index.kind == "btree":
                leading_name = index.attributes[0]
                canonical_name = table.attribute_map[leading_name.casefold()].name
                sorted_on = AttributeRef(alias, canonical_name)
            options.append(
                _AccessOption(
                    algorithm=f"{index_label} index scan ({index.name})",
                    cost=cost,
                    sorted_on=sorted_on,
                    details={
                        "index": index.name,
                        "index_type": index.kind,
                        "clustered": index.clustered,
                        "index_predicates": [
                            str(predicate) for predicate in used_predicates
                        ],
                        "residual_predicates": [
                            str(predicate) for predicate in residual
                        ],
                        "estimated_index_rows": round(fetched_rows, 2),
                        "index_traversal_blocks": traversal_cost,
                        "data_fetch_blocks": fetched_blocks,
                        "formula": (
                            f"{traversal_cost} + {fetched_blocks} + {output.blocks}"
                        ),
                    },
                )
            )
        return options

    def _usable_predicates(
        self,
        index: IndexStats,
        alias: str,
        predicates: tuple[Predicate, ...],
    ) -> tuple[Predicate, ...] | None:
        by_attribute: dict[str, list[Predicate]] = {}
        for predicate in predicates:
            if not isinstance(predicate.left, AttributeRef):
                continue
            if predicate.left.table != alias or not isinstance(
                predicate.right, Literal
            ):
                continue
            by_attribute.setdefault(predicate.left.name.casefold(), []).append(
                predicate
            )

        if index.kind == "hash":
            matched: list[Predicate] = []
            for attribute_name in index.attributes:
                equality = next(
                    (
                        predicate
                        for predicate in by_attribute.get(attribute_name.casefold(), [])
                        if predicate.operator == "="
                    ),
                    None,
                )
                if equality is None:
                    return None
                matched.append(equality)
            return tuple(matched)

        matched = []
        for attribute_name in index.attributes:
            candidates = by_attribute.get(attribute_name.casefold(), [])
            equality = next(
                (predicate for predicate in candidates if predicate.operator == "="),
                None,
            )
            if equality is not None:
                matched.append(equality)
                continue
            range_predicate = next(
                (
                    predicate
                    for predicate in candidates
                    if predicate.operator in {"<", "<=", ">", ">="}
                ),
                None,
            )
            if range_predicate is not None:
                matched.append(range_predicate)
            break
        return tuple(matched) if matched else None

    @staticmethod
    def _index_data_fetch_blocks(
        table: TableStats, index: IndexStats, fetched_rows: float
    ) -> int:
        if fetched_rows <= 0:
            return 0
        if index.clustered:
            return ceil_div(fetched_rows, table.rows_per_block)
        return min(table.block_count, math.ceil(fetched_rows))

    @staticmethod
    def _physical_order(table: TableStats, alias: str) -> AttributeRef | None:
        clustered_btree = next(
            (
                index
                for index in table.indexes
                if index.kind == "btree" and index.clustered
            ),
            None,
        )
        if clustered_btree is None:
            return None
        leading_name = clustered_btree.attributes[0]
        canonical_name = table.attribute_map[leading_name.casefold()].name
        return AttributeRef(alias, canonical_name)

    @staticmethod
    def _prune_by_order(states: list[PlanState]) -> list[PlanState]:
        best: dict[tuple[AttributeRef | None, str | None], PlanState] = {}
        for state in states:
            key = (state.stats.sorted_on, state.stats.sort_direction)
            current = best.get(key)
            if current is None or state.cost < current.cost:
                best[key] = state
        return list(best.values())
