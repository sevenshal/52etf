"""因子实验室批量搜参子进程入口。

主进程（uvicorn）只负责：启动本进程、轮询 SQLite 中的任务状态转发 WS 事件、
接收取消请求（写入 DB 取消标记）。本进程内独立执行全部搜参回测
（内部再用 ThreadPoolExecutor 并行跑各参数组合），CPU 密集计算不会占用
uvicorn 主进程，正常网页请求不受影响。

任务输入：stdin 一行 JSON，含 account_id 与 request（FactorBacktestSearchRequest
的 model_dump 后经 jsonable_encoder 编码）。
进度/结果：全部写入 SQLite（FactorBacktestSearchState / FactorBacktestSearchResult），
由主进程轮询转发事件。
"""
import json
import sys

from ..app.api.factor_lab import (
    FactorBacktestSearchRequest,
    _build_backtest_search_job,
    _get_backtest_search_state,
    _run_backtest_search_job,
)
from ..core.database import Session as DBSession


def _db_cancel_requested() -> bool:
    """取消标记由主进程写入 DB，worker 每轮检查。"""
    db = DBSession()
    try:
        state = _get_backtest_search_state(db)
        return bool(state and state.cancel_requested and state.status in {"queued", "running"})
    finally:
        db.close()
        DBSession.remove()


def _publish_noop(job):  # noqa: ANN001 进度事件由主进程轮询 DB 转发，worker 内不发布
    pass


def main() -> int:
    payload = json.load(sys.stdin)
    search_request = FactorBacktestSearchRequest.model_validate(payload["request"])
    account_id = payload.get("account_id") or "default"
    job = _build_backtest_search_job(search_request, account_id)
    _run_backtest_search_job(
        search_request,
        job,
        cancel_check=_db_cancel_requested,
        publish=_publish_noop,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
