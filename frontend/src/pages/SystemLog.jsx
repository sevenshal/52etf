import React, { useState, useEffect, useRef } from 'react';
import { Card } from 'antd';

const SystemLog = () => {
  const [logs, setLogs] = useState([]);
  const logsEndRef = useRef(null);

  const scrollToBottom = () => {
    logsEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }

  useEffect(() => {
    const ws = new WebSocket("wss://api.52etf.vip/ws/log");

    ws.onmessage = (event) => {
      setLogs(prevLogs => [...prevLogs, event.data]);
    };

    ws.onclose = () => {
      setLogs(prevLogs => [...prevLogs, "Connection closed"]);
    };

    ws.onerror = (error) => {
      setLogs(prevLogs => [...prevLogs, `WebSocket error: ${error.message}`]);
    };

    return () => {
      ws.close();
    };
  }, []);

  useEffect(scrollToBottom, [logs]);

  return (
    <Card title="系统日志">
      <pre style={{ 
        backgroundColor: '#000',
        color: '#fff',
        padding: '10px',
        height: '600px',
        overflowY: 'scroll' 
      }}>
        {logs.map((log, index) => (
          <div key={index}>{log}</div>
        ))}
        <div ref={logsEndRef} />
      </pre>
    </Card>
  );
};

export default SystemLog;