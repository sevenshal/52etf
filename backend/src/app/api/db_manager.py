import base64
import re
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import inspect

from ...core.database import DB_PATH, engine
from .account import valid_account


router = APIRouter(prefix="/api/db", tags=["DB"])

MAX_QUERY_LIMIT = 500
INTERNAL_TABLE_PREFIXES = ("sqlite_",)


class DbColumn(BaseModel):
    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False


class DbTable(BaseModel):
    name: str
    columns: List[DbColumn]
    column_count: int
    sample_sql: str


class DbSchemaResponse(BaseModel):
    tables: List[DbTable]
    max_limit: int = MAX_QUERY_LIMIT


class DbQueryRequest(BaseModel):
    sql: str = Field(..., min_length=1, max_length=20000)


class DbQueryResponse(BaseModel):
    columns: List[str]
    rows: List[Dict[str, Any]]
    row_count: int
    max_limit: int = MAX_QUERY_LIMIT
    limit_applied: bool
    executed_sql: str


def _quote_identifier(identifier: str) -> str:
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'


def _get_table_metadata() -> Tuple[List[DbTable], Set[str]]:
    inspector = inspect(engine)
    tables: List[DbTable] = []
    allowed_table_names: Set[str] = set()

    for table_name in sorted(inspector.get_table_names()):
        lowered = table_name.lower()
        if lowered.startswith(INTERNAL_TABLE_PREFIXES):
            continue

        columns_raw = inspector.get_columns(table_name)
        if any(column["name"].lower() == "account_id" for column in columns_raw):
            continue

        primary_keys = set(inspector.get_pk_constraint(table_name).get("constrained_columns") or [])
        columns = [
            DbColumn(
                name=column["name"],
                type=str(column.get("type") or ""),
                nullable=bool(column.get("nullable", True)),
                primary_key=column["name"] in primary_keys,
            )
            for column in columns_raw
        ]
        tables.append(
            DbTable(
                name=table_name,
                columns=columns,
                column_count=len(columns),
                sample_sql=f"SELECT * FROM {_quote_identifier(table_name)} LIMIT 100",
            )
        )
        allowed_table_names.add(table_name.lower())

    return tables, allowed_table_names


def _strip_sql_comments(sql: str) -> str:
    result: List[str] = []
    index = 0
    quote: Optional[str] = None
    bracket_quote = False

    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""

        if quote:
            result.append(char)
            if bracket_quote:
                if char == "]":
                    quote = None
                    bracket_quote = False
            elif char == quote:
                if next_char == quote:
                    result.append(next_char)
                    index += 1
                else:
                    quote = None
            index += 1
            continue

        if char in ("'", '"', "`"):
            quote = char
            result.append(char)
            index += 1
            continue

        if char == "[":
            quote = char
            bracket_quote = True
            result.append(char)
            index += 1
            continue

        if char == "-" and next_char == "-":
            while index < len(sql) and sql[index] not in "\r\n":
                index += 1
            result.append("\n")
            continue

        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(sql) and not (sql[index] == "*" and sql[index + 1] == "/"):
                result.append("\n" if sql[index] in "\r\n" else " ")
                index += 1
            index += 2 if index + 1 < len(sql) else 0
            continue

        result.append(char)
        index += 1

    return "".join(result)


def _find_statement_semicolon(sql: str) -> Optional[int]:
    index = 0
    quote: Optional[str] = None
    bracket_quote = False

    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""

        if quote:
            if bracket_quote:
                if char == "]":
                    quote = None
                    bracket_quote = False
            elif char == quote:
                if next_char == quote:
                    index += 1
                else:
                    quote = None
            index += 1
            continue

        if char in ("'", '"', "`"):
            quote = char
            index += 1
            continue

        if char == "[":
            quote = char
            bracket_quote = True
            index += 1
            continue

        if char == "-" and next_char == "-":
            while index < len(sql) and sql[index] not in "\r\n":
                index += 1
            continue

        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(sql) and not (sql[index] == "*" and sql[index + 1] == "/"):
                index += 1
            index += 2 if index + 1 < len(sql) else 0
            continue

        if char == ";":
            return index

        index += 1

    return None


def _normalize_single_statement(sql: str) -> str:
    statement = sql.strip()
    if not statement:
        raise HTTPException(status_code=400, detail="SQL不能为空")

    semicolon_index = _find_statement_semicolon(statement)
    if semicolon_index is not None:
        trailing = statement[semicolon_index + 1 :]
        if _strip_sql_comments(trailing).strip():
            raise HTTPException(status_code=400, detail="一次只能执行一条查询语句")
        statement = statement[:semicolon_index].strip()

    if not statement:
        raise HTTPException(status_code=400, detail="SQL不能为空")
    return statement


def _assert_select_statement(statement: str):
    statement_without_comments = _strip_sql_comments(statement)
    match = re.search(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", statement_without_comments)
    first_keyword = match.group(1).lower() if match else ""

    if first_keyword not in {"select", "with"}:
        raise HTTPException(status_code=400, detail="只允许执行SELECT查询语句")


def _contains_limit(statement: str) -> bool:
    return bool(re.search(r"\blimit\b", _strip_sql_comments(statement), flags=re.IGNORECASE))


def _build_limited_query(statement: str) -> Tuple[str, bool]:
    limit_applied = not _contains_limit(statement)
    return f"SELECT * FROM (\n{statement}\n) AS db_tool_query LIMIT {MAX_QUERY_LIMIT}", limit_applied


def _get_readonly_db_uri() -> str:
    return f"file:{quote(DB_PATH)}?mode=ro"


def _make_authorizer(allowed_table_names: Set[str]):
    allowed_actions = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
    }

    def authorizer(action, arg1, arg2, _database_name, _trigger_name):
        if action not in allowed_actions:
            return sqlite3.SQLITE_DENY

        if action == sqlite3.SQLITE_READ:
            table_name = (arg1 or "").lower()
            if not table_name:
                return sqlite3.SQLITE_OK
            if table_name.startswith(INTERNAL_TABLE_PREFIXES):
                return sqlite3.SQLITE_DENY
            if table_name not in allowed_table_names:
                return sqlite3.SQLITE_DENY

        if action == sqlite3.SQLITE_FUNCTION:
            function_name = (arg1 or arg2 or "").lower()
            if function_name == "load_extension":
                return sqlite3.SQLITE_DENY

        return sqlite3.SQLITE_OK

    return authorizer


def _serialize_value(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return {
            "type": "binary",
            "size": len(value),
            "base64": base64.b64encode(value).decode("ascii"),
        }
    return str(value)


@router.get("/tables", response_model=DbSchemaResponse)
def list_queryable_tables(_account_id: str = Depends(valid_account)):
    tables, _allowed_table_names = _get_table_metadata()
    return DbSchemaResponse(tables=tables)


@router.post("/query", response_model=DbQueryResponse)
def execute_query(payload: DbQueryRequest, _account_id: str = Depends(valid_account)):
    statement = _normalize_single_statement(payload.sql)
    _assert_select_statement(statement)
    _tables, allowed_table_names = _get_table_metadata()

    query, limit_applied = _build_limited_query(statement)

    try:
        with sqlite3.connect(_get_readonly_db_uri(), uri=True, timeout=30) as connection:
            connection.execute("PRAGMA busy_timeout=30000")
            connection.set_authorizer(_make_authorizer(allowed_table_names))
            cursor = connection.execute(query)
            columns = [column[0] for column in (cursor.description or [])]
            rows = [
                {column: _serialize_value(value) for column, value in zip(columns, row)}
                for row in cursor.fetchall()
            ]
    except sqlite3.Error as exc:
        detail = str(exc)
        if "prohibited" in detail or "not authorized" in detail:
            detail = "只允许查询没有account_id字段的数据表，且只能执行只读SELECT语句"
        raise HTTPException(status_code=400, detail=detail) from exc

    return DbQueryResponse(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        limit_applied=limit_applied,
        executed_sql=query,
    )
