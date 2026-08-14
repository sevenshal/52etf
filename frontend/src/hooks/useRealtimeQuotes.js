import { useCallback, useEffect, useRef, useState } from 'react';
import { subscribeBackendEvent, sendBackendEventMessage } from '../utils/backendEvents';

/**
 * 实时行情（tick 级）hook。
 *
 * - 订阅 `realtime_quotes` 事件，维护 code -> quote 的内存 map（code 统一为
 *   .SH/.SZ 格式，与后端实时行情池一致）。
 * - 通过现有长连接发送 `watch_register` / `watch_unregister` 控制消息，注册与
 *   WS 会话绑定：断开时后端自动清理，重连成功后由 `ws_connected` 事件触发重新注册。
 * - 组件卸载时主动 `watch_unregister` 清理本来源。
 *
 * @param source 注册来源标识（同一页面固定一个，后端按 (session, source) 全量替换）
 */
const useRealtimeQuotes = (source = 'realtime_page') => {
  const [quotes, setQuotes] = useState({});
  // code -> 递增序号；每次 last_px 真实变化时 +1，用于前端价格变动闪烁提示
  const [flashes, setFlashes] = useState({});
  const quotesRef = useRef({});
  const flashRef = useRef({});
  const sourceRef = useRef(source);
  sourceRef.current = source;
  const registerRef = useRef(() => {});

  const register = useCallback((codes) => {
    const list = Array.from(new Set((codes || []).filter(Boolean)));
    registerRef.current = () => {
      if (list.length) {
        sendBackendEventMessage({ type: 'watch_register', source: sourceRef.current, codes: list });
      } else {
        sendBackendEventMessage({ type: 'watch_unregister', source: sourceRef.current });
      }
    };
    registerRef.current();
  }, []);

  const unregister = useCallback(() => {
    sendBackendEventMessage({ type: 'watch_unregister', source: sourceRef.current });
  }, []);

  useEffect(() => {
    const unsubQuote = subscribeBackendEvent('realtime_quotes', event => {
      const batch = event.quotes;
      if (!batch || typeof batch !== 'object') return;

      const prev = quotesRef.current;
      const next = { ...prev, ...batch };
      quotesRef.current = next;

      // 检测真实价格变化：仅当该 code 之前已有价格且与最新价不同才触发闪烁
      const changed = {};
      Object.keys(batch).forEach(code => {
        const prevPrice = prev[code] ? prev[code].last_px : null;
        const newPrice = batch[code] ? batch[code].last_px : null;
        if (
          prevPrice !== null && prevPrice !== undefined
          && newPrice !== null && newPrice !== undefined
          && Number(prevPrice) !== Number(newPrice)
        ) {
          changed[code] = (flashRef.current[code] || 0) + 1;
        }
      });

      setQuotes(next);
      if (Object.keys(changed).length) {
        flashRef.current = { ...flashRef.current, ...changed };
        setFlashes({ ...flashRef.current });
      }
    });
    const unsubConnected = subscribeBackendEvent('ws_connected', () => {
      // 长连接重连成功后重新注册当前展示的代码
      registerRef.current?.();
    });
    return () => {
      unsubQuote();
      unsubConnected();
      // 页面/组件卸载：主动清理该来源的注册（不等断线）
      unregister();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unregister]);

  return { quotes, flashes, register, unregister };
};

export default useRealtimeQuotes;
