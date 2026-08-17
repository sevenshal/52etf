import React, { lazy, Suspense } from 'react';
import { Navigate, Routes, Route, useLocation } from 'react-router-dom';
import { Spin } from 'antd';
import { AccountProvider, useAccount } from './contexts/AccountContext';
import AppLayout from './pages/Layout';
import { adminRouteDescriptors } from './routes/adminRoutes';

// ---- 普通页面：按需加载（code splitting），每个页面一个独立 chunk ----
const FearDashboard = lazy(() => import('./pages/fear/FearDashboard'));
const FearTradingLogs = lazy(() => import('./pages/fear/FearTradingLogs'));
const Profile = lazy(() => import('./pages/Profile'));
const ETFReport = lazy(() => import('./pages/ETFReport'));
const ETFDetail = lazy(() => import('./pages/ETFDetail'));
const EVCValuation = lazy(() => import('./pages/EVCValuation'));
const StockDetail = lazy(() => import('./pages/StockDetail'));

// ---- 管理员页面：见 ./routes/adminRoutes.js（独立 admin chunk，仅管理员加载）----

const LiveTabRedirect = ({ tab }) => {
  const location = useLocation();
  const searchParams = new URLSearchParams(location.search);
  searchParams.set('tab', tab);
  return <Navigate to={`/live?${searchParams.toString()}`} replace />;
};

const AdminRoute = ({ children }) => {
  const { isAdmin, accountReady } = useAccount();

  if (!accountReady) {
    return null;
  }

  return isAdmin ? children : <Navigate to="/profile" replace />;
};

const LoadingFallback = ({ fullScreen = false }) => (
  <div
    style={{
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      minHeight: fullScreen ? '100vh' : '60vh',
    }}
  >
    <Spin size="large" />
  </div>
);

function AppRoutes() {
  const { isAdmin, accountReady } = useAccount();

  // 账户信息还没校验完（validate-account 返回前）不渲染路由，
  // 避免管理员直接访问 /ai-stock 等 URL 时被兜底路由误重定向到 /profile。
  if (!accountReady) {
    return <LoadingFallback fullScreen />;
  }

  return (
    <Suspense fallback={<LoadingFallback />}>
      <Routes>
        <Route path="/" element={<AppLayout />}>
          <Route index element={<ETFReport />} />
          <Route path='/fear' element={<FearDashboard />} />
          <Route path="/evc" element={<EVCValuation />} />
          <Route path="/executor-status" element={<LiveTabRedirect tab="executor" />} />
          <Route path="/profile" element={<Profile />} />
          <Route path="/fear/logs" element={<FearTradingLogs />} />
          <Route path="/etf/:symbol" element={<ETFDetail />} />
          <Route path="/evc/valuation" element={<EVCValuation />} />
          <Route path="/stock/:symbol" element={<StockDetail />} />
          <Route path="/external-trading-accounts" element={<LiveTabRedirect tab="accounts" />} />
          <Route path="/soxl-fear-strategy" element={<LiveTabRedirect tab="sentiment" />} />

          {/* 管理员专属路由：仅 isAdmin 时注册。非管理员访问这些 URL 会命中下面的
              "*" 兜底路由重定向到 /profile，且永远不会加载 admin chunk。 */}
          {isAdmin &&
            adminRouteDescriptors.map(({ path, Component, props }) => (
              <Route
                key={path}
                path={path}
                element={
                  <AdminRoute>
                    <Component {...props} />
                  </AdminRoute>
                }
              />
            ))}

          {/* 兜底：未匹配路径（含非管理员访问管理员 URL）跳转到「我的」 */}
          <Route path="*" element={<Navigate to="/profile" replace />} />
        </Route>
      </Routes>
    </Suspense>
  );
}

function App() {
  return (
    <AccountProvider>
      <AppRoutes />
    </AccountProvider>
  );
}

export default App;
