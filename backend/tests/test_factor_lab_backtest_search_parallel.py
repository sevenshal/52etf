"""因子实验室批量搜参：验证并行执行（并发数 = 可用 CPU 的 2/3）与取消语义。

回归背景：搜参循环原来写死 worker_count=1（串行执行），本次改为
ThreadPoolExecutor 并行执行，每个 worker 使用独立 DB 会话。
"""
import json
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from datetime import datetime
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
        window_weight_bucket_count=20,
        factor_weight_bucket_count=20,
        max_positions_candidates=None,
        position_weight_candidates=None,
        sell_rank_multiplier_candidates=None,
        rotation_mode_candidates=None,
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

    def test_build_search_job_shape(self):
        with patch.object(factor_lab, "_effective_cpu_count", return_value=8), \
             patch.object(factor_lab, "_estimate_backtest_search_cases", return_value=12), \
             patch.object(factor_lab, "_backtest_request_payload", return_value={"legs": []}):
            job = factor_lab._build_backtest_search_job(_make_search_request(), "acct-1")
        self.assertEqual("queued", job["status"])
        self.assertEqual("acct-1", job["account_id"])
        self.assertEqual(12, job["total_cases"])
        # 8 核 → 并发数 = int(8 * 2 / 3) = 5
        self.assertEqual(5, job["worker_count"])
        self.assertIn("search_params", job)
        self.assertIn("request_payload", job)
        self.assertEqual(0, job["completed_cases"])

    def test_spawn_builds_worker_command(self):
        search_request = SimpleNamespace(
            request=SimpleNamespace(legs=[object()]),
            objective="annualized_return",
            window_weight_bucket_count=20,
            factor_weight_bucket_count=20,
            max_positions_candidates=None,
            position_weight_candidates=None,
            sell_rank_multiplier_candidates=None,
            rotation_mode_candidates=None,
            model_dump=lambda: {"legs": []},
        )
        with patch.object(factor_lab.subprocess, "Popen") as mock_popen, \
             patch.object(factor_lab, "jsonable_encoder", return_value={"legs": []}), \
             patch.object(factor_lab, "_effective_cpu_count", return_value=8):
            process = factor_lab._spawn_backtest_search_process(search_request, "acct-1")
        self.assertIsNotNone(process)
        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        command = args[0]
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1], "-c")
        self.assertIn("src.scripts.factor_backtest_search_worker", command[2])
        self.assertEqual(subprocess.DEVNULL, kwargs.get("stdout"))
        mock_popen.return_value.stdin.write.assert_called_once()
        payload = json.loads(mock_popen.return_value.stdin.write.call_args[0][0])
        self.assertEqual("acct-1", payload["account_id"])
        self.assertIn("request", payload)
        mock_popen.return_value.stdin.close.assert_called_once()

    def test_relay_forwards_state_and_finalizes(self):
        process = SimpleNamespace()
        poll_results = iter([None, None, 0])  # alive x2，然后退出
        process.poll = lambda: next(poll_results)
        running_snapshot = {
            "account_id": "acct-1",
            "status": "running",
            "updated_at": datetime(2024, 1, 1),
            "submitted_cases": 1,
            "completed_cases": 0,
            "failed_cases": 0,
            "current_case": "case-1",
            "error": None,
            "cancel_requested": False,
            "payload": {"status": "running"},
        }
        fake_state = SimpleNamespace(account_id="acct-1", status="completed")
        with patch.object(factor_lab, "BACKTEST_SEARCH_ACTIVE_PROCESS", process), \
             patch.object(
                 factor_lab,
                 "_snapshot_backtest_search_state",
                 side_effect=[running_snapshot, None] + [None] * 5,
             ), \
             patch.object(factor_lab, "_get_backtest_search_state", return_value=fake_state), \
             patch.object(
                 factor_lab,
                 "_serialize_backtest_search_status_from_record",
                 return_value={"status": "completed"},
             ), \
             patch.object(factor_lab, "publish_event") as mock_pub:
            factor_lab._relay_backtest_search_state(process)
        events = [call.args for call in mock_pub.call_args_list]
        self.assertIn(("acct-1", "factor_backtest_search", {"status": "running"}), events)
        # 进程退出后转发最终 completed 状态
        self.assertIn(("acct-1", "factor_backtest_search", {"status": "completed"}), events)
