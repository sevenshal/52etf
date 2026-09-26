import React, { useLayoutEffect, useRef, useState } from 'react';
import ReactDOM from 'react-dom/client';
import { Router, UNSAFE_createBrowserHistory } from 'react-router-dom';
import './index.css';
import App from './App';
import appLogo from './logo';

const appTitle = process.env.NODE_ENV === 'production' ? '我爱ETF' : '我爱ETF (dev)';

// react-router v7 的 BrowserRouter 会把地址变化放进 startTransition。市场页打开内嵌
// K 线后，图表的重绘/销毁会让这类低优先级更新一直保留在旧页面：URL 已变，但画面要等
// 到再发生一次同步更新（例如关闭 K 线）才切过去。主导航属于明确的用户操作，应同步提交。
const SynchronousBrowserRouter = ({ children }) => {
  const historyRef = useRef(null);
  if (!historyRef.current) {
    historyRef.current = UNSAFE_createBrowserHistory({ window, v5Compat: true });
  }
  const history = historyRef.current;
  const [state, setState] = useState({ action: history.action, location: history.location });

  useLayoutEffect(() => history.listen(setState), [history]);

  return (
    <Router
      location={state.location}
      navigationType={state.action}
      navigator={history}
    >
      {children}
    </Router>
  );
};

const setFavicon = (href) => {
  const existingIcon = document.querySelector('link[rel="icon"]');
  const icon = existingIcon || document.createElement('link');

  icon.rel = 'icon';
  icon.type = 'image/svg+xml';
  icon.href = href;

  if (!existingIcon) {
    document.head.appendChild(icon);
  }
};

document.title = appTitle;
setFavicon(appLogo);

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(
  <React.StrictMode>
    <SynchronousBrowserRouter>
      <App />
    </SynchronousBrowserRouter>
  </React.StrictMode>
);
