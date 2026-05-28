import React, { useState, useEffect, useRef } from 'react';
import { Card, Select, Space } from 'antd';

const LOG_FILE_OPTIONS = [
  { label: 'service.log', value: 'service' },
  { label: 'error.log', value: 'error' }
];

const buildLogWsUrl = (logFile) => {
  const apiUrl = (process.env.REACT_APP_API_URL || '').replace(/\/$/, '');
  const wsHost = apiUrl
    ? apiUrl.replace(/^http/, 'ws')
    : `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}`;

  return `${wsHost}/ws/log?file=${encodeURIComponent(logFile)}`;
};

const SystemLog = () => {
  const [logs, setLogs] = useState([]);
  const [logFile, setLogFile] = useState('service');
  const logsEndRef = useRef(null);

  const scrollToBottom = () => {
    logsEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }

  useEffect(() => {
    let isCurrent = true;
    setLogs([]);

    const ws = new WebSocket(buildLogWsUrl(logFile));

    ws.onmessage = (event) => {
      if (!isCurrent) return;
      setLogs(prevLogs => [...prevLogs, event.data]);
    };

    ws.onclose = () => {
      if (!isCurrent) return;
      setLogs(prevLogs => [...prevLogs, "Connection closed"]);
    };

    ws.onerror = () => {
      if (!isCurrent) return;
      setLogs(prevLogs => [...prevLogs, "WebSocket error"]);
    };

    return () => {
      isCurrent = false;
      ws.close();
    };
  }, [logFile]);

  useEffect(() => {
    scrollToBottom();
  }, [logs]);

  return (
    <Card
      title="系统日志"
      extra={
        <Space>
          <span>日志文件</span>
          <Select
            value={logFile}
            options={LOG_FILE_OPTIONS}
            onChange={setLogFile}
            style={{ width: 140 }}
          />
        </Space>
      }
    >
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
