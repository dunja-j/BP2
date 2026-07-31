from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import SQLParseError
from .models import (
    AttributeRef,
    FromItem,
    Literal,
    Operand,
    OrderSpec,
    ParsedQuery,
    Predicate,
    TableStats,
)

_TOKEN_PATTERN = re.compile(
    r"(?P<SPACE>\s+)"
    r"|(?P<STRING>'(?:''|[^'])*')"
    r"|(?P<NUMBER>[+-]?\d+(?:\.\d+)?)"
    r"|(?P<OP><=|>=|<>|!=|=|<|>)"
    r"|(?P<COMMA>,)"
    r"|(?P<DOT>\.)"
    r"|(?P<SEMICOLON>;)"
    r"|(?P<LPAREN>\()"
    r"|(?P<RPAREN>\))"
    r"|(?P<STAR>\*)"
    r"|(?P<IDENT>[A-Za-z_][A-Za-z0-9_$]*)"
    r"|(?P<MISMATCH>.)"
)

_RESERVED_AFTER_TABLE = {
    "WHERE",
    "ORDER",
    "GROUP",
    "JOIN",
    "INNER",
    "LEFT",
    "RIGHT",
    "FULL",
    "CROSS",
    "ON",
    "LIMIT",
    "HAVING",
    "UNION",
}

_INVERTED_OPERATOR = {
    "=": "=",
    "!=": "!=",
    "<>": "<>",
    "<": ">",
    "<=": ">=",
    ">": "<",
    ">=": "<=",
}


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    position: int


@dataclass(frozen=True)
class _RawRef:
    qualifier: str | None
    name: str


@dataclass(frozen=True)
class _RawPredicate:
    left: _RawRef | Literal
    operator: str
    right: _RawRef | Literal


@dataclass(frozen=True)
class _RawFromItem:
    table_name: str
    alias: str | None


def parse_sql(sql: str, tables: tuple[TableStats, ...]) -> ParsedQuery:
    return _Parser(sql, tables).parse()


class _Parser:
    def __init__(self, sql: str, tables: tuple[TableStats, ...]) -> None:
        if not sql.strip():
            raise SQLParseError("SQL upit ne sme biti prazan.")
        self.sql = sql
        self.tokens = self._tokenize(sql)
        self.position = 0
        self.tables = tables
        self.schema_tables = {table.name.casefold(): table for table in tables}

    def parse(self) -> ParsedQuery:
        self._expect_keyword("SELECT")
        raw_select = self._parse_reference_list("SELECT")
        self._expect_keyword("FROM")
        raw_from = self._parse_from_list()

        raw_predicates: list[_RawPredicate] = []
        if self._match_keyword("WHERE"):
            raw_predicates.append(self._parse_predicate())
            while self._match_keyword("AND"):
                raw_predicates.append(self._parse_predicate())
            if self._check_keyword("OR"):
                raise self._error(
                    "WHERE podrzava samo konjunkciju povezanu operatorom AND"
                )

        raw_order: tuple[_RawRef, str] | None = None
        if self._match_keyword("ORDER"):
            self._expect_keyword("BY")
            order_attribute = self._parse_reference()
            direction = "ASC"
            if self._match_keyword("ASC"):
                direction = "ASC"
            elif self._match_keyword("DESC"):
                direction = "DESC"
            if self._peek().kind == "COMMA":
                raise self._error("ORDER BY podrzava najvise jedan atribut")
            raw_order = (order_attribute, direction)

        self._match_kind("SEMICOLON")
        if self._peek().kind != "EOF":
            keyword = self._peek().value.upper()
            if keyword == "GROUP":
                raise self._error("GROUP BY nije podrzan")
            if keyword in {"JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS"}:
                raise self._error(
                    "Koristite tabele razdvojene zarezima; JOIN operator nije podrzan"
                )
            raise self._error(f"Neocekivan deo upita: {self._peek().value!r}")

        if len(raw_from) > 4:
            raise SQLParseError("Upit sme da koristi najvise 4 tabele.")
        if len(raw_predicates) > 6:
            raise SQLParseError("WHERE klauzula sme da sadrzi najvise 6 uslova.")

        from_items = self._bind_from_items(raw_from)
        select = tuple(
            self._bind_reference(reference, from_items) for reference in raw_select
        )
        predicates = tuple(
            self._bind_predicate(predicate, from_items) for predicate in raw_predicates
        )
        order_by = None
        if raw_order is not None:
            order_by = OrderSpec(
                attribute=self._bind_reference(raw_order[0], from_items),
                direction=raw_order[1],
            )
        return ParsedQuery(
            select=select,
            from_items=from_items,
            predicates=predicates,
            order_by=order_by,
        )

    def _parse_reference_list(self, clause: str) -> list[_RawRef]:
        if self._peek().kind == "STAR":
            raise self._error(
                f"{clause} lista mora eksplicitno navesti atribute; '*' nije podrzan"
            )
        references = [self._parse_reference()]
        while self._match_kind("COMMA"):
            references.append(self._parse_reference())
        return references

    def _parse_from_list(self) -> list[_RawFromItem]:
        items = [self._parse_from_item()]
        while self._match_kind("COMMA"):
            items.append(self._parse_from_item())
        return items

    def _parse_from_item(self) -> _RawFromItem:
        table_name = self._parse_identifier("Ocekivan je naziv tabele")
        alias = None
        if self._match_keyword("AS"):
            alias = self._parse_identifier("Ocekivan je alias posle AS")
        elif self._peek().kind == "IDENT":
            possible_alias = self._peek().value.upper()
            if possible_alias not in _RESERVED_AFTER_TABLE:
                alias = self._parse_identifier("Ocekivan je alias tabele")
        return _RawFromItem(table_name=table_name, alias=alias)

    def _parse_predicate(self) -> _RawPredicate:
        left = self._parse_operand()
        operator_token = self._peek()
        if operator_token.kind != "OP":
            raise self._error("Ocekivan je operator =, !=, <>, <, <=, > ili >=")
        self.position += 1
        right = self._parse_operand()
        return _RawPredicate(left=left, operator=operator_token.value, right=right)

    def _parse_operand(self) -> _RawRef | Literal:
        token = self._peek()
        if token.kind == "STRING":
            self.position += 1
            return Literal(value=token.value[1:-1].replace("''", "'"), raw=token.value)
        if token.kind == "NUMBER":
            self.position += 1
            value: int | float
            if "." in token.value:
                value = float(token.value)
            else:
                value = int(token.value)
            return Literal(value=value, raw=token.value)
        return self._parse_reference()

    def _parse_reference(self) -> _RawRef:
        first = self._parse_identifier("Ocekivan je naziv atributa")
        if self._match_kind("DOT"):
            second = self._parse_identifier("Ocekivan je atribut posle tacke")
            return _RawRef(qualifier=first, name=second)
        if self._peek().kind == "LPAREN":
            raise self._error(
                "SELECT podrzava samo atribute, bez funkcija i agregacija"
            )
        return _RawRef(qualifier=None, name=first)

    def _bind_from_items(self, raw_items: list[_RawFromItem]) -> tuple[FromItem, ...]:
        bound: list[FromItem] = []
        aliases: set[str] = set()
        for raw_item in raw_items:
            table = self.schema_tables.get(raw_item.table_name.casefold())
            if table is None:
                raise SQLParseError(
                    f"Tabela '{raw_item.table_name}' ne postoji u ulaznoj semi."
                )
            alias = raw_item.alias or table.name
            folded_alias = alias.casefold()
            if folded_alias in aliases:
                raise SQLParseError(
                    f"Alias ili naziv tabele '{alias}' se koristi vise puta."
                )
            aliases.add(folded_alias)
            bound.append(FromItem(table_name=table.name, alias=alias))
        return tuple(bound)

    def _bind_predicate(
        self, raw_predicate: _RawPredicate, from_items: tuple[FromItem, ...]
    ) -> Predicate:
        left = self._bind_operand(raw_predicate.left, from_items)
        right = self._bind_operand(raw_predicate.right, from_items)
        operator = raw_predicate.operator
        if isinstance(left, Literal) and isinstance(right, Literal):
            raise SQLParseError("Uslov mora da referencira najmanje jedan atribut.")
        if isinstance(left, Literal) and isinstance(right, AttributeRef):
            left, right = right, left
            operator = _INVERTED_OPERATOR[operator]
        return Predicate(left=left, operator=operator, right=right)

    def _bind_operand(
        self, operand: _RawRef | Literal, from_items: tuple[FromItem, ...]
    ) -> Operand:
        if isinstance(operand, Literal):
            return operand
        return self._bind_reference(operand, from_items)

    def _bind_reference(
        self, reference: _RawRef, from_items: tuple[FromItem, ...]
    ) -> AttributeRef:
        if reference.qualifier is not None:
            matching_items = [
                item
                for item in from_items
                if item.alias.casefold() == reference.qualifier.casefold()
            ]
            if not matching_items:
                aliases = ", ".join(item.alias for item in from_items)
                raise SQLParseError(
                    f"Nepoznata tabela ili alias '{reference.qualifier}'. Dostupno: {aliases}."
                )
            item = matching_items[0]
            table = self.schema_tables[item.table_name.casefold()]
            attribute = table.attribute_map.get(reference.name.casefold())
            if attribute is None:
                raise SQLParseError(
                    f"Tabela '{item.alias}' nema atribut '{reference.name}'."
                )
            return AttributeRef(table=item.alias, name=attribute.name)

        matches: list[tuple[FromItem, str]] = []
        for item in from_items:
            table = self.schema_tables[item.table_name.casefold()]
            attribute = table.attribute_map.get(reference.name.casefold())
            if attribute is not None:
                matches.append((item, attribute.name))
        if not matches:
            raise SQLParseError(
                f"Atribut '{reference.name}' ne postoji ni u jednoj FROM tabeli."
            )
        if len(matches) > 1:
            owners = ", ".join(item.alias for item, _ in matches)
            raise SQLParseError(
                f"Atribut '{reference.name}' je dvosmislen; navedite tabelu ({owners})."
            )
        item, attribute_name = matches[0]
        return AttributeRef(table=item.alias, name=attribute_name)

    def _parse_identifier(self, message: str) -> str:
        token = self._peek()
        if token.kind == "IDENT":
            self.position += 1
            return token.value
        raise self._error(message)

    def _expect_keyword(self, keyword: str) -> None:
        if not self._match_keyword(keyword):
            raise self._error(f"Ocekivana je kljucna rec {keyword}")

    def _match_keyword(self, keyword: str) -> bool:
        if self._check_keyword(keyword):
            self.position += 1
            return True
        return False

    def _check_keyword(self, keyword: str) -> bool:
        token = self._peek()
        return token.kind == "IDENT" and token.value.upper() == keyword

    def _match_kind(self, kind: str) -> bool:
        if self._peek().kind == kind:
            self.position += 1
            return True
        return False

    def _peek(self) -> _Token:
        return self.tokens[self.position]

    def _error(self, message: str) -> SQLParseError:
        token = self._peek()
        line = self.sql.count("\n", 0, token.position) + 1
        last_newline = self.sql.rfind("\n", 0, token.position)
        column = token.position - last_newline
        near = "kraj upita" if token.kind == "EOF" else repr(token.value)
        return SQLParseError(f"{message} (red {line}, kolona {column}, kod {near}).")

    @staticmethod
    def _tokenize(sql: str) -> list[_Token]:
        tokens: list[_Token] = []
        for match in _TOKEN_PATTERN.finditer(sql):
            kind = match.lastgroup
            if kind is None or kind == "SPACE":
                continue
            value = match.group()
            if kind == "MISMATCH":
                raise SQLParseError(
                    f"Nedozvoljen znak {value!r} na poziciji {match.start() + 1}."
                )
            tokens.append(_Token(kind=kind, value=value, position=match.start()))
        tokens.append(_Token(kind="EOF", value="", position=len(sql)))
        return tokens
