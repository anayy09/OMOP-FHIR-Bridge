# Vendored OMOP CDM v5.4 DDL

Fetched by `scripts/fetch_vendor.py` on 2026-08-12. Do not edit by hand.

- Source repository: **OHDSI/CommonDataModel** (Apache-2.0)
- Commit: `bf8177821d5b78236932eb5d799a2e8da35ab4b6` (2026-08-04T17:33:38Z)
- Dialect: `duckdb`, from `inst/ddl/5.4/duckdb/`

These files are the specification this project is checked against. `ddl.py` parses them to
derive column nullability, `varchar(n)` bounds, primary keys and foreign keys, and every
structural check in `checks.py` is generated from that parse. Editing them by hand would
quietly change what conformance *means* here, which is why they are refreshed by script
and fingerprinted below.

| file | bytes | SHA-256 |
|---|---|---|
| `OMOPCDM_duckdb_5.4_ddl.sql` | 18,297 | `40257d6a4fbb34adb080539f1aec323be24942df57937714f804f4219883920b` |
| `OMOPCDM_duckdb_5.4_primary_keys.sql` | 2,855 | `1078511f613cc9f8148d936126645dc6fdb5ecefdf410388a01dbaee78a7111a` |
| `OMOPCDM_duckdb_5.4_constraints.sql` | 32,514 | `2d3606bbaf4bf756f23ab1d963d6f97caafb01b63ed3bfe8a6d53d2d461ac621` |
| `OMOPCDM_duckdb_5.4_indices.sql` | 7,941 | `a659dd7e9eb6a9900a117c39c51b779ee4408458725231c2cad6a69b8c244c8b` |

## Why the DuckDB dialect

DuckDB is the default target because it needs no server, so `ofb pipeline` runs in CI and on
a laptop with no setup. OHDSI publishes the same v5.4 model for PostgreSQL, SQL Server,
Snowflake, BigQuery, Spark and others; re-run this script with `--dialect postgresql` to
vendor those instead. The mapping logic is dialect-independent — only `ddl.py`'s parse and
the connection in `cli.py` care.

## Manifest

```json
{
  "repo": "OHDSI/CommonDataModel",
  "commit": {
    "sha": "bf8177821d5b78236932eb5d799a2e8da35ab4b6",
    "date": "2026-08-04T17:33:38Z"
  },
  "files": [
    {
      "file": "OMOPCDM_duckdb_5.4_ddl.sql",
      "url": "https://raw.githubusercontent.com/OHDSI/CommonDataModel/bf8177821d5b78236932eb5d799a2e8da35ab4b6/inst/ddl/5.4/duckdb/OMOPCDM_duckdb_5.4_ddl.sql",
      "bytes": 18297,
      "sha256": "40257d6a4fbb34adb080539f1aec323be24942df57937714f804f4219883920b"
    },
    {
      "file": "OMOPCDM_duckdb_5.4_primary_keys.sql",
      "url": "https://raw.githubusercontent.com/OHDSI/CommonDataModel/bf8177821d5b78236932eb5d799a2e8da35ab4b6/inst/ddl/5.4/duckdb/OMOPCDM_duckdb_5.4_primary_keys.sql",
      "bytes": 2855,
      "sha256": "1078511f613cc9f8148d936126645dc6fdb5ecefdf410388a01dbaee78a7111a"
    },
    {
      "file": "OMOPCDM_duckdb_5.4_constraints.sql",
      "url": "https://raw.githubusercontent.com/OHDSI/CommonDataModel/bf8177821d5b78236932eb5d799a2e8da35ab4b6/inst/ddl/5.4/duckdb/OMOPCDM_duckdb_5.4_constraints.sql",
      "bytes": 32514,
      "sha256": "2d3606bbaf4bf756f23ab1d963d6f97caafb01b63ed3bfe8a6d53d2d461ac621"
    },
    {
      "file": "OMOPCDM_duckdb_5.4_indices.sql",
      "url": "https://raw.githubusercontent.com/OHDSI/CommonDataModel/bf8177821d5b78236932eb5d799a2e8da35ab4b6/inst/ddl/5.4/duckdb/OMOPCDM_duckdb_5.4_indices.sql",
      "bytes": 7941,
      "sha256": "a659dd7e9eb6a9900a117c39c51b779ee4408458725231c2cad6a69b8c244c8b"
    }
  ]
}
```
