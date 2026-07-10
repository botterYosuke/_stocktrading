from __future__ import annotations

# DuckDB cannot bind prepared parameters inside CREATE/COPY, so those statements
# have to inline their values. Everything that does so goes through here.


def sql_str(value: str) -> str:
    """Quote a value as a SQL string literal (single-quote escaped)."""
    return "'" + value.replace("'", "''") + "'"
