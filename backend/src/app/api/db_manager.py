import base64
import re
import sqlite3
import threading
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional, Set, Tuple
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import inspect
from sqlalchemy.orm import Session as OrmSession

from ...core.database import DB_PATH, DbSqlFavorite, engine, get_db
from ...core.analytics_database import ANALYTICS_DB_PATH, ANALYTICS_TABLE_NAMES
from ...core.duckdb_utils import connect_duckdb
from .account import valid_admin_account


router = APIRouter(prefix="/api/db", tags=["DB"])

MAX_QUERY_LIMIT = 500
INTERNAL_TABLE_PREFIXES = ("sqlite_",)
SCHEMA_CACHE_TTL_SECONDS = 60
DUCKDB_FORBIDDEN_IDENTIFIERS = {
    "attach",
    "call",
    "copy",
    "csv_scan",
    "detach",
    "duckdb_columns",
    "duckdb_databases",
    "duckdb_extensions",
    "duckdb_functions",
    "duckdb_schemas",
    "duckdb_secrets",
    "duckdb_settings",
    "duckdb_tables",
    "export",
    "glob",
    "httpfs",
    "import",
    "information_schema",
    "install",
    "json_scan",
    "load",
    "parquet_scan",
    "pg_catalog",
    "pragma",
    "read_blob",
    "read_csv",
    "read_csv_auto",
    "read_json",
    "read_json_auto",
    "read_ndjson",
    "read_parquet",
    "read_text",
    "reset",
    "set",
    "sqlite_master",
    "sqlite_query",
    "sqlite_scan",
    "sqlite_schema",
}

_schema_cache_lock = threading.Lock()
_schema_cache: Dict[str, Any] = {
    "expires_at": 0.0,
    "tables": [],
    "allowed_table_names": set(),
    "restricted_table_names": set(),
}


class DbColumn(BaseModel):
    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False


class DbTable(BaseModel):
    name: str
    source: Literal["duckdb", "sqlite"]
    columns: List[DbColumn]
    column_count: int
    sample_sql: str


class DbSchemaResponse(BaseModel):
    tables: List[DbTable]
    max_limit: int = MAX_QUERY_LIMIT


class DbQueryRequest(BaseModel):
    sql: str = Field(..., min_length=1, max_length=20000)
    engine: Literal["sqlite", "duckdb"] = "sqlite"


class DbQueryResponse(BaseModel):
    columns: List[str]
    rows: List[Dict[str, Any]]
    row_count: int
    max_limit: int = MAX_QUERY_LIMIT
    limit_applied: bool
    executed_sql: str
    engine: str
    timings: Dict[str, float] = Field(default_factory=dict)


class DbSqlFavoriteRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    sql: str = Field(..., min_length=1, max_length=20000)
    engine: Literal["sqlite", "duckdb"] = "duckdb"


class DbSqlFavoriteResponse(BaseModel):
    id: int
    name: str
    sql: str
    engine: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DbSqlFavoriteDeleteResponse(BaseModel):
    success: bool


def _serialize_favorite(favorite: DbSqlFavorite) -> DbSqlFavoriteResponse:
    return DbSqlFavoriteResponse(
        id=int(favorite.id),
        name=favorite.name,
        sql=favorite.sql,
        engine=favorite.engine or "duckdb",
        created_at=favorite.created_at,
        updated_at=favorite.updated_at,
    )


def _quote_identifier(identifier: str) -> str:
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'


def _quote_sql_string(value: str) -> str:
    return f"'{value.replace(chr(39), chr(39) * 2)}'"


def _connect_duckdb_with_fallback(database: str, prefer_read_only: bool = True):
    _import_duckdb()
    return connect_duckdb(database=database, prefer_read_only=prefer_read_only)


def _get_primary_key_columns(inspector, table_name: str) -> Set[str]:
    try:
        return set(inspector.get_pk_constraint(table_name).get("constrained_columns") or [])
    except Exception:
        return set()


def _build_table_metadata(inspector, table_name: str) -> Optional[DbTable]:
    columns_raw = inspector.get_columns(table_name)
    if any(column["name"].lower() == "account_id" for column in columns_raw):
        return None

    primary_keys = _get_primary_key_columns(inspector, table_name)
    columns = [
        DbColumn(
            name=column["name"],
            type=str(column.get("type") or ""),
            nullable=bool(column.get("nullable", True)),
            primary_key=column["name"] in primary_keys,
        )
        for column in columns_raw
    ]
    return DbTable(
        name=table_name,
        source="sqlite",
        columns=columns,
        column_count=len(columns),
        sample_sql=f"SELECT * FROM {_quote_identifier(table_name)} LIMIT 100",
    )


def _build_duckdb_table_metadata(connection, table_name: str) -> Optional[DbTable]:
    rows = connection.execute(f"PRAGMA table_info({_quote_sql_string(table_name)})").fetchall()
    if any(str(row[1]).lower() == "account_id" for row in rows):
        return None

    columns = [
        DbColumn(
            name=str(row[1]),
            type=str(row[2] or ""),
            nullable=not bool(row[3]),
            primary_key=bool(row[5]),
        )
        for row in rows
    ]
    return DbTable(
        name=table_name,
        source="duckdb",
        columns=columns,
        column_count=len(columns),
        sample_sql=f"SELECT * FROM {_quote_identifier(table_name)} LIMIT 100",
    )


def _load_table_metadata() -> Tuple[List[DbTable], Set[str], Set[str]]:
    sqlite_inspector = inspect(engine)
    table_by_name: Dict[str, DbTable] = {}
    allowed_table_names: Set[str] = set()
    restricted_table_names: Set[str] = set()

    for table_name in sorted(sqlite_inspector.get_table_names()):
        lowered = table_name.lower()
        if lowered.startswith(INTERNAL_TABLE_PREFIXES):
            restricted_table_names.add(lowered)
            continue

        table = _build_table_metadata(sqlite_inspector, table_name)
        if table is None:
            restricted_table_names.add(lowered)
            continue

        table_by_name[lowered] = table
        allowed_table_names.add(table_name.lower())

    connection = _connect_duckdb_with_fallback(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        analytics_tables = [row[0] for row in connection.execute("SHOW TABLES").fetchall()]
        for table_name in sorted(analytics_tables):
            lowered = table_name.lower()
            if lowered.startswith(INTERNAL_TABLE_PREFIXES):
                restricted_table_names.add(lowered)
                continue

            table = _build_duckdb_table_metadata(connection, table_name)
            if table is None:
                restricted_table_names.add(lowered)
                continue

            table_by_name[lowered] = table
            allowed_table_names.add(lowered)
            restricted_table_names.discard(lowered)
    finally:
        connection.close()

    return sorted(
        table_by_name.values(),
        key=lambda table: (0 if table.source == "duckdb" else 1, table.name.lower()),
    ), allowed_table_names, restricted_table_names


def _get_table_metadata(force_refresh: bool = False) -> Tuple[List[DbTable], Set[str], Set[str]]:
    now = time.perf_counter()
    with _schema_cache_lock:
        if not force_refresh and _schema_cache["expires_at"] > now:
            return (
                _schema_cache["tables"],
                _schema_cache["allowed_table_names"],
                _schema_cache["restricted_table_names"],
            )

        tables, allowed_table_names, restricted_table_names = _load_table_metadata()
        _schema_cache.update(
            {
                "expires_at": now + SCHEMA_CACHE_TTL_SECONDS,
                "tables": tables,
                "allowed_table_names": allowed_table_names,
                "restricted_table_names": restricted_table_names,
            }
        )
        return tables, allowed_table_names, restricted_table_names


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


def _mask_sql_comments_and_string_literals(sql: str) -> str:
    result: List[str] = []
    index = 0
    in_single_quote = False

    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""

        if in_single_quote:
            if char == "'":
                if next_char == "'":
                    result.extend("  ")
                    index += 2
                    continue
                in_single_quote = False
            result.append(" ")
            index += 1
            continue

        if char == "'":
            in_single_quote = True
            result.append(" ")
            index += 1
            continue

        if char == "-" and next_char == "-":
            while index < len(sql) and sql[index] not in "\r\n":
                result.append(" ")
                index += 1
            continue

        if char == "/" and next_char == "*":
            result.extend("  ")
            index += 2
            while index + 1 < len(sql) and not (sql[index] == "*" and sql[index + 1] == "/"):
                result.append("\n" if sql[index] in "\r\n" else " ")
                index += 1
            if index + 1 < len(sql):
                result.extend("  ")
                index += 2
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
    return bool(re.search(r"\blimit\b", _mask_sql_comments_and_string_literals(statement), flags=re.IGNORECASE))


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


def _assert_duckdb_safe_statement(statement: str):
    masked_statement = _mask_sql_comments_and_string_literals(statement).lower()
    identifiers = set(re.findall(r"\b[a-z_][a-z0-9_]*\b", masked_statement))
    forbidden = sorted(identifiers.intersection(DUCKDB_FORBIDDEN_IDENTIFIERS))
    if forbidden:
        raise HTTPException(
            status_code=400,
            detail=f"DuckDB分析模式禁止使用以下关键字或函数: {', '.join(forbidden)}",
        )


def _import_duckdb():
    try:
        import duckdb  # type: ignore
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="DuckDB未安装，请先在后端环境安装 duckdb 依赖",
        ) from exc
    return duckdb


def _find_referenced_tables(statement: str, tables: List[DbTable]) -> List[DbTable]:
    masked_statement = _mask_sql_comments_and_string_literals(statement).lower()
    identifiers = set(re.findall(r"\b[a-z_][a-z0-9_]*\b", masked_statement))
    return [table for table in tables if table.name.lower() in identifiers]


def _create_duckdb_sqlite_views(connection, tables: List[DbTable]):
    connection.execute("LOAD sqlite")
    for table in tables:
        quoted_table_name = _quote_identifier(table.name)
        connection.execute(
            f"""
            CREATE TEMP VIEW {quoted_table_name} AS
            SELECT * FROM sqlite_scan({_quote_sql_string(DB_PATH)}, {_quote_sql_string(table.name)})
            """
        )


def _execute_sqlite_query(query: str, allowed_table_names: Set[str]) -> Tuple[List[str], List[Dict[str, Any]], Dict[str, float]]:
    timings: Dict[str, float] = {}
    started_at = time.perf_counter()
    try:
        with sqlite3.connect(_get_readonly_db_uri(), uri=True, timeout=30) as connection:
            connection.execute("PRAGMA busy_timeout=30000")
            connection.set_authorizer(_make_authorizer(allowed_table_names))
            execute_started_at = time.perf_counter()
            cursor = connection.execute(query)
            timings["execute_ms"] = round((time.perf_counter() - execute_started_at) * 1000, 2)
            columns = [column[0] for column in (cursor.description or [])]
            fetch_started_at = time.perf_counter()
            raw_rows = cursor.fetchall()
            timings["fetch_ms"] = round((time.perf_counter() - fetch_started_at) * 1000, 2)
    except sqlite3.Error as exc:
        detail = str(exc)
        if "prohibited" in detail or "not authorized" in detail:
            detail = "只允许查询没有account_id字段的数据表，且只能执行只读SELECT语句"
        raise HTTPException(status_code=400, detail=detail) from exc

    serialize_started_at = time.perf_counter()
    rows = [
        {column: _serialize_value(value) for column, value in zip(columns, row)}
        for row in raw_rows
    ]
    timings["serialize_ms"] = round((time.perf_counter() - serialize_started_at) * 1000, 2)
    timings["total_ms"] = round((time.perf_counter() - started_at) * 1000, 2)
    return columns, rows, timings


def _execute_duckdb_query(query: str, referenced_tables: List[DbTable]) -> Tuple[List[str], List[Dict[str, Any]], Dict[str, float]]:
    duckdb = _import_duckdb()
    timings: Dict[str, float] = {}
    started_at = time.perf_counter()
    native_tables = [
        table
        for table in referenced_tables
        if table.name.lower() in ANALYTICS_TABLE_NAMES
    ]
    sqlite_tables = [
        table
        for table in referenced_tables
        if table.name.lower() not in ANALYTICS_TABLE_NAMES
    ]
    has_native_tables = bool(native_tables)

    try:
        if has_native_tables:
            connection = _connect_duckdb_with_fallback(database=ANALYTICS_DB_PATH, prefer_read_only=True)
        else:
            connection = duckdb.connect(database=":memory:", read_only=False)
        try:
            setup_started_at = time.perf_counter()
            if sqlite_tables:
                _create_duckdb_sqlite_views(connection, sqlite_tables)
            timings["setup_ms"] = round((time.perf_counter() - setup_started_at) * 1000, 2)

            execute_started_at = time.perf_counter()
            result = connection.execute(query)
            timings["execute_ms"] = round((time.perf_counter() - execute_started_at) * 1000, 2)
            columns = [column[0] for column in (result.description or [])]

            fetch_started_at = time.perf_counter()
            raw_rows = result.fetchall()
            timings["fetch_ms"] = round((time.perf_counter() - fetch_started_at) * 1000, 2)
        finally:
            connection.close()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    serialize_started_at = time.perf_counter()
    rows = [
        {column: _serialize_value(value) for column, value in zip(columns, row)}
        for row in raw_rows
    ]
    timings["serialize_ms"] = round((time.perf_counter() - serialize_started_at) * 1000, 2)
    timings["total_ms"] = round((time.perf_counter() - started_at) * 1000, 2)
    return columns, rows, timings


@router.get("/tables", response_model=DbSchemaResponse)
def list_queryable_tables(
    refresh: bool = Query(False),
    _account_id: str = Depends(valid_admin_account),
):
    tables, _allowed_table_names, _restricted_table_names = _get_table_metadata(force_refresh=refresh)
    return DbSchemaResponse(tables=tables)


@router.get("/favorites", response_model=List[DbSqlFavoriteResponse])
def list_sql_favorites(
    account_id: str = Depends(valid_admin_account),
    db: OrmSession = Depends(get_db),
):
    favorites = (
        db.query(DbSqlFavorite)
        .filter(DbSqlFavorite.account_id == account_id)
        .order_by(DbSqlFavorite.updated_at.desc(), DbSqlFavorite.id.desc())
        .all()
    )
    return [_serialize_favorite(favorite) for favorite in favorites]


@router.post("/favorites", response_model=DbSqlFavoriteResponse)
def save_sql_favorite(
    payload: DbSqlFavoriteRequest,
    account_id: str = Depends(valid_admin_account),
    db: OrmSession = Depends(get_db),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="收藏名称不能为空")

    statement = _normalize_single_statement(payload.sql)
    _assert_select_statement(statement)

    favorite = (
        db.query(DbSqlFavorite)
        .filter(
            DbSqlFavorite.account_id == account_id,
            DbSqlFavorite.name == name,
        )
        .first()
    )
    now = datetime.now()
    if favorite:
        favorite.sql = statement
        favorite.engine = payload.engine
        favorite.updated_at = now
    else:
        favorite = DbSqlFavorite(
            account_id=account_id,
            name=name,
            sql=statement,
            engine=payload.engine,
            created_at=now,
            updated_at=now,
        )
        db.add(favorite)

    db.commit()
    db.refresh(favorite)
    return _serialize_favorite(favorite)


@router.delete("/favorites/{favorite_id}", response_model=DbSqlFavoriteDeleteResponse)
def delete_sql_favorite(
    favorite_id: int,
    account_id: str = Depends(valid_admin_account),
    db: OrmSession = Depends(get_db),
):
    favorite = (
        db.query(DbSqlFavorite)
        .filter(
            DbSqlFavorite.id == favorite_id,
            DbSqlFavorite.account_id == account_id,
        )
        .first()
    )
    if not favorite:
        raise HTTPException(status_code=404, detail="未找到收藏")

    db.delete(favorite)
    db.commit()
    return DbSqlFavoriteDeleteResponse(success=True)


@router.post("/query", response_model=DbQueryResponse)
def execute_query(
    payload: DbQueryRequest,
    _account_id: str = Depends(valid_admin_account),
):
    total_started_at = time.perf_counter()
    statement = _normalize_single_statement(payload.sql)
    _assert_select_statement(statement)

    schema_started_at = time.perf_counter()
    tables, allowed_table_names, restricted_table_names = _get_table_metadata()
    schema_ms = round((time.perf_counter() - schema_started_at) * 1000, 2)

    query, limit_applied = _build_limited_query(statement)

    if payload.engine == "duckdb":
        _assert_duckdb_safe_statement(statement)
        referenced_tables = _find_referenced_tables(statement, tables)
        masked_statement = _mask_sql_comments_and_string_literals(statement).lower()
        referenced_identifiers = set(re.findall(r"\b[a-z_][a-z0-9_]*\b", masked_statement))
        blocked_identifiers = sorted(referenced_identifiers.intersection(restricted_table_names))
        if blocked_identifiers:
            raise HTTPException(
                status_code=400,
                detail=f"DuckDB分析模式只能查询没有account_id字段的数据表，当前SQL引用了受限表: {', '.join(blocked_identifiers)}",
            )
        columns, rows, timings = _execute_duckdb_query(query, referenced_tables)
    else:
        columns, rows, timings = _execute_sqlite_query(query, allowed_table_names)

    timings["schema_ms"] = schema_ms
    timings["request_total_ms"] = round((time.perf_counter() - total_started_at) * 1000, 2)

    return DbQueryResponse(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        limit_applied=limit_applied,
        executed_sql=query,
        engine=payload.engine,
        timings=timings,
    )
