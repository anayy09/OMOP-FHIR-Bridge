"""The OMOP CDM v5.4 schema, read out of the vendored OHDSI DDL rather than restated here.

Every conformance check in ``checks.py`` is generated from what this module parses. That is the
point: a hand-written list of NOT NULL columns drifts away from the specification the moment the
specification moves, and the drift is invisible. Parsing the DDL means the checks are wrong only if
OHDSI's own DDL is wrong.

Source: OHDSI/CommonDataModel, ``inst/ddl/5.4/duckdb/``. See ``vendor/README.md`` for the exact
commit, URLs and SHA-256 of each vendored file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

SCHEMA_PLACEHOLDER = "@cdmDatabaseSchema."

_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+" + re.escape(SCHEMA_PLACEHOLDER) + r"(\w+)\s*\((.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)
_PK_RE = re.compile(
    r"ALTER\s+TABLE\s+" + re.escape(SCHEMA_PLACEHOLDER) + r"(\w+)\s+ADD\s+CONSTRAINT\s+\w+\s+"
    r"PRIMARY\s+KEY\s*\(([^)]*)\)",
    re.IGNORECASE,
)
_FK_RE = re.compile(
    r"ALTER\s+TABLE\s+" + re.escape(SCHEMA_PLACEHOLDER) + r"(\w+)\s+ADD\s+CONSTRAINT\s+\w+\s+"
    r"FOREIGN\s+KEY\s*\(([^)]*)\)\s+REFERENCES\s+" + re.escape(SCHEMA_PLACEHOLDER)
    + r"(\w+)\s*\(([^)]*)\)",
    re.IGNORECASE,
)
_VARCHAR_RE = re.compile(r"varchar\s*\(\s*(\d+)\s*\)", re.IGNORECASE)


@dataclass(frozen=True)
class Column:
    name: str
    sql_type: str
    nullable: bool

    @property
    def max_length(self) -> int | None:
        m = _VARCHAR_RE.search(self.sql_type)
        return int(m.group(1)) if m else None

    @property
    def is_concept_id(self) -> bool:
        return self.name.endswith("_concept_id")


@dataclass(frozen=True)
class ForeignKey:
    table: str
    column: str
    ref_table: str
    ref_column: str


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...]

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(f"{self.name} has no column {name}")

    @property
    def not_null(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if not c.nullable)

    @property
    def bounded_text(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.max_length is not None)


@dataclass(frozen=True)
class Schema:
    tables: dict[str, Table]
    foreign_keys: tuple[ForeignKey, ...]

    def __getitem__(self, name: str) -> Table:
        return self.tables[name.lower()]

    def foreign_keys_for(self, table: str) -> tuple[ForeignKey, ...]:
        return tuple(fk for fk in self.foreign_keys if fk.table == table.lower())


def _split_column_defs(body: str) -> list[str]:
    """Split a CREATE TABLE body on commas that are not inside parentheses."""
    parts, depth, current = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _parse_column(defn: str) -> Column | None:
    tokens = defn.split()
    if not tokens or tokens[0].upper() in {"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE"}:
        return None
    name = tokens[0].strip('"').lower()
    rest = " ".join(tokens[1:])
    upper = rest.upper()
    nullable = "NOT NULL" not in upper
    sql_type = re.split(r"\s+NOT\s+NULL|\s+NULL\b", rest, flags=re.IGNORECASE)[0].strip()
    return Column(name=name, sql_type=sql_type, nullable=nullable)


def parse_schema(ddl_dir: Path) -> Schema:
    ddl_sql = (ddl_dir / "OMOPCDM_duckdb_5.4_ddl.sql").read_text(encoding="utf-8")
    pk_sql = (ddl_dir / "OMOPCDM_duckdb_5.4_primary_keys.sql").read_text(encoding="utf-8")
    fk_sql = (ddl_dir / "OMOPCDM_duckdb_5.4_constraints.sql").read_text(encoding="utf-8")

    pks: dict[str, tuple[str, ...]] = {}
    for m in _PK_RE.finditer(pk_sql):
        cols = tuple(c.strip().strip('"').lower() for c in m.group(2).split(","))
        pks[m.group(1).lower()] = cols

    tables: dict[str, Table] = {}
    for m in _TABLE_RE.finditer(ddl_sql):
        name = m.group(1).lower()
        cols = tuple(
            c for c in (_parse_column(d) for d in _split_column_defs(m.group(2))) if c is not None
        )
        tables[name] = Table(name=name, columns=cols, primary_key=pks.get(name, ()))

    fks = tuple(
        ForeignKey(
            table=m.group(1).lower(),
            column=m.group(2).strip().strip('"').lower(),
            ref_table=m.group(3).lower(),
            ref_column=m.group(4).strip().strip('"').lower(),
        )
        for m in _FK_RE.finditer(fk_sql)
    )
    if not tables:
        raise RuntimeError(f"no CREATE TABLE statements found under {ddl_dir}")
    return Schema(tables=tables, foreign_keys=fks)


def vendored_ddl_dir() -> Path:
    """Locate ``vendor/omop-cdm-5.4/duckdb`` from an installed or checked-out copy."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "vendor" / "omop-cdm-5.4" / "duckdb"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "vendored OMOP CDM DDL not found; run `python scripts/fetch_vendor.py` from the repo root"
    )


@lru_cache(maxsize=1)
def schema() -> Schema:
    return parse_schema(vendored_ddl_dir())


def create_tables(con, only: tuple[str, ...] | None = None) -> list[str]:
    """Execute the vendored DDL (and primary keys) against a DuckDB connection.

    The DDL is applied verbatim apart from stripping OHDSI's schema placeholder, so the column
    types in the database are OHDSI's types and not this project's opinion of them.
    """
    ddl_dir = vendored_ddl_dir()
    ddl_sql = (ddl_dir / "OMOPCDM_duckdb_5.4_ddl.sql").read_text(encoding="utf-8")
    pk_sql = (ddl_dir / "OMOPCDM_duckdb_5.4_primary_keys.sql").read_text(encoding="utf-8")
    created: list[str] = []
    for m in _TABLE_RE.finditer(ddl_sql):
        name = m.group(1).lower()
        if only and name not in only:
            continue
        con.execute(m.group(0).replace(SCHEMA_PLACEHOLDER, ""))
        created.append(name)
    # DuckDB has no ALTER TABLE ... ADD PRIMARY KEY, so primary keys are enforced by the
    # `primary_key_unique` check in checks.py, driven by the same file parsed here.
    _ = pk_sql
    return created
