from __future__ import annotations

from .models import EstimationResult, PlanNode

_INTERNAL_INPUT_NODES = {"BASE RELATION", "INDEXED INNER INPUT"}


def render_text(result: EstimationResult) -> str:
    lines = [
        "NAJBOLJI PLAN EVALUACIJE",
        "=" * 40,
    ]

    operations = [
        node
        for node in _postorder(result.root)
        if node.operation not in _INTERNAL_INPUT_NODES
    ]
    for number, node in enumerate(operations, start=1):
        relations = ", ".join(node.relations)
        lines.extend(
            (
                f"{number}. {node.operation} ({relations})",
                f"   Algoritam: {node.algorithm}",
                f"   Cena: {node.operation_cost} blok transfera",
                "",
            )
        )
    lines.append(f"UKUPNA CENA PLANA: {result.root.cumulative_cost} blok transfera")
    return "\n".join(lines)


def _postorder(node: PlanNode) -> list[PlanNode]:
    ordered: list[PlanNode] = []
    for child in node.children:
        ordered.extend(_postorder(child))
    ordered.append(node)
    return ordered
