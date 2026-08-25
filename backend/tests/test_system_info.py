from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.app.api import system_info


def test_collect_process_info_uses_blocking_cpu_sample(monkeypatch):
    proc = MagicMock()
    proc.pid = 1234
    proc.cpu_percent.return_value = 710.3
    proc.oneshot.return_value = nullcontext()
    proc.name.return_value = "python3.12"
    proc.cmdline.return_value = ["python3.12", "backend"]
    proc.memory_percent.return_value = 3.2
    proc.memory_info.return_value = SimpleNamespace(rss=984_508)
    proc.num_threads.return_value = 8
    proc.create_time.return_value = 1_700_000_000.9
    proc.username.return_value = "quantd"
    monkeypatch.setattr(system_info.psutil, "Process", lambda _pid: proc)

    result = system_info._collect_process_info()

    assert result["cpu_percent"] == 710.3
    proc.cpu_percent.assert_called_once_with(interval=0.1)
