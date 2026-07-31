from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AttributeStats:
    name: str
    data_type: str
    unique: bool
    distinct_values: int
    width_bytes: int


@dataclass(frozen=True)
class IndexStats:
    name: str
    attributes: tuple[str, ...]
    kind: str
    clustered: bool
    height: int | None = None


@dataclass(frozen=True)
class TableStats:
    name: str
    row_count: int
    block_count: int
    rows_per_block: int
    attributes: tuple[AttributeStats, ...]
    indexes: tuple[IndexStats, ...] = ()

    @property
    def attribute_map(self) -> dict[str, AttributeStats]:
        return {attribute.name.casefold(): attribute for attribute in self.attributes}

    @property
    def row_width_bytes(self) -> int:
        return sum(attribute.width_bytes for attribute in self.attributes)


@dataclass(frozen=True)
class EstimatorInput:
    query: str
    buffer_blocks: int
    tables: tuple[TableStats, ...]
    block_payload_bytes: int

    @property
    def table_map(self) -> dict[str, TableStats]:
        return {table.name.casefold(): table for table in self.tables}


@dataclass(frozen=True, order=True)
class AttributeRef:
    table: str
    name: str

    def __str__(self) -> str:
        return f"{self.table}.{self.name}"


@dataclass(frozen=True)
class Literal:
    value: Any
    raw: str

    def __str__(self) -> str:
        return self.raw


Operand = AttributeRef | Literal


@dataclass(frozen=True)
class Predicate:
    left: Operand
    operator: str
    right: Operand

    @property
    def referenced_tables(self) -> frozenset[str]:
        tables: set[str] = set()
        if isinstance(self.left, AttributeRef):
            tables.add(self.left.table)
        if isinstance(self.right, AttributeRef):
            tables.add(self.right.table)
        return frozenset(tables)

    def __str__(self) -> str:
        return f"{self.left} {self.operator} {self.right}"


@dataclass(frozen=True)
class FromItem:
    table_name: str
    alias: str


@dataclass(frozen=True)
class OrderSpec:
    attribute: AttributeRef
    direction: str = "ASC"


@dataclass(frozen=True)
class ParsedQuery:
    select: tuple[AttributeRef, ...]
    from_items: tuple[FromItem, ...]
    predicates: tuple[Predicate, ...]
    order_by: OrderSpec | None = None


@dataclass(frozen=True)
class RelationStats:
    tables: frozenset[str]
    rows: float
    blocks: int
    column_widths: dict[AttributeRef, int]
    distinct_values: dict[AttributeRef, float]
    sorted_on: AttributeRef | None = None
    sort_direction: str | None = None


@dataclass
class PlanAlternative:
    algorithm: str
    cost: int | None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"algorithm": self.algorithm, "cost": self.cost}
        if self.note:
            result["note"] = self.note
        return result


@dataclass
class PlanNode:
    operation: str
    algorithm: str
    relations: tuple[str, ...]
    estimated_rows: float
    estimated_blocks: int
    operation_cost: int
    children: list[PlanNode] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    alternatives: list[PlanAlternative] = field(default_factory=list)

    @property
    def cumulative_cost(self) -> int:
        return self.operation_cost + sum(
            child.cumulative_cost for child in self.children
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "algorithm": self.algorithm,
            "relations": list(self.relations),
            "estimated_rows": round(self.estimated_rows, 2),
            "estimated_blocks": self.estimated_blocks,
            "operation_cost": self.operation_cost,
            "cumulative_cost": self.cumulative_cost,
            "details": self.details,
            "alternatives": [
                alternative.to_dict() for alternative in self.alternatives
            ],
            "children": [child.to_dict() for child in self.children],
        }


@dataclass(frozen=True)
class PlanState:
    stats: RelationStats
    cost: int
    node: PlanNode


@dataclass(frozen=True)
class EstimationResult:
    query: str
    buffer_blocks: int
    root: PlanNode
    assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "buffer_blocks": self.buffer_blocks,
            "estimated_total_cost": self.root.cumulative_cost,
            "estimated_rows": round(self.root.estimated_rows, 2),
            "estimated_blocks": self.root.estimated_blocks,
            "assumptions": list(self.assumptions),
            "warnings": list(self.warnings),
            "plan": self.root.to_dict(),
        }
