import { lazy } from 'react';

/**
 * 「市场」页面注册表（含雪球持仓、东方财富子页）。
 *
 * 管理员或被授权查看市场（canViewMarket）的账户可见，打成独立的 "market"
 * chunk，不进 admin chunk，被授权的普通账户不会加载到管理员代码。
 * 真正的权限边界在后端（valid_market_viewer）。
 */
const Market = lazy(() => import(/* webpackChunkName: "market" */ '../pages/Market'));

export const marketRouteDescriptors = [
  { path: '/market', Component: Market },
  { path: '/market/alerts', Component: Market, props: { initialTab: 'alerts' } },
  { path: '/market/industry', Component: Market, props: { initialTab: 'industry' } },
  { path: '/market/fund-flow', Component: Market, props: { initialTab: 'fund-flow' } },
  { path: '/market/earnings-gap', Component: Market, props: { initialTab: 'earnings-gap' } },
  { path: '/market/xueqiu-holdings', Component: Market, props: { initialTab: 'xueqiu-holdings' } },
  { path: '/market/eastmoney-holdings', Component: Market, props: { initialTab: 'eastmoney-holdings' } },
];
