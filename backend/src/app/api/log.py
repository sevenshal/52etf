import asyncio
import os
from typing import Optional, Tuple

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.websockets import WebSocketState

router = APIRouter()

DEFAULT_LOG_FILE_KEY = "service"
LOG_FILES = {
    "service": "/var/log/quant/service.log",
    "error": "/var/log/quant/error.log",
}
LOG_FILE_ALIASES = {
    "service.log": "service",
    "error.log": "error",
}


def _resolve_log_file(log_file_key: Optional[str] = None) -> Tuple[str, str]:
    normalized_key = (log_file_key or DEFAULT_LOG_FILE_KEY).strip().lower()
    normalized_key = LOG_FILE_ALIASES.get(normalized_key, normalized_key)
    if normalized_key not in LOG_FILES:
        raise ValueError(f"Unsupported log file: {log_file_key}")
    return normalized_key, LOG_FILES[normalized_key]


def _read_last_lines(path: str, num_lines: int = 10, chunk_size: int = 8192):
    """Read the last `num_lines` lines from a file efficiently."""
    try:
        lines = []
        buffer = b""
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            while pos > 0 and len(lines) <= num_lines:
                read_size = min(chunk_size, pos)
                pos -= read_size
                f.seek(pos)
                data = f.read(read_size)
                buffer = data + buffer
                lines = buffer.splitlines()
        if not lines:
            return []
        return [line.decode("utf-8", errors="replace") for line in lines[-num_lines:]]
    except Exception as e:
        print(f"Error reading last lines: {e}")
        return []


@router.websocket("/ws/log")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    log_file_key = websocket.query_params.get("file", DEFAULT_LOG_FILE_KEY)
    try:
        resolved_log_key, log_file = _resolve_log_file(log_file_key)
    except ValueError:
        available_logs = ", ".join(sorted(LOG_FILES.keys()))
        await websocket.send_text(f"Unsupported log file: {log_file_key}. Available: {available_logs}")
        await websocket.close(code=1008)
        return

    try:
        print(f"Attempting to open and tail log file: {resolved_log_key} ({log_file})")
        # Send last 10 lines immediately upon connection
        for initial_line in _read_last_lines(log_file, num_lines=10):
            if initial_line:
                try:
                    await websocket.send_text(initial_line)
                except WebSocketDisconnect:
                    print("Client disconnected while sending initial lines")
                    return

        # Continue tailing live updates
        with open(log_file, "r") as f:
            f.seek(0, 2)  # Go to the end of the file
            while True:
                line = f.readline()
                if line:
                    try:
                        await websocket.send_text(line)
                    except WebSocketDisconnect:
                        print("Client disconnected, stopping log tail.")
                        break
                else:
                    await asyncio.sleep(0.1)
    except FileNotFoundError:
        error_msg = f"Log file not found: {log_file}"
        print(error_msg)
        await websocket.send_text(error_msg)
    except WebSocketDisconnect:
        print("Client disconnected")
    except Exception as e:
        error_msg = f"An unexpected error occurred: {e}"
        print(error_msg)
        await websocket.send_text(error_msg)
    finally:
        print("Closing WebSocket connection.")
        try:
            if websocket.client_state != WebSocketState.DISCONNECTED:
                await websocket.close()
        except RuntimeError:
            pass
        except Exception:
            pass
