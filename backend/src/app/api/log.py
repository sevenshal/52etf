import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

router = APIRouter()

@router.websocket("/ws/log")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        log_file = "/var/log/quant/app.log"
        print(f"Attempting to open and tail log file: {log_file}")
        with open(log_file, "r") as f:
            f.seek(0, 2)  # Go to the end of the file
            while True:
                line = f.readline()
                if line:
                    try:
                        await websocket.send_text(line)
                    except ConnectionClosed:
                        print("Client connection closed, stopping log tail.")
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
        await websocket.close()