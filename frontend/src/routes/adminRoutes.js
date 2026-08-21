import { lazy } from 'react';

/**
 * 管理员专属页面注册表。
 *
 * 这些页面（以及它们引用的所有代码）会被 webpack 打成一个独立的
 * "admin" chunk（见下面的 webpackChunkName 注释）。App.jsx 只在
 * isAdmin === true 时才把这里的路由注册进 <Routes>，所以：
 *   - 非管理员永远不会请求 / 下载这个 chunk，看不到任何管理员相关代码；
 *   - 管理员第一次进入任一管理员页面时按需加载，不拖慢首屏。
 *
 * 注意：这只是前端代码隔离（减少暴露面 + 减小非管理员包体积）。
 * 真正的权限边界在后端（valid_admin_account），前端隐藏不能替代后端鉴权。
 */
export const adminRouteDescriptors = [
  {
    path: '/db',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/FactorLab')),
    props: { initialTab: 'db' },
  },
  {
    path: '/factor-lab',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/FactorLab')),
  },
  {
    path: '/factor-lab/fund-flow',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/FactorLab')),
    props: { initialTab: 'fund-flow' },
  },
  {
    path: '/factor-lab/xueqiu-holdings',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/FactorLab')),
    props: { initialTab: 'xueqiu-holdings' },
  },
  {
    path: '/factor-lab/nine-turn-breadth',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/FactorLab')),
    props: { initialTab: 'nine-turn-breadth' },
  },
  {
    path: '/live',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/LiveTrading')),
  },
  {
    path: '/web-account-manager',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/WebAccountManager')),
  },
  {
    path: '/tushare-account-manager',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/TushareAccountManager')),
  },
  {
    path: '/system-log',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/SystemLog')),
  },
  {
    path: '/system-info',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/SystemInfo')),
  },
  {
    path: '/scheduled-tasks',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/ScheduledTasks')),
  },
  {
    path: '/email-settings',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/EmailSettings')),
  },
  {
    path: '/fear-greed-signal-config',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/FearGreedSignalConfig')),
  },
  // ---- Profile 管理员菜单页面（持仓/账户管理/回测/系统等）----
  {
    path: '/options',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/OptionsPositions')),
  },
  {
    path: '/automated-trading',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/AutomatedTrading')),
  },
  {
    path: '/fear/stocks',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/fear/FearStockList')),
  },
  {
    path: '/portfolio-copy-trading',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/PortfolioCopyTrading')),
  },
  {
    path: '/ib-account-manager',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/IBKRAccountManager')),
  },
  {
    path: '/longport-account-manager',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/LongPortAccountManager')),
  },
  {
    path: '/evc-account-manager',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/EVCAccountManager')),
  },
  {
    path: '/lev-etf-backtest',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/LevETFBacktest')),
  },
  {
    path: '/all-weather-backtest',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/AllWeatherBacktest')),
  },
  {
    path: '/fear/backtest',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/fear/FearBacktest')),
  },
  {
    path: '/fear-volume-backtest',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/SoxlFearBacktest')),
  },
  {
    path: '/a-stock-fear-etf-backtest',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/AStockFearEtfBacktest')),
  },
  {
    path: '/monthly-analysis',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/MonthlyAnalysis')),
  },
  {
    path: '/szdt-auto-trading',
    Component: lazy(() => import(/* webpackChunkName: "admin" */ '../pages/SZDTAutoTrading')),
  },
];
