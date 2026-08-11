import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_api_imports_from_src_only_distribution(tmp_path):
    """Production deploys contain src but intentionally omit lab."""
    repo_root = Path(__file__).resolve().parents[1]
    shutil.copytree(repo_root / "src", tmp_path / "src")
    env = os.environ.copy()
    env["QUANT_SQLITE_PATH"] = str(tmp_path / "app.db")
    env["ANALYTICS_DB_PATH"] = str(tmp_path / "analytics.duckdb")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from src.app.api.a_stock_fear_etf_backtest import "
                "RunRequest, _benchmark_payload, options, router; "
                "assert any("
                "r.path == '/api/a-stock-fear-etf-backtest/run' "
                "for r in router.routes); "
                "data = options('local-test'); "
                "low_vol = next("
                "item for item in data['targets'] "
                "if item['index_symbol'] == 'H30269.CSI'); "
                "assert low_vol['etf_symbol'] == '512890.SH'; "
                "assert RunRequest(benchmark_symbol='h30269.csi').benchmark_symbol == 'H30269.CSI'; "
                "assert RunRequest().benchmark_symbol == '000300.SH'; "
                "assert _benchmark_payload('H30269.CSI', 'etf_proxy')['price_symbol'] == '512890.SH'"
            ),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert not (tmp_path / "lab").exists()
