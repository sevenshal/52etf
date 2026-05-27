import asyncio
import logging
import threading
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)


@dataclass
class EventConnection:
    connection_id: str
    account_id: str
    queue: asyncio.Queue
    loop: asyncio.AbstractEventLoop


class EventBroker:
    def __init__(self):
        self._lock = threading.RLock()
        self._connections: Dict[str, Dict[str, EventConnection]] = defaultdict(dict)

    async def connect(self, account_id: str) -> EventConnection:
        connection = EventConnection(
            connection_id=uuid.uuid4().hex,
            account_id=account_id,
            queue=asyncio.Queue(maxsize=200),
            loop=asyncio.get_running_loop(),
        )
        with self._lock:
            self._connections[account_id][connection.connection_id] = connection
        return connection

    async def disconnect(self, connection: EventConnection) -> None:
        with self._lock:
            account_connections = self._connections.get(connection.account_id)
            if not account_connections:
                return
            account_connections.pop(connection.connection_id, None)
            if not account_connections:
                self._connections.pop(connection.account_id, None)

    def publish(self, account_id: Optional[str], event: Dict[str, Any]) -> None:
        with self._lock:
            if account_id:
                connections: List[EventConnection] = list(self._connections.get(account_id, {}).values())
            else:
                connections = [
                    connection
                    for account_connections in self._connections.values()
                    for connection in account_connections.values()
                ]

        if not connections:
            return

        for connection in connections:
            try:
                connection.loop.call_soon_threadsafe(self._enqueue, connection.queue, event)
            except RuntimeError:
                logger.debug("Skipped backend event for closed loop")

    @staticmethod
    def _enqueue(queue: asyncio.Queue, event: Dict[str, Any]) -> None:
        try:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)
        except Exception as exc:
            logger.warning("Failed to enqueue backend event: %s", exc)


event_broker = EventBroker()


def publish_event(account_id: Optional[str], event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
    event = {
        "type": event_type,
        "pushed_at": datetime.now().isoformat(),
        **(payload or {}),
    }
    event_broker.publish(account_id, event)
