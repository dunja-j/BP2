from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, replace

from .access_paths import AccessPathPlanner
from .costing import (
    SortEstimate,
    estimate_predicates,
    external_sort_cost,
    predicate_selectivity,
    relation_blocks,
)
from .errors import OptimizationError
from .models import (
    AttributeRef,
    EstimationResult,
    EstimatorInput,
    IndexStats,
    Literal,
    ParsedQuery,
    PlanAlternative,
    PlanNode,
    PlanState,
    Predicate,
    RelationStats,
    TableStats,
)

ASSUMPTIONS = (
    "Cena je broj citanja i upisa disk blokova; CPU cena i kesiranje se zanemaruju.",
    "Svaki logicki operator materijalizuje rezultat, osim indeksnih proba unutar index nested-loop join-a.",
    "Vrednosti su uniformno raspodeljene, a konjunktivni uslovi su statisticki nezavisni.",
    "Za A = konstanta koristi se selektivnost 1/V(A), a za poredjenje opsega 1/3.",
    "Za A = B koristi se 1/max(V(A), V(B)); integritet referenci i korelacije nisu poznati.",
    "Hash algoritmi zanemaruju particionu neuravnotezenost; B+ treeHeight obuhvata put do lista.",
    "Za tipove bez zadate sirine koristi se STRING=32, DOUBLE=8, INT/DATE=4 bajta; velicina bloka se procenjuje iz rowsPerBlock.",
)

_INVERTED_OPERATOR = {
    "=": "=",
    "!=": "!=",
    "<>": "<>",
    "<": ">",
    "<=": ">=",
    ">": "<",
    ">=": "<=",
}


@dataclass
class _JoinCandidate:
    algorithm: str
    operation_cost: int
    total_cost: int
    children: list[PlanNode]
    output_orders: tuple[tuple[AttributeRef, str], ...]
    details: dict[str, object]
    alternative_cost: int | None
    alternative_note: str


class QueryOptimizer:
    def __init__(self, estimator_input: EstimatorInput, query: ParsedQuery) -> None:
        self.input = estimator_input
        self.query = query
        self.table_by_alias = {
            item.alias: estimator_input.table_map[item.table_name.casefold()]
            for item in query.from_items
        }
        self.local_predicates: dict[str, tuple[Predicate, ...]] = {
            item.alias: tuple(
                predicate
                for predicate in query.predicates
                if predicate.referenced_tables == frozenset((item.alias,))
            )
            for item in query.from_items
        }
        self.join_predicates = tuple(
            predicate
            for predicate in query.predicates
            if len(predicate.referenced_tables) == 2
        )
        self.logical_singletons: dict[str, RelationStats] = {}
        self.logical_subsets: dict[frozenset[str], RelationStats] = {}

    def optimize(self) -> EstimationResult:
        access_planner = AccessPathPlanner(self.table_by_alias)
        plans: dict[frozenset[str], list[PlanState]] = {}
        aliases = tuple(item.alias for item in self.query.from_items)

        for item in self.query.from_items:
            predicates = self.local_predicates[item.alias]
            self.logical_singletons[item.alias] = access_planner.logical_stats(
                item, predicates
            )
            self.logical_subsets[frozenset((item.alias,))] = self.logical_singletons[
                item.alias
            ]
            plans[frozenset((item.alias,))] = access_planner.plan(item, predicates)

        for size in range(2, len(aliases) + 1):
            for subset_tuple in itertools.combinations(aliases, size):
                subset = frozenset(subset_tuple)
                candidates: list[PlanState] = []
                anchor = subset_tuple[0]
                remaining = subset_tuple[1:]
                for left_extra_size in range(len(remaining) + 1):
                    for left_extra in itertools.combinations(
                        remaining, left_extra_size
                    ):
                        left_tables = frozenset((anchor, *left_extra))
                        if left_tables == subset:
                            continue
                        right_tables = subset - left_tables
                        crossing = tuple(
                            predicate
                            for predicate in self.join_predicates
                            if predicate.referenced_tables <= subset
                            and predicate.referenced_tables & left_tables
                            and predicate.referenced_tables & right_tables
                        )
                        for left in plans[left_tables]:
                            for right in plans[right_tables]:
                                candidates.extend(
                                    self._join_states(left, right, crossing)
                                )
                if not candidates:
                    raise OptimizationError(
                        f"Nije moguce napraviti plan za tabele: {', '.join(subset_tuple)}. "
                        "Proverite velicinu bafera i podrzane algoritme."
                    )
                plans[subset] = self._prune_states(candidates)

        complete_states = plans[frozenset(aliases)]
        final_states = [self._finish_plan(state) for state in complete_states]
        feasible_states = [state for state in final_states if state is not None]
        if not feasible_states:
            raise OptimizationError(
                "ORDER BY nije moguce izvrsiti sa zadatom velicinom bafera, "
                "a nijedan prethodni operator ne daje trazeni redosled."
            )
        best = min(feasible_states, key=lambda state: state.cost)
        warnings = self._warnings(aliases)
        return EstimationResult(
            query=self.input.query,
            buffer_blocks=self.input.buffer_blocks,
            root=best.node,
            assumptions=ASSUMPTIONS,
            warnings=warnings,
        )

    def _join_states(
        self,
        left: PlanState,
        right: PlanState,
        predicates: tuple[Predicate, ...],
    ) -> list[PlanState]:
        output = self._logical_stats_for_tables(left.stats.tables | right.stats.tables)
        candidates: list[_JoinCandidate] = []

        candidates.extend(self._nested_loop_candidates(left, right, output, predicates))
        candidates.extend(
            self._block_nested_loop_candidates(left, right, output, predicates)
        )
        candidates.extend(
            self._index_nested_loop_candidates(left, right, output, predicates)
        )
        candidates.extend(self._sort_merge_candidates(left, right, output, predicates))
        candidates.extend(self._hash_join_candidates(left, right, output, predicates))

        if not candidates:
            return []
        alternatives = [
            PlanAlternative(
                algorithm=candidate.algorithm,
                cost=candidate.alternative_cost,
                note=candidate.alternative_note,
            )
            for candidate in sorted(
                candidates,
                key=lambda candidate: (
                    candidate.alternative_cost is None,
                    candidate.alternative_cost or 0,
                    candidate.algorithm,
                ),
            )
        ]

        states: list[PlanState] = []
        for candidate in candidates:
            details = dict(candidate.details)
            details.update(
                {
                    "predicates": [str(predicate) for predicate in predicates],
                    "cartesian_product": not predicates,
                    "materialized_output_blocks": output.blocks,
                }
            )
            node = PlanNode(
                operation="JOIN",
                algorithm=candidate.algorithm,
                relations=tuple(sorted(output.tables)),
                estimated_rows=output.rows,
                estimated_blocks=output.blocks,
                operation_cost=candidate.operation_cost,
                children=candidate.children,
                details=details,
                alternatives=list(alternatives),
            )
            if candidate.output_orders:
                for attribute, direction in candidate.output_orders:
                    ordered_stats = replace(
                        output, sorted_on=attribute, sort_direction=direction
                    )
                    states.append(
                        PlanState(
                            stats=ordered_stats, cost=candidate.total_cost, node=node
                        )
                    )
            else:
                states.append(
                    PlanState(stats=output, cost=candidate.total_cost, node=node)
                )
        return states

    def _nested_loop_candidates(
        self,
        left: PlanState,
        right: PlanState,
        output: RelationStats,
        predicates: tuple[Predicate, ...],
    ) -> list[_JoinCandidate]:
        if self.input.buffer_blocks < 3:
            return []
        candidates = []
        for outer, inner in ((left, right), (right, left)):
            operation_cost = (
                outer.stats.blocks
                + math.ceil(outer.stats.rows) * inner.stats.blocks
                + output.blocks
            )
            outer_name = "+".join(sorted(outer.stats.tables))
            inner_name = "+".join(sorted(inner.stats.tables))
            candidates.append(
                _JoinCandidate(
                    algorithm=f"Tuple nested-loop join (outer: {outer_name})",
                    operation_cost=operation_cost,
                    total_cost=outer.cost + inner.cost + operation_cost,
                    children=[outer.node, inner.node],
                    output_orders=self._preserved_order(outer.stats),
                    details={
                        "outer": outer_name,
                        "inner": inner_name,
                        "formula": (
                            f"{outer.stats.blocks} + ceil({outer.stats.rows:.2f}) * "
                            f"{inner.stats.blocks} + {output.blocks}"
                        ),
                    },
                    alternative_cost=operation_cost,
                    alternative_note="Spoljasnja relacija se cita jednom; unutrasnja za svaki red.",
                )
            )
        return candidates

    def _block_nested_loop_candidates(
        self,
        left: PlanState,
        right: PlanState,
        output: RelationStats,
        predicates: tuple[Predicate, ...],
    ) -> list[_JoinCandidate]:
        if self.input.buffer_blocks < 3:
            return []
        usable_outer_buffers = self.input.buffer_blocks - 2
        candidates = []
        for outer, inner in ((left, right), (right, left)):
            chunks = math.ceil(outer.stats.blocks / usable_outer_buffers)
            operation_cost = (
                outer.stats.blocks + chunks * inner.stats.blocks + output.blocks
            )
            outer_name = "+".join(sorted(outer.stats.tables))
            inner_name = "+".join(sorted(inner.stats.tables))
            candidates.append(
                _JoinCandidate(
                    algorithm=f"Block nested-loop join (outer: {outer_name})",
                    operation_cost=operation_cost,
                    total_cost=outer.cost + inner.cost + operation_cost,
                    children=[outer.node, inner.node],
                    output_orders=self._preserved_order(outer.stats),
                    details={
                        "outer": outer_name,
                        "inner": inner_name,
                        "outer_chunks": chunks,
                        "usable_outer_buffers": usable_outer_buffers,
                        "formula": (
                            f"{outer.stats.blocks} + ceil({outer.stats.blocks} / "
                            f"{usable_outer_buffers}) * {inner.stats.blocks} + {output.blocks}"
                        ),
                    },
                    alternative_cost=operation_cost,
                    alternative_note=f"Koristi M-2 = {usable_outer_buffers} blokova za spoljasnju relaciju.",
                )
            )
        return candidates

    def _index_nested_loop_candidates(
        self,
        left: PlanState,
        right: PlanState,
        output: RelationStats,
        predicates: tuple[Predicate, ...],
    ) -> list[_JoinCandidate]:
        candidates: list[_JoinCandidate] = []
        for outer, inner in ((left, right), (right, left)):
            if len(inner.stats.tables) != 1:
                continue
            inner_alias = next(iter(inner.stats.tables))
            table = self.table_by_alias[inner_alias]
            for index, index_predicates in self._join_index_matches(
                table, inner_alias, outer.stats.tables, predicates
            ):
                traversal = index.height if index.kind == "btree" else 1
                assert traversal is not None
                selectivity = math.prod(
                    self._base_predicate_selectivity(predicate, inner_alias)
                    for predicate in index_predicates
                )
                fetched_rows_per_probe = table.row_count * selectivity
                if index.clustered:
                    data_blocks_per_probe = min(
                        float(table.block_count),
                        fetched_rows_per_probe / table.rows_per_block,
                    )
                else:
                    data_blocks_per_probe = min(
                        float(table.block_count), fetched_rows_per_probe
                    )
                probe_cost = math.ceil(
                    outer.stats.rows * (traversal + data_blocks_per_probe)
                )
                operation_cost = outer.stats.blocks + probe_cost + output.blocks
                outer_name = "+".join(sorted(outer.stats.tables))
                index_label = "B+ tree" if index.kind == "btree" else "hash"
                inner_leaf = self._indexed_inner_leaf(
                    inner_alias, index, index_predicates
                )
                candidates.append(
                    _JoinCandidate(
                        algorithm=(
                            f"Index nested-loop join ({index.name}, outer: {outer_name})"
                        ),
                        operation_cost=operation_cost,
                        total_cost=outer.cost + operation_cost,
                        children=[outer.node, inner_leaf],
                        output_orders=self._preserved_order(outer.stats),
                        details={
                            "outer": outer_name,
                            "inner": inner_alias,
                            "index": index.name,
                            "index_type": index_label,
                            "clustered": index.clustered,
                            "index_predicates": [
                                str(predicate) for predicate in index_predicates
                            ],
                            "estimated_rows_per_probe": round(
                                fetched_rows_per_probe, 4
                            ),
                            "estimated_data_blocks_per_probe": round(
                                data_blocks_per_probe, 4
                            ),
                            "formula": (
                                f"{outer.stats.blocks} + ceil({outer.stats.rows:.2f} * "
                                f"({traversal} + {data_blocks_per_probe:.4f})) + "
                                f"{output.blocks}"
                            ),
                        },
                        alternative_cost=operation_cost,
                        alternative_note=(
                            "Unutrasnja bazna tabela se ne materijalizuje; indeks se "
                            "proverava za svaki red spoljasnjeg rezultata."
                        ),
                    )
                )
        return candidates

    def _sort_merge_candidates(
        self,
        left: PlanState,
        right: PlanState,
        output: RelationStats,
        predicates: tuple[Predicate, ...],
    ) -> list[_JoinCandidate]:
        if self.input.buffer_blocks < 3:
            return []
        equality_pairs = self._equality_pairs(
            left.stats.tables, right.stats.tables, predicates
        )
        candidates: list[_JoinCandidate] = []
        for left_key, right_key in equality_pairs:
            for direction in ("ASC", "DESC"):
                sorted_left = self._ensure_sorted(left, left_key, direction)
                sorted_right = self._ensure_sorted(right, right_key, direction)
                if sorted_left is None or sorted_right is None:
                    continue
                merge_cost = left.stats.blocks + right.stats.blocks + output.blocks
                prerequisite_cost = (sorted_left.cost - left.cost) + (
                    sorted_right.cost - right.cost
                )
                local_path_cost = prerequisite_cost + merge_cost
                candidates.append(
                    _JoinCandidate(
                        algorithm=(
                            f"Sort-merge join ({left_key} = {right_key}, {direction})"
                        ),
                        operation_cost=merge_cost,
                        total_cost=sorted_left.cost + sorted_right.cost + merge_cost,
                        children=[sorted_left.node, sorted_right.node],
                        output_orders=((left_key, direction), (right_key, direction)),
                        details={
                            "left_key": str(left_key),
                            "right_key": str(right_key),
                            "direction": direction,
                            "merge_formula": (
                                f"{left.stats.blocks} + {right.stats.blocks} + {output.blocks}"
                            ),
                            "prerequisite_sort_cost": prerequisite_cost,
                        },
                        alternative_cost=local_path_cost,
                        alternative_note=(
                            "Cena ukljucuje potrebna eksterna sortiranja i jedan merge prolaz."
                        ),
                    )
                )
        return candidates

    def _hash_join_candidates(
        self,
        left: PlanState,
        right: PlanState,
        output: RelationStats,
        predicates: tuple[Predicate, ...],
    ) -> list[_JoinCandidate]:
        if self.input.buffer_blocks < 3 or not self._equality_pairs(
            left.stats.tables, right.stats.tables, predicates
        ):
            return []
        build, probe = (
            (left, right) if left.stats.blocks <= right.stats.blocks else (right, left)
        )
        buffer_blocks = self.input.buffer_blocks
        if build.stats.blocks <= buffer_blocks - 2:
            algorithm = "One-pass hash join"
            operation_cost = left.stats.blocks + right.stats.blocks + output.blocks
            formula = f"{left.stats.blocks} + {right.stats.blocks} + {output.blocks}"
            note = "Manja relacija staje u M-2 bafer blokova."
        elif build.stats.blocks <= (buffer_blocks - 1) * (buffer_blocks - 2):
            algorithm = "Grace hash join"
            operation_cost = (
                3 * (left.stats.blocks + right.stats.blocks) + output.blocks
            )
            formula = (
                f"3 * ({left.stats.blocks} + {right.stats.blocks}) + {output.blocks}"
            )
            note = "Dva particiona prolaza: particionisanje, zatim build/probe."
        else:
            return []
        build_name = "+".join(sorted(build.stats.tables))
        probe_name = "+".join(sorted(probe.stats.tables))
        return [
            _JoinCandidate(
                algorithm=f"{algorithm} (build: {build_name})",
                operation_cost=operation_cost,
                total_cost=left.cost + right.cost + operation_cost,
                children=[left.node, right.node],
                output_orders=(),
                details={
                    "build": build_name,
                    "probe": probe_name,
                    "formula": formula,
                },
                alternative_cost=operation_cost,
                alternative_note=note,
            )
        ]

    def _logical_stats_for_tables(self, tables: frozenset[str]) -> RelationStats:
        cached = self.logical_subsets.get(tables)
        if cached is not None:
            return cached

        singletons = [self.logical_singletons[alias] for alias in sorted(tables)]
        widths = {
            attribute: width
            for singleton in singletons
            for attribute, width in singleton.column_widths.items()
        }
        distinct = {
            attribute: value
            for singleton in singletons
            for attribute, value in singleton.distinct_values.items()
        }
        cartesian_rows = math.prod(singleton.rows for singleton in singletons)
        predicates = tuple(
            predicate
            for predicate in self.join_predicates
            if predicate.referenced_tables <= tables
        )
        rows, output_distinct, _ = estimate_predicates(
            cartesian_rows, distinct, predicates
        )
        blocks = relation_blocks(
            rows,
            sum(widths.values()),
            self.input.block_payload_bytes,
        )
        stats = RelationStats(
            tables=tables,
            rows=rows,
            blocks=blocks,
            column_widths=widths,
            distinct_values=output_distinct,
        )
        self.logical_subsets[tables] = stats
        return stats

    def _finish_plan(self, state: PlanState) -> PlanState | None:
        order = self.query.order_by
        selected_attributes = self.query.select
        selected_set = set(selected_attributes)

        if order is not None and order.attribute not in selected_set:
            ordered = self._apply_order_by(state, order.attribute, order.direction)
            if ordered is None:
                return None
            return self._apply_projection(ordered)

        projected = self._apply_projection(state)
        if order is None:
            return projected
        return self._apply_order_by(projected, order.attribute, order.direction)

    def _apply_projection(self, state: PlanState) -> PlanState:
        selected = self.query.select
        output_width = sum(
            state.stats.column_widths[attribute] for attribute in selected
        )
        output_blocks = relation_blocks(
            state.stats.rows, output_width, self.input.block_payload_bytes
        )
        operation_cost = state.stats.blocks + output_blocks
        distinct = {
            attribute: state.stats.distinct_values[attribute]
            for attribute in dict.fromkeys(selected)
        }
        widths = {
            attribute: state.stats.column_widths[attribute]
            for attribute in dict.fromkeys(selected)
        }
        preserves_sort = state.stats.sorted_on in selected
        output_stats = RelationStats(
            tables=state.stats.tables,
            rows=state.stats.rows,
            blocks=output_blocks,
            column_widths=widths,
            distinct_values=distinct,
            sorted_on=state.stats.sorted_on if preserves_sort else None,
            sort_direction=state.stats.sort_direction if preserves_sort else None,
        )
        node = PlanNode(
            operation="PROJECTION",
            algorithm="Sequential projection with materialization",
            relations=tuple(sorted(state.stats.tables)),
            estimated_rows=state.stats.rows,
            estimated_blocks=output_blocks,
            operation_cost=operation_cost,
            children=[state.node],
            details={
                "attributes": [str(attribute) for attribute in selected],
                "duplicates_eliminated": False,
                "input_row_width_bytes": sum(state.stats.column_widths.values()),
                "output_row_width_bytes": output_width,
                "formula": f"{state.stats.blocks} + {output_blocks}",
            },
            alternatives=[
                PlanAlternative(
                    algorithm="Sequential projection with materialization",
                    cost=operation_cost,
                )
            ],
        )
        return PlanState(
            stats=output_stats, cost=state.cost + operation_cost, node=node
        )

    def _apply_order_by(
        self, state: PlanState, attribute: AttributeRef, direction: str
    ) -> PlanState | None:
        if (
            state.stats.sorted_on == attribute
            and state.stats.sort_direction == direction
        ):
            node = PlanNode(
                operation="ORDER BY",
                algorithm="Reuse existing order",
                relations=tuple(sorted(state.stats.tables)),
                estimated_rows=state.stats.rows,
                estimated_blocks=state.stats.blocks,
                operation_cost=0,
                children=[state.node],
                details={
                    "attribute": str(attribute),
                    "direction": direction,
                    "formula": "0",
                    "note": "Ulaz je vec u trazenom redosledu.",
                },
                alternatives=[
                    PlanAlternative(algorithm="Reuse existing order", cost=0)
                ],
            )
            return PlanState(stats=state.stats, cost=state.cost, node=node)

        sorted_state = self._ensure_sorted(
            state, attribute, direction, operation="ORDER BY"
        )
        return sorted_state

    def _ensure_sorted(
        self,
        state: PlanState,
        attribute: AttributeRef,
        direction: str,
        operation: str = "SORT",
    ) -> PlanState | None:
        if (
            state.stats.sorted_on == attribute
            and state.stats.sort_direction == direction
        ):
            return state
        estimate = external_sort_cost(state.stats.blocks, self.input.buffer_blocks)
        if estimate.cost is None:
            return None
        sorted_stats = replace(
            state.stats, sorted_on=attribute, sort_direction=direction
        )
        node = self._sort_node(
            state, sorted_stats, attribute, direction, estimate, operation
        )
        return PlanState(
            stats=sorted_stats,
            cost=state.cost + estimate.cost,
            node=node,
        )

    def _sort_node(
        self,
        state: PlanState,
        output: RelationStats,
        attribute: AttributeRef,
        direction: str,
        estimate: SortEstimate,
        operation: str,
    ) -> PlanNode:
        assert estimate.cost is not None
        return PlanNode(
            operation=operation,
            algorithm="External merge sort",
            relations=tuple(sorted(state.stats.tables)),
            estimated_rows=state.stats.rows,
            estimated_blocks=state.stats.blocks,
            operation_cost=estimate.cost,
            children=[state.node],
            details={
                "attribute": str(attribute),
                "direction": direction,
                "initial_runs": estimate.initial_runs,
                "merge_passes": estimate.merge_passes,
                "formula": estimate.formula,
            },
            alternatives=[
                PlanAlternative(algorithm="External merge sort", cost=estimate.cost)
            ],
        )

    def _join_index_matches(
        self,
        table: TableStats,
        inner_alias: str,
        outer_tables: frozenset[str],
        crossing_predicates: tuple[Predicate, ...],
    ) -> list[tuple[IndexStats, tuple[Predicate, ...]]]:
        candidates = crossing_predicates + self.local_predicates[inner_alias]
        matches: list[tuple[IndexStats, tuple[Predicate, ...]]] = []
        for index in table.indexes:
            matched: list[Predicate] = []
            has_join_key = False
            for attribute_name in index.attributes:
                attribute_predicates = [
                    oriented
                    for predicate in candidates
                    if (
                        oriented := self._orient_predicate_for_inner(
                            predicate, inner_alias, attribute_name, outer_tables
                        )
                    )
                    is not None
                ]
                equality = next(
                    (
                        predicate
                        for predicate in attribute_predicates
                        if predicate.operator == "="
                    ),
                    None,
                )
                if equality is not None:
                    matched.append(equality)
                    has_join_key = has_join_key or len(equality.referenced_tables) == 2
                    continue
                if index.kind == "btree":
                    range_predicate = next(
                        (
                            predicate
                            for predicate in attribute_predicates
                            if predicate.operator in {"<", "<=", ">", ">="}
                        ),
                        None,
                    )
                    if range_predicate is not None:
                        matched.append(range_predicate)
                        has_join_key = (
                            has_join_key or len(range_predicate.referenced_tables) == 2
                        )
                break
            complete_hash_key = index.kind != "hash" or len(matched) == len(
                index.attributes
            )
            if matched and has_join_key and complete_hash_key:
                matches.append((index, tuple(matched)))
        return matches

    @staticmethod
    def _orient_predicate_for_inner(
        predicate: Predicate,
        inner_alias: str,
        attribute_name: str,
        outer_tables: frozenset[str],
    ) -> Predicate | None:
        left = predicate.left
        right = predicate.right
        if (
            isinstance(left, AttributeRef)
            and left.table == inner_alias
            and left.name.casefold() == attribute_name.casefold()
            and (
                isinstance(right, Literal)
                or (isinstance(right, AttributeRef) and right.table in outer_tables)
            )
        ):
            return predicate
        if (
            isinstance(right, AttributeRef)
            and right.table == inner_alias
            and right.name.casefold() == attribute_name.casefold()
            and isinstance(left, AttributeRef)
            and left.table in outer_tables
        ):
            return Predicate(
                left=right,
                operator=_INVERTED_OPERATOR[predicate.operator],
                right=left,
            )
        return None

    def _base_predicate_selectivity(self, predicate: Predicate, alias: str) -> float:
        table = self.table_by_alias[alias]
        distinct = {
            AttributeRef(alias, attribute.name): float(attribute.distinct_values)
            for attribute in table.attributes
        }
        return predicate_selectivity(predicate, distinct)

    def _indexed_inner_leaf(
        self,
        alias: str,
        index: IndexStats,
        index_predicates: tuple[Predicate, ...],
    ) -> PlanNode:
        logical = self.logical_singletons[alias]
        return PlanNode(
            operation="INDEXED INNER INPUT",
            algorithm="On-demand base table lookup",
            relations=(alias,),
            estimated_rows=logical.rows,
            estimated_blocks=logical.blocks,
            operation_cost=0,
            details={
                "index": index.name,
                "index_predicates": [str(predicate) for predicate in index_predicates],
                "local_predicates": [
                    str(predicate) for predicate in self.local_predicates[alias]
                ],
                "note": "Trosak proba je uracunat u roditeljski index nested-loop join.",
            },
        )

    @staticmethod
    def _equality_pairs(
        left_tables: frozenset[str],
        right_tables: frozenset[str],
        predicates: tuple[Predicate, ...],
    ) -> tuple[tuple[AttributeRef, AttributeRef], ...]:
        pairs: list[tuple[AttributeRef, AttributeRef]] = []
        for predicate in predicates:
            if (
                predicate.operator != "="
                or not isinstance(predicate.left, AttributeRef)
                or not isinstance(predicate.right, AttributeRef)
            ):
                continue
            if (
                predicate.left.table in left_tables
                and predicate.right.table in right_tables
            ):
                pairs.append((predicate.left, predicate.right))
            elif (
                predicate.right.table in left_tables
                and predicate.left.table in right_tables
            ):
                pairs.append((predicate.right, predicate.left))
        return tuple(pairs)

    @staticmethod
    def _preserved_order(
        stats: RelationStats,
    ) -> tuple[tuple[AttributeRef, str], ...]:
        if stats.sorted_on is None or stats.sort_direction is None:
            return ()
        return ((stats.sorted_on, stats.sort_direction),)

    @staticmethod
    def _prune_states(states: list[PlanState]) -> list[PlanState]:
        best: dict[tuple[AttributeRef | None, str | None], PlanState] = {}
        for state in states:
            key = (state.stats.sorted_on, state.stats.sort_direction)
            current = best.get(key)
            if current is None or state.cost < current.cost:
                best[key] = state
        return list(best.values())

    def _warnings(self, aliases: tuple[str, ...]) -> tuple[str, ...]:
        if len(aliases) < 2:
            return ()
        connected = {aliases[0]}
        changed = True
        while changed:
            changed = False
            for predicate in self.join_predicates:
                if (
                    predicate.referenced_tables & connected
                    and not predicate.referenced_tables <= connected
                ):
                    connected.update(predicate.referenced_tables)
                    changed = True
        if connected != set(aliases):
            disconnected = ", ".join(sorted(set(aliases) - connected))
            return (
                (
                    "Upit sadrzi Dekartov proizvod jer join graf nije povezan; "
                    f"nepovezane tabele: {disconnected}."
                ),
            )
        return ()


def optimize_query(
    estimator_input: EstimatorInput, query: ParsedQuery
) -> EstimationResult:
    return QueryOptimizer(estimator_input, query).optimize()
