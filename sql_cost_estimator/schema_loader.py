from __future__ import annotations

import json
from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path
from statistics import median
from typing import Any

from jsonschema import Draft202012Validator

from .errors import InputValidationError
from .models import AttributeStats, EstimatorInput, IndexStats, TableStats

_TYPE_WIDTHS = {
    "STRING": 32,
    "DOUBLE": 8,
    "INT": 4,
    "DATE": 4,
}
_INDEX_KINDS = {"B_PLUS_TREE": "btree", "HASH": "hash"}
_INPUT_SCHEMA = json.loads(
    files("sql_cost_estimator")
    .joinpath("input.schema.json")
    .read_text(encoding="utf-8")
)
_INPUT_VALIDATOR = Draft202012Validator(_INPUT_SCHEMA)


def load_input(path: str | Path, query: str | None = None) -> EstimatorInput:
    input_path = Path(path)
    try:
        with input_path.open("r", encoding="utf-8") as input_file:
            raw_data = json.load(input_file)
    except FileNotFoundError as error:
        raise InputValidationError(f"Ulazni fajl ne postoji: {input_path}") from error
    except json.JSONDecodeError as error:
        raise InputValidationError(
            f"Neispravan JSON u {input_path}, red {error.lineno}, kolona {error.colno}: {error.msg}"
        ) from error
    except OSError as error:
        raise InputValidationError(
            f"Ulazni fajl nije moguce procitati: {error}"
        ) from error
    return parse_input(raw_data, query=query)


def parse_input(raw_data: Any, query: str | None = None) -> EstimatorInput:
    resolved_query = _require_query(query)
    root = _validate_structure(raw_data)
    buffer_blocks = root["bufferBlocks"]
    raw_tables = root["schema"]["tables"]

    tables = tuple(
        _parse_table(raw_table, index) for index, raw_table in enumerate(raw_tables)
    )
    _ensure_unique(
        (table.name for table in tables), "Nazivi tabela moraju biti jedinstveni"
    )

    payload_estimates = [
        table.row_width_bytes * table.rows_per_block for table in tables
    ]
    block_payload_bytes = max(1, round(median(payload_estimates)))

    return EstimatorInput(
        query=resolved_query,
        buffer_blocks=buffer_blocks,
        tables=tables,
        block_payload_bytes=block_payload_bytes,
    )


def _parse_table(raw_table: dict[str, Any], position: int) -> TableStats:
    context = f"schema.tables[{position}]"
    name = raw_table["name"]
    row_count = raw_table["rowCount"]
    block_count = raw_table["blockCount"]
    rows_per_block = raw_table["rowsPerBlock"]

    if row_count > 0 and block_count == 0:
        raise InputValidationError(
            f"{context}.blockCount mora biti pozitivan za nepraznu tabelu."
        )
    if row_count == 0 and block_count != 0:
        raise InputValidationError(
            f"{context}.blockCount mora biti 0 za praznu tabelu."
        )

    attributes = tuple(
        _parse_attribute(raw_attribute, attribute_index, context, row_count)
        for attribute_index, raw_attribute in enumerate(raw_table["attributes"])
    )
    _ensure_unique(
        (attribute.name for attribute in attributes),
        f"Nazivi atributa tabele '{name}' moraju biti jedinstveni",
    )

    attribute_names = {attribute.name.casefold() for attribute in attributes}
    indexes = tuple(
        _parse_index(raw_index, index, context, attribute_names)
        for index, raw_index in enumerate(raw_table["indexes"])
    )
    _ensure_unique(
        (index.name for index in indexes),
        f"Nazivi indeksa tabele '{name}' moraju biti jedinstveni",
    )

    return TableStats(
        name=name,
        row_count=row_count,
        block_count=block_count,
        rows_per_block=rows_per_block,
        attributes=attributes,
        indexes=indexes,
    )


def _parse_attribute(
    raw_attribute: dict[str, Any],
    position: int,
    table_context: str,
    row_count: int,
) -> AttributeStats:
    context = f"{table_context}.attributes[{position}]"
    name = raw_attribute["name"]
    data_type = raw_attribute["type"]
    unique = raw_attribute["unique"]
    distinct_values = raw_attribute["distinctValues"]

    if distinct_values > row_count:
        raise InputValidationError(
            f"{context}.distinctValues ne sme biti veci od broja redova ({row_count})."
        )
    if row_count > 0 and distinct_values == 0:
        raise InputValidationError(
            f"{context}.distinctValues mora biti pozitivan za nepraznu tabelu."
        )
    if unique and distinct_values != row_count:
        raise InputValidationError(
            f"{context} je unique, pa distinctValues mora biti jednak rowCount ({row_count})."
        )

    return AttributeStats(
        name=name,
        data_type=data_type,
        unique=unique,
        distinct_values=distinct_values,
        width_bytes=_TYPE_WIDTHS[data_type],
    )


def _parse_index(
    raw_index: dict[str, Any],
    position: int,
    table_context: str,
    attribute_names: set[str],
) -> IndexStats:
    context = f"{table_context}.indexes[{position}]"
    attributes = tuple(raw_index["attributes"])
    unknown_attributes = [
        name for name in attributes if name.casefold() not in attribute_names
    ]
    if unknown_attributes:
        raise InputValidationError(
            f"{context} sadrzi nepoznate atribute: {', '.join(unknown_attributes)}."
        )

    return IndexStats(
        name=raw_index["name"],
        attributes=attributes,
        kind=_INDEX_KINDS[raw_index["type"]],
        clustered=raw_index["clustered"],
        height=raw_index.get("treeHeight"),
    )


def _validate_structure(raw_data: Any) -> dict[str, Any]:
    errors = sorted(
        _INPUT_VALIDATOR.iter_errors(raw_data),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.absolute_path)
        location = path or "koren dokumenta"
        raise InputValidationError(
            f"Ulaz ne odgovara zvanicnom JSON formatu ({location}): {error.message}"
        )
    return raw_data


def _require_query(query: str | None) -> str:
    if not isinstance(query, str) or not query.strip():
        raise InputValidationError(
            "SQL upit nije deo datog JSON formata. Prosledite ga opcijom "
            "--query ili argumentom query programskog API-ja."
        )
    return query.strip()


def _ensure_unique(values: Iterable[str], message: str) -> None:
    seen: set[str] = set()
    for value in values:
        folded = value.casefold()
        if folded in seen:
            raise InputValidationError(f"{message}: '{value}'.")
        seen.add(folded)
