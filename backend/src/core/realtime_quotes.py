"""统一实时行情能力：内存股票池注册表 + 最新 tick 快照缓存。

设计约定（v4 计划）：
- 注册表**纯内存**，不落 SQLite；行情快照只进内存（tick 3s/池 300 只，写库违反短事务铁律）。
- 注册/清理与长连接（WebSocket 会话）绑定：前端通过 /api/events/ws 发 watch_register /
  watch_unregister，断线时按 connection_id 整体清理。
- 池 = 所有会话注册代码的并集，全局最多 max_pool_size（默认 300）只，超限按
  (session, source) 最后活跃时间 LRU 淘汰。
- PTrade 侧为纯跟随者：POST /api/realtime/pool 上报报价、响应携带最新池 + pool_version。
"""

import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

MAX_POOL_SIZE = 300
PTRADE_BRIDGE_ACCOUNT_ID = "ptrade-bridge"


def normalize_code(value: Any) -> str:
    """统一股票代码格式（与系统其它模块一致：沪 .SH / 深 .SZ / 北 .BJ）。

    PTrade 上报的是 .SS/.SZ，前端注册的是 .SH/.SZ，统一到 .SH/.SZ 避免同一
    股票在池里出现两次。
    """
    raw = str(value or "").strip().upper()
    if raw.endswith(".SS"):
        raw = raw[:-3] + ".SH"
    if raw.startswith(("SH", "SZ", "BJ")) and len(raw) == 8:
        raw = f"{raw[2:]}.{raw[:2]}"
    if "." not in raw and raw.isdigit() and len(raw) == 6:
        if raw.startswith(("6", "9")):
            raw = f"{raw}.SH"
        elif raw.startswith(("4", "8")):
            raw = f"{raw}.BJ"
        else:
            raw = f"{raw}.SZ"
    return raw


class RealtimeQuoteManager:
    def __init__(self, max_pool_size: int = MAX_POOL_SIZE):
        self.max_pool_size = max(1, int(max_pool_size))
        self._lock = threading.RLock()
        # session_id -> {source -> {ts_code: last_touch_epoch}}
        self._registry: Dict[str, Dict[str, Dict[str, float]]] = {}
        # ts_code -> latest tick quote dict
        self._quotes: Dict[str, Dict[str, Any]] = {}
        self._pool_version = 0

    # ------------------------------------------------------------------ #
    # 注册 / 清理（事件循环线程调用，断线在 finally 里清整个 session）
    # ------------------------------------------------------------------ #
    def register(self, session_id: str, source: str, codes: List[str]) -> None:
        """以 (session, source) 为单位全量替换代码集；空列表等价于清空该 source。"""
        if not session_id or not source:
            return
        mapped: Dict[str, float] = {}
        now = time.time()
        for code in codes or []:
            normalized = normalize_code(code)
            if normalized:
                mapped[normalized] = now
        with self._lock:
            before = self._pool()
            sessions = self._registry.setdefault(session_id, {})
            if mapped:
                sessions[source] = mapped
            else:
                sessions.pop(source, None)
                if not sessions:
                    self._registry.pop(session_id, None)
            self._evict_lru_locked()
            self._bump_version_if_changed(before)

    def unregister(self, session_id: str, source: str, codes: Optional[List[str]] = None) -> None:
        """移除指定代码；codes 省略则清掉该 (session, source) 全部。"""
        if not session_id or not source:
            return
        with self._lock:
            before = self._pool()
            sessions = self._registry.get(session_id)
            if not sessions:
                return
            src = sessions.get(source)
            if not src:
                return
            if codes is None:
                sessions.pop(source, None)
            else:
                for code in codes:
                    src.pop(normalize_code(code), None)
                if not src:
                    sessions.pop(source, None)
            if not sessions:
                self._registry.pop(session_id, None)
            self._bump_version_if_changed(before)

    def clear_session(self, session_id: str) -> None:
        """长连接断开：清空该会话注册的所有代码。"""
        with self._lock:
            before = self._pool()
            if self._registry.pop(session_id, None) is not None:
                self._bump_version_if_changed(before)

    # ------------------------------------------------------------------ #
    # PTrade 上报（线程安全，API 请求线程调用）
    # ------------------------------------------------------------------ #
    def update_quotes(self, quotes: Dict[str, Dict[str, Any]]) -> None:
        """合并最新 tick 快照到内存缓存（不落库、不广播，广播由调用方负责）。"""
        if not quotes:
            return
        now = datetime.now().isoformat()
        with self._lock:
            for code, quote in quotes.items():
                normalized = normalize_code(code)
                if not normalized:
                    continue
                merged = dict(quote or {})
                merged["updated_at"] = now
                self._quotes[normalized] = merged

    def quote(self, code: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._quotes.get(normalize_code(code))

    # ------------------------------------------------------------------ #
    # 只读访问器
    # ------------------------------------------------------------------ #
    def pool(self) -> List[str]:
        with self._lock:
            return sorted(self._pool())

    @property
    def pool_version(self) -> int:
        with self._lock:
            return self._pool_version

    def status(self) -> Dict[str, Any]:
        with self._lock:
            pool = self._pool()
            return {
                "pool": pool,
                "pool_size": len(pool),
                "max_pool_size": self.max_pool_size,
                "pool_version": self._pool_version,
                "quote_count": len(self._quotes),
                "sessions": {sid: {src: sorted(c) for src, c in srcs.items()} for sid, srcs in self._registry.items()},
            }

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #
    def _pool(self) -> Set[str]:
        codes: Set[str] = set()
        for sessions in self._registry.values():
            for src in sessions.values():
                codes.update(src.keys())
        return codes

    def _total_size(self) -> int:
        return sum(len(src) for sessions in self._registry.values() for src in sessions.values())

    def _bump_version_if_changed(self, before: Set[str]) -> None:
        after = self._pool()
        if after != before:
            self._pool_version += 1
            self._prune_quotes(after)

    def _prune_quotes(self, pool: Set[str]) -> None:
        if self._quotes:
            self._quotes = {code: quote for code, quote in self._quotes.items() if code in pool}

    def _evict_lru_locked(self) -> None:
        """超限时先裁剪单个超大 source，再按 (session, source) 最后活跃时间淘汰最旧条目。"""
        if self._total_size() <= self.max_pool_size:
            return
        # 1) 单个 source 独自超限：source 内部按注册时间裁到上限（保留最新）
        for sid, sessions in self._registry.items():
            for source, codes in sessions.items():
                while len(codes) > self.max_pool_size:
                    oldest = min(codes, key=codes.get)
                    del codes[oldest]
        # 2) 仍超限：整体淘汰最旧的 (session, source)
        if self._total_size() <= self.max_pool_size:
            return
        entries: List[Tuple[float, str, str]] = []
        for sid, sessions in self._registry.items():
            for source, codes in sessions.items():
                touch = max(codes.values()) if codes else 0.0
                entries.append((touch, sid, source))
        entries.sort(key=lambda item: item[0])
        for _touch, sid, source in entries:
            if self._total_size() <= self.max_pool_size:
                break
            del self._registry[sid][source]
            if not self._registry[sid]:
                del self._registry[sid]


realtime_quotes = RealtimeQuoteManager()
