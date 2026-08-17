"""系统信息接口：返回后端服务所在机器的 CPU / 内存 / 磁盘 / 温度 / 进程等状态。"""
import logging
import os
import platform
import socket
import sys
import time
from typing import List, Optional

from fastapi import APIRouter, Depends

from .account import valid_admin_account

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/system", tags=["System Info"])

# 除普通挂载点外额外关注的路径（生产数据目录等，存在时单独列出）
EXTRA_DISK_PATHS = ("/var/lib/quant_robot", "/var/log/quant")

# 这些文件系统的分区不展示（虚拟/临时文件系统）
SKIP_FSTYPES = {"", "tmpfs", "devtmpfs", "overlay", "squashfs", "ramfs", "iso9660"}


def _read_cpu_temperature() -> Optional[dict]:
    """读取 CPU 温度（摄氏度）。

    Linux 下读取 /sys/class/thermal/thermal_zone*/temp（x86_pkg_temp / coretemp / k10temp 等），
    其他平台或读取失败返回 None。返回最高温 zone 及全部 zone 明细。
    """
    if sys.platform != "linux":
        return None
    zones = []
    thermal_dir = "/sys/class/thermal"
    try:
        names = sorted(os.listdir(thermal_dir))
    except OSError:
        return None
    for name in names:
        if not name.startswith("thermal_zone"):
            continue
        zone_dir = os.path.join(thermal_dir, name)
        try:
            with open(os.path.join(zone_dir, "type"), "r") as f:
                zone_type = f.read().strip()
            with open(os.path.join(zone_dir, "temp"), "r") as f:
                raw = int(f.read().strip())
            zones.append({"zone": name, "type": zone_type, "temperature_c": raw / 1000.0})
        except (OSError, ValueError):
            continue
    if not zones:
        return None
    hottest = max(zones, key=lambda z: z["temperature_c"])
    return {"temperature_c": hottest["temperature_c"], "zones": zones}


def _mount_device(mountpoint: str) -> Optional[int]:
    """返回挂载点所在分区的设备号（用于同分区去重）。"""
    try:
        return os.stat(mountpoint).st_dev
    except OSError:
        return None


def _collect_disk_usage() -> List[dict]:
    """收集主要磁盘分区及重点关注路径的使用情况。"""
    if psutil is None:
        return []
    disks: List[dict] = []
    seen = set()
    seen_devices = set()
    for part in psutil.disk_partitions(all=False):
        fstype = part.fstype or ""
        if fstype in SKIP_FSTYPES:
            continue
        if part.device.startswith(("/dev/loop", "loop", "snap")):
            continue
        if part.mountpoint in seen:
            continue
        seen.add(part.mountpoint)
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        st_dev = _mount_device(part.mountpoint)
        if st_dev is not None:
            seen_devices.add(st_dev)
        disks.append({
            "mountpoint": part.mountpoint,
            "device": part.device,
            "fstype": fstype,
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": usage.percent,
        })
    # 补充关注路径（未被分区覆盖且存在时；与已列出分区同设备则跳过，避免重复）
    for path in EXTRA_DISK_PATHS:
        if path in seen or not os.path.exists(path):
            continue
        st_dev = _mount_device(path)
        if st_dev is not None and st_dev in seen_devices:
            continue
        try:
            usage = psutil.disk_usage(path)
        except OSError:
            continue
        if st_dev is not None:
            seen_devices.add(st_dev)
        disks.append({
            "mountpoint": path,
            "device": "-",
            "fstype": "-",
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": usage.percent,
        })
        seen.add(path)
    # 根分区排最前
    disks.sort(key=lambda d: (d["mountpoint"] != "/", d["mountpoint"]))
    return disks


@router.get("/info")
def get_system_info(_account_id: str = Depends(valid_admin_account)):
    """返回后端服务所在机器的系统信息（CPU/内存/磁盘/温度/进程等）。"""
    info = {
        "collected_at": int(time.time()),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "psutil_available": psutil is not None,
    }
    if psutil is None:
        return info

    # CPU（含 0.1s 采样，得到真实使用率）
    try:
        cpu_percent = psutil.cpu_percent(interval=0.1)
        cpu_freq = psutil.cpu_freq()
        info["cpu"] = {
            "percent": cpu_percent,
            "count_physical": psutil.cpu_count(logical=False) or 0,
            "count_logical": psutil.cpu_count(logical=True) or 0,
            "load_avg": [round(x, 2) for x in psutil.getloadavg()],
            "frequency_mhz": round(cpu_freq.current, 1) if cpu_freq else None,
        }
    except Exception as e:
        logger.warning("Failed to collect CPU info: %s", e)
        info["cpu"] = {"error": str(e)}

    try:
        info["cpu"]["temperature"] = _read_cpu_temperature()
    except Exception as e:
        logger.warning("Failed to read CPU temperature: %s", e)
        info["cpu"]["temperature"] = None

    # 内存 / Swap
    try:
        vm = psutil.virtual_memory()
        info["memory"] = {
            "total": vm.total,
            "available": vm.available,
            "used": vm.used,
            "free": vm.free,
            "percent": vm.percent,
        }
        sm = psutil.swap_memory()
        info["swap"] = {
            "total": sm.total,
            "used": sm.used,
            "free": sm.free,
            "percent": sm.percent,
        }
    except Exception as e:
        logger.warning("Failed to collect memory info: %s", e)
        info["memory"] = {"error": str(e)}

    # 磁盘
    try:
        info["disks"] = _collect_disk_usage()
    except Exception as e:
        logger.warning("Failed to collect disk info: %s", e)
        info["disks"] = []

    # 系统运行时间
    try:
        boot_time = int(psutil.boot_time())
        info["boot_time"] = boot_time
        info["uptime_seconds"] = max(0, int(time.time() - boot_time))
    except Exception:
        pass

    # 后端进程信息
    try:
        proc = psutil.Process(os.getpid())
        proc.cpu_percent(interval=0.1)
        with proc.oneshot():
            info["process"] = {
                "pid": proc.pid,
                "name": proc.name(),
                "cmdline": " ".join(proc.cmdline() or []),
                "cpu_percent": round(proc.cpu_percent(interval=None), 2),
                "memory_percent": round(proc.memory_percent(), 2),
                "memory_rss": proc.memory_info().rss,
                "num_threads": proc.num_threads(),
                "start_time": int(proc.create_time()),
                "username": proc.username(),
            }
    except Exception as e:
        logger.warning("Failed to collect process info: %s", e)
        info["process"] = {"error": str(e)}

    return info
