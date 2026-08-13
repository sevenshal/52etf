"""因子实验室批量搜参：验证并行执行（并发数 = 可用 CPU 的 2/3）与取消语义。

回归背景：搜参循环原来写死 worker_count=1（串行执行），本次改为
ThreadPoolExecutor 并行执行，每个 worker 使用独立 DB 会话。
"""
import threading
import time
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from src.app.api import factor_lab


def _make_job(total_cases: int = 12) -> dict:
    return {
        "account_id": "test",
        "status": "queued",
        "created_at": None,
        "started_at": None,
        "finished_at": None,
        "updated_at": None,
        "objective": "annualized_return",
        "request_payload": {},
        "search_params": {},
        "total_cases": total_cases,
        "submitted_cases": 0,
        "completed_cases": 0,
        "failed_cases": 0,
        "result_count": 0,
        "worker_count": 1,
        "current_case": None,
        "error": None,
        "cancel_requested": False,
        "available_cpu_cores": 8,
    }


def _make_search_request() -> SimpleNamespace:
    return SimpleNamespace(
        request=SimpleNamespace(legs=[object()]),
        objective="annualized_return",
    )


def _fake_cases(total_cases: int):
    return iter((SimpleNamespace(legs=[object()]), []) for _ in range(total_cases))


class FactorBacktestSearchParallelTest(TestCase):
    def _run_job(self, job: dict, fake_case, total_cases: int = 12):
        patches = [
            patch.object(factor_lab, "_effective_cpu_count", return_value=8),
            patch.object(factor_lab, "_resolve_factor_legs", return_value=[object()]),
            patch.object(factor_lab, "_prepare_factor_backtest_base_data", return_value={}),
            patch.object(factor_lab, "_warm_backtest_search_factor_caches", return_value=None),
            patch.object(factor_lab, "_format_backtest_search_params", return_value="case"),
            patch.object(factor_lab, "_iter_backtest_search_requests", return_value=_fake_cases(total_cases)),
            patch.object(factor_lab, "_run_backtest_search_case", side_effect=fake_case),
            patch.object(factor_lab, "_insert_backtest_search_result", return_value=None),
            patch.object(factor_lab, "_persist_active_backtest_search_job", return_value=None),
            patch.object(factor_lab, "_persist_backtest_search_job", return_value=None),
            patch.object(factor_lab, "_publish_backtest_search_job", return_value=None),
            patch.object(factor_lab, "_snapshot_backtest_search_job", side_effect=lambda j: dict(j)),
        ]
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            factor_lab._run_backtest_search_job(_make_search_request(), job)

    def test_parallel_search_runs_all_cases_with_cpu_two_thirds_workers(self):
        job = _make_job(total_cases=12)
        active = {"value": 0}
        max_active = {"value": 0}
        lock = threading.Lock()

        def fake_case(index, case_request, legs, objective, prepared_data):
            with lock:
                active["value"] += 1
                max_active["value"] = max(max_active["value"], active["value"])
            time.sleep(0.02)
            with lock:
                active["value"] -= 1
            return {"case_index": index}

        self._run_job(job, fake_case)

        self.assertEqual("completed", job["status"])
        self.assertEqual(12, job["submitted_cases"])
        self.assertEqual(12, job["completed_cases"])
        self.assertEqual(12, job["result_count"])
        self.assertEqual(0, job["failed_cases"])
        # 8 核可用 → 并发数 = int(8 * 2 / 3) = 5
        self.assertEqual(5, job["worker_count"])
        # 必须真正并行执行（串行时最大同时执行数恒为 1）
        self.assertGreater(max_active["value"], 1)

    def test_worker_count_min_one(self):
        with patch.object(factor_lab, "_effective_cpu_count", return_value=1):
            self.assertEqual(1, factor_lab._backtest_search_worker_count())
        with patch.object(factor_lab, "_effective_cpu_count", return_value=2):
            self.assertEqual(1, factor_lab._backtest_search_worker_count())
        with patch.object(factor_lab, "_effective_cpu_count", return_value=4):
            self.assertEqual(2, factor_lab._backtest_search_worker_count())

    def test_cancel_stops_submitting_new_cases(self):
        job = _make_job(total_cases=12)
        counter = {"n": 0}
        cancel_fired = {"fired": False}
        lock = threading.Lock()

        def fake_case(index, case_request, legs, objective, prepared_data):
            with lock:
                counter["n"] += 1
            time.sleep(0.01)
            if counter["n"] >= 3 and not cancel_fired["fired"]:
                cancel_fired["fired"] = True
                job["cancel_requested"] = True
            return {"case_index": index}

        self._run_job(job, fake_case)

        self.assertEqual("cancelled", job["status"])
        self.assertLess(job["completed_cases"], job["total_cases"])
        self.assertEqual(job["completed_cases"], job["result_count"])
        self.assertEqual(0, job["failed_cases"])
        self.assertFalse(job["cancel_requested"])
