"""A股业绩类公告流与**精确披露时刻**（东方财富公告接口）。

为什么需要它
------------
Tushare（forecast / express / income）和巨潮都只有**日期**粒度，而 A股绝大多数业绩公告
是在**收盘后（16:00~23:00）**披露的。这类公告的「公告日期」通常已经是**次日**——
也就是说日期本身已经等于市场第一次能反应的那一天。只看日期无法区分：

- 交易日 08:00 披露 → 当天开盘就能跳空（T = 公告日）；
- 交易日 20:00 披露 → 要到下一个交易日开盘才能跳空（T = 公告日 + 1）。

东方财富公告列表接口的 ``display_time`` 精确到毫秒（例如芯联集成 2026 三季报预告：
``notice_date=2026-09-29``、``display_time=2026-09-28 19:34:43``），是判断 T 日的唯一可靠依据。

接口
----
``https://np-anotice-stock.eastmoney.com/api/security/ann``（page_size 上限 100）

- ``f_node=1`` 财务报告大类；再按 ``s_node`` 细分：

  ======  ==========
  s_node  含义
  ======  ==========
  1       定期报告（年报/中报/季报全文、摘要、更正）
  5       业绩预告
  6       业绩快报
  ======  ==========

  只取这三个子类，单次增量只需几页（业绩快报/预告季 10 页量级），不用把全市场
  两万多条公告拉一遍。

- ``begin_time`` / ``end_time`` 过滤的是 ``notice_date``。晚间披露的公告 notice_date 是次日，
  所以增量窗口必须**往后多取一天**，否则当晚 20:00 跑的时候当天的公告还没出现。
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
import requests

from ..duckdb_utils import ANALYTICS_DB_PATH, connect_duckdb_for_write

logger = logging.getLogger(__name__)

ANNOUNCEMENT_TABLE = "a_stock_announcement"
ANNOUNCEMENT_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}
PAGE_SIZE = 100
REQUEST_TIMEOUT = 30
# 同一天公告多，逐页之间稍等一下，避免被东财限流
PAGE_SLEEP_SECONDS = 0.15
MAX_PAGES_PER_NODE = 60
# 表里数据很旧时最多一次往前补拉的日历天数，避免一次拉爆接口
MAX_LOOKBACK_DAYS = 400

EVENT_KIND_FORECAST = "forecast"
EVENT_KIND_EXPRESS = "express"
EVENT_KIND_REPORT = "report"

# s_node -> 事件类型
NODE_EVENT_KINDS: Dict[int, str] = {
    1: EVENT_KIND_REPORT,
    5: EVENT_KIND_FORECAST,
    6: EVENT_KIND_EXPRESS,
}
# 客户端再按 column_code 兜一层：避免以后东财调整节点含义时把无关公告写进库
EARNINGS_COLUMN_PREFIXES: Dict[str, str] = {
    "001001001": EVENT_KIND_REPORT,
    "001002004001": EVENT_KIND_FORECAST,
    "001002004002": EVENT_KIND_EXPRESS,
}
# 节点含义变化时的兜底：标题里出现这些词也算业绩公告
TITLE_KEYWORDS: Tuple[Tuple[str, str], ...] = (
    ("业绩预告", EVENT_KIND_FORECAST),
    ("业绩预增", EVENT_KIND_FORECAST),
    ("业绩快报", EVENT_KIND_EXPRESS),
    ("年度报告", EVENT_KIND_REPORT),
    ("半年度报告", EVENT_KIND_REPORT),
    ("季度报告", EVENT_KIND_REPORT),
)

ANNOUNCEMENT_COLUMNS = (
    "art_code",
    "ts_code",
    "name",
    "ann_date",
    "disclose_at",
    "event_kind",
    "category_code",
    "category_name",
    "title",
    "source",
)


class AnnouncementDataError(RuntimeError):
    pass


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def code_to_ts_code(value: Any) -> Optional[str]:
    """6 位 A股代码 → ts_code；带后缀的原样返回。与项目其它模块同一套前后缀规则。"""
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    if "." in raw:
        code, suffix = raw.split(".", 1)
        return f"{code}.{suffix}" if re.fullmatch(r"\d{6}", code) else None
    code = re.sub(r"\D", "", raw)
    if len(code) != 6:
        return None
    if code.startswith(("43", "83", "87", "88", "92")):
        return f"{code}.BJ"
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    return f"{code}.SZ"


def _parse_display_time(value: Any) -> Optional[datetime]:
    """东财 display_time 形如 ``2026-09-28 19:34:43:178``（毫秒用冒号分隔）。"""
    text = _clean_text(value)
    if not text:
        return None
    match = re.match(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", text)
    if not match:
        try:
            parsed = pd.Timestamp(text)
        except (TypeError, ValueError):
            return None
        return None if pd.isna(parsed) else parsed.to_pydatetime()
    try:
        return datetime.fromisoformat(f"{match.group(1)} {match.group(2)}")
    except ValueError:
        return None


def _parse_notice_date(value: Any) -> Optional[date]:
    text = _clean_text(value)
    if not text:
        return None
    try:
        parsed = pd.Timestamp(text)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(parsed) else parsed.date()


def _event_kind_of(columns: Sequence[Dict[str, Any]], title: Optional[str], fallback: Optional[str]) -> Optional[str]:
    """判定事件类型：先看 category 代码前缀，再看标题关键词，都不认识就不要这条。

    接口已经按 ``s_node`` 筛过一遍，但东财偶尔会把别类公告挂到财务报告节点下；这里不
    无条件信任节点，避免把无关公告的披露时刻错配到业绩事件上。
    """
    for column in columns or []:
        prefix = _clean_text(column.get("column_code"))
        if not prefix:
            continue
        for known_prefix, kind in EARNINGS_COLUMN_PREFIXES.items():
            if prefix.startswith(known_prefix):
                return kind
    for keyword, kind in TITLE_KEYWORDS:
        if title and keyword in title:
            return kind
    # 一条分类都没有时（接口异常）才退回节点推断
    return fallback if not columns else None


def _market_of(code_row: Dict[str, Any]) -> Optional[str]:
    ann_type = str(code_row.get("ann_type") or "").upper()
    if "KCB" in ann_type or "SHA" in ann_type:
        return "SH"
    if "BJA" in ann_type:
        return "BJ"
    return None


def _is_a_share_code(code: str) -> bool:
    """200xxx / 900xxx 是 B 股；A 股公告不取它们（否则 B 股会顶掉同一天的 A 股）。"""
    if not re.fullmatch(r"\d{6}", code):
        return False
    return not code.startswith(("200", "900"))


def _a_share_rank(code_row: Dict[str, Any]) -> int:
    """越小越优先：板块标记（SHA/KCB/CYB/BJA）> 带 A 的标记 > 其它。"""
    ann_type = str(code_row.get("ann_type") or "").upper()
    if any(flag in ann_type for flag in ("SHA", "KCB", "CYB", "BJA")):
        return 0
    if "A" in ann_type:
        return 1
    return 2


def normalize_announcement_rows(payload: Dict[str, Any], event_kind: str) -> List[Dict[str, Any]]:
    """把东财列表接口的一条公告摊平成一行（一只股票一行；多代码公告取第一个 A股）。"""
    rows: List[Dict[str, Any]] = []
    for item in payload.get("list") or []:
        art_code = _clean_text(item.get("art_code"))
        ann_date = _parse_notice_date(item.get("notice_date"))
        if not art_code or not ann_date:
            continue
        codes = [code for code in (item.get("codes") or []) if _clean_text(code.get("stock_code"))]
        if not codes:
            continue
        candidates = []
        for code_row in codes:
            stock_code = _clean_text(code_row.get("stock_code")) or ""
            if not _is_a_share_code(stock_code):
                continue
            market = _market_of(code_row) or (code_to_ts_code(stock_code) or "").split(".")[-1]
            if market not in ("SH", "SZ", "BJ"):
                continue
            candidates.append((_a_share_rank(code_row), code_row, market, stock_code))
        if not candidates:
            continue
        _, chosen, market, stock_code = min(candidates, key=lambda item: item[0])
        ts_code = f"{stock_code}.{market}"
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", ts_code):
            continue
        columns = item.get("columns") or []
        kind = _event_kind_of(columns, _clean_text(item.get("title")), event_kind)
        if kind is None:
            continue
        first_column = columns[0] if columns else {}
        rows.append({
            "art_code": art_code,
            "ts_code": ts_code,
            "name": _clean_text(chosen.get("short_name")) or _clean_text(item.get("short_name")),
            "ann_date": ann_date,
            "disclose_at": _parse_display_time(item.get("display_time")),
            "event_kind": kind,
            "category_code": _clean_text(first_column.get("column_code")),
            "category_name": _clean_text(first_column.get("column_name")),
            "title": _clean_text(item.get("title")),
            "source": "eastmoney",
        })
    return rows


def _request_page(
    session: requests.Session,
    begin: date,
    end: date,
    node: int,
    page_index: int,
    *,
    log_prefix: str = "",
) -> Dict[str, Any]:
    params = {
        "page_size": PAGE_SIZE,
        "page_index": page_index,
        "ann_type": "A",
        "client_source": "web",
        "f_node": 1,
        "s_node": node,
        "begin_time": begin.isoformat(),
        "end_time": end.isoformat(),
    }
    response = session.get(ANNOUNCEMENT_URL, params=params, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise AnnouncementDataError(f"{log_prefix}东方财富公告接口返回失败: {payload.get('message')}")
    return payload.get("data") or {}


def fetch_announcements(
    begin: date,
    end: date,
    *,
    session: Optional[requests.Session] = None,
    nodes: Sequence[int] = tuple(NODE_EVENT_KINDS),
) -> pd.DataFrame:
    """按公告日期区间拉取业绩类公告（预告/快报/定期报告），带精确披露时刻。"""
    if begin > end:
        return pd.DataFrame(columns=ANNOUNCEMENT_COLUMNS)
    owns_session = session is None
    session = session or requests.Session()
    rows: List[Dict[str, Any]] = []
    try:
        for node in nodes:
            event_kind = NODE_EVENT_KINDS.get(node)
            if event_kind is None:
                continue
            page_index = 1
            fetched_for_node = 0
            while page_index <= MAX_PAGES_PER_NODE:
                data = _request_page(
                    session, begin, end, node, page_index,
                    log_prefix=f"s_node={node} page={page_index}: ",
                )
                items = data.get("list") or []
                rows.extend(normalize_announcement_rows(data, event_kind))
                total = int(data.get("total_hits") or 0)
                fetched_for_node += len(items)
                if not items or len(items) < PAGE_SIZE or fetched_for_node >= total:
                    break
                page_index += 1
                time.sleep(PAGE_SLEEP_SECONDS)
    finally:
        if owns_session:
            session.close()
    if not rows:
        return pd.DataFrame(columns=ANNOUNCEMENT_COLUMNS)
    frame = pd.DataFrame(rows)
    # 同一 art_code 可能命中多个节点，保留先出现的
    frame = frame.drop_duplicates(subset=["art_code"], keep="first")
    return frame.reset_index(drop=True)


def _upsert_announcements(frame: pd.DataFrame) -> int:
    if frame is None or frame.empty:
        return 0
    insert_frame = frame.loc[:, list(ANNOUNCEMENT_COLUMNS)].copy()
    now = datetime.now()
    insert_frame["created_at"] = now
    insert_frame["updated_at"] = now
    columns = [*ANNOUNCEMENT_COLUMNS, "created_at", "updated_at"]
    quoted_table = f'"{ANNOUNCEMENT_TABLE}"'
    quoted_columns = ", ".join(f'"{column}"' for column in columns)
    connection = connect_duckdb_for_write(ANALYTICS_DB_PATH)
    try:
        connection.execute("BEGIN TRANSACTION")
        connection.register("a_stock_announcement_insert", insert_frame.loc[:, columns])
        connection.execute(
            f"INSERT OR REPLACE INTO {quoted_table} ({quoted_columns}) "
            f"SELECT {quoted_columns} FROM a_stock_announcement_insert"
        )
        connection.execute("COMMIT")
    except Exception:
        try:
            connection.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        connection.close()
    return len(insert_frame)


def _latest_stored_ann_date() -> Optional[date]:
    """库里已入库的最新公告日期；没有表/没有数据时返回 None。"""
    try:
        from ..duckdb_utils import connect_duckdb

        connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    except Exception as exc:  # 分析库还没建好
        logger.warning("读取已入库公告日期失败，本轮按回看窗口拉取: %s", exc)
        return None
    try:
        row = connection.execute(
            f"SELECT MAX(ann_date) FROM {ANNOUNCEMENT_TABLE}"
        ).fetchone()
    except Exception:
        return None
    finally:
        connection.close()
    if not row or row[0] is None:
        return None
    value = row[0]
    return value if isinstance(value, date) else pd.Timestamp(value).date()


def sync_a_stock_announcements(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    *,
    lookback_days: int = 7,
    lookahead_days: int = 1,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """同步业绩类公告及其精确披露时刻。

    增量窗口：往前到 ``min(今天-lookback_days, 库里最新公告日-1)``，往后多取 ``lookahead_days`` 天。
    往前回看是为了接住更正/补充公告，按库里最新公告日自适应是为了长假或任务中断后能自动续上；
    往后多取一天是因为**晚间披露的公告 notice_date 就是次日**——当晚 20:00 跑的时候，
    当天刚发的公告在接口里的日期是明天。
    """
    today = date.today()
    end = end_date or (today + timedelta(days=lookahead_days))
    if start_date is not None:
        begin = start_date
    else:
        begin = today - timedelta(days=lookback_days)
        stored = _latest_stored_ann_date()
        if stored is not None:
            begin = min(begin, stored - timedelta(days=1))
        # 兜个下限，避免表里是很久以前的数据时一次拉爆接口
        begin = max(begin, today - timedelta(days=MAX_LOOKBACK_DAYS))
    started = time.monotonic()
    frame = fetch_announcements(begin, end, session=session)
    saved = _upsert_announcements(frame)
    kinds: Dict[str, int] = {}
    if not frame.empty:
        for kind in (EVENT_KIND_FORECAST, EVENT_KIND_EXPRESS, EVENT_KIND_REPORT):
            kinds[kind] = int((frame["event_kind"] == kind).sum())
    result = {
        "status": "ok",
        "start_date": begin.isoformat(),
        "end_date": end.isoformat(),
        "fetched_rows": int(len(frame)),
        "saved_rows": saved,
        "kinds": kinds,
        "seconds": round(time.monotonic() - started, 1),
    }
    logger.info("业绩公告流 sync done: %s", result)
    return result


def load_disclosure_times(
    connection,
    since: date,
    *,
    kinds: Iterable[str] = (EVENT_KIND_FORECAST, EVENT_KIND_EXPRESS, EVENT_KIND_REPORT),
) -> Dict[Tuple[str, date, str], datetime]:
    """读 ``(ts_code, ann_date, event_kind) -> 最早精确披露时刻``。

    同一天同一只股票可能有多条同类公告（正文/摘要/更正），取最早那条——市场最早看到
    的时间才是跳空的起点。
    """
    exists = connection.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
        [ANNOUNCEMENT_TABLE],
    ).fetchone()
    if not exists or not exists[0]:
        return {}
    kinds = [kind for kind in kinds if kind]
    if not kinds:
        return {}
    placeholders = ", ".join("?" for _ in kinds)
    frame = connection.execute(
        f"""
        SELECT ts_code, ann_date, event_kind, MIN(disclose_at) AS disclose_at
        FROM {ANNOUNCEMENT_TABLE}
        WHERE ann_date >= ? AND event_kind IN ({placeholders}) AND disclose_at IS NOT NULL
        GROUP BY ts_code, ann_date, event_kind
        """,
        [since, *kinds],
    ).fetchdf()
    result: Dict[Tuple[str, date, str], datetime] = {}
    for row in frame.itertuples(index=False):
        if row.disclose_at is None or pd.isna(row.disclose_at):
            continue
        ts_code = str(row.ts_code).strip().upper()
        ann_date = row.ann_date
        if isinstance(ann_date, datetime):  # pd.Timestamp 也是 datetime
            ann_date = ann_date.date()
        elif not isinstance(ann_date, date):
            ann_date = pd.Timestamp(ann_date).date()
        result[(ts_code, ann_date, str(row.event_kind))] = pd.Timestamp(row.disclose_at).to_pydatetime()
    return result
