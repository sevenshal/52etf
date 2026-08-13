const listeners = new Map();

let socket = null;
let socketAccountId = null;
let reconnectTimer = null;
let reconnectAttempt = 0;
let shouldReconnect = true;

const getAccountId = () => localStorage.getItem('accountId');

const buildEventsWsUrl = (accountId) => {
  const apiUrl = (process.env.REACT_APP_API_URL || '').replace(/\/$/, '');
  const wsHost = apiUrl
    ? apiUrl.replace(/^http/, 'ws')
    : `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}`;
  return `${wsHost}/api/events/ws?account_id=${encodeURIComponent(accountId)}`;
};

const hasListeners = () => Array.from(listeners.values()).some(set => set.size > 0);

const dispatchEvent = (event) => {
  const typeListeners = listeners.get(event.type);
  if (typeListeners) {
    typeListeners.forEach(handler => {
      try {
        handler(event);
      } catch (error) {
        console.warn('Backend event handler failed', error);
      }
    });
  }

  const wildcardListeners = listeners.get('*');
  if (wildcardListeners) {
    wildcardListeners.forEach(handler => {
      try {
        handler(event);
      } catch (error) {
        console.warn('Backend event handler failed', error);
      }
    });
  }
};

const clearReconnectTimer = () => {
  if (reconnectTimer) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
};

const closeSocket = () => {
  clearReconnectTimer();
  if (socket) {
    socket.onopen = null;
    socket.onmessage = null;
    socket.onclose = null;
    socket.onerror = null;
    socket.close();
    socket = null;
  }
  socketAccountId = null;
};

const scheduleReconnect = () => {
  if (!shouldReconnect || reconnectTimer || !hasListeners()) {
    return;
  }
  const delay = Math.min(30000, 1000 * (2 ** reconnectAttempt));
  reconnectAttempt += 1;
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null;
    ensureBackendEventsConnection();
  }, delay);
};

const ensureBackendEventsConnection = () => {
  const accountId = getAccountId();
  if (!accountId || !hasListeners()) {
    closeSocket();
    return;
  }

  if (
    socket
    && socketAccountId === accountId
    && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)
  ) {
    return;
  }

  shouldReconnect = true;
  closeSocket();
  socketAccountId = accountId;
  socket = new WebSocket(buildEventsWsUrl(accountId));

  socket.onopen = () => {
    reconnectAttempt = 0;
    // 通知各页面重连成功，便于重新注册实时行情股票池等会话级状态
    dispatchEvent({ type: 'ws_connected', pushed_at: new Date().toISOString() });
  };

  socket.onmessage = (message) => {
    try {
      const event = JSON.parse(message.data);
      if (event?.type && event.type !== 'heartbeat' && event.type !== 'connected') {
        dispatchEvent(event);
      }
    } catch (error) {
      console.warn('Failed to parse backend event', error);
    }
  };

  socket.onclose = () => {
    socket = null;
    socketAccountId = null;
    scheduleReconnect();
  };

  socket.onerror = () => {
    socket?.close();
  };
};

export const sendBackendEventMessage = (msg) => {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(msg));
    return true;
  }
  return false;
};

export const subscribeBackendEvent = (type, handler) => {
  if (!listeners.has(type)) {
    listeners.set(type, new Set());
  }
  listeners.get(type).add(handler);
  shouldReconnect = true;
  ensureBackendEventsConnection();

  return () => {
    const typeListeners = listeners.get(type);
    if (typeListeners) {
      typeListeners.delete(handler);
      if (typeListeners.size === 0) {
        listeners.delete(type);
      }
    }

    if (!hasListeners()) {
      shouldReconnect = false;
      closeSocket();
    }
  };
};
