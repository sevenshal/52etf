import React from 'react';
import { Routes, Route } from 'react-router-dom';
import { AccountProvider } from './contexts/AccountContext';
import AppLayout from './pages/Layout';
import FearDashboard from './pages/fear/FearDashboard';
import FearStockList from './pages/fear/FearStockList';
import FearTradingLogs from './pages/fear/FearTradingLogs';
import Profile from './pages/Profile';
import EVCDashboard from './pages/EVCDashboard';
import ETFReport from './pages/ETFReport';
import ETFDetail from './pages/ETFDetail';
import EVCStrategy from './pages/EVCStrategy';
import EVCValuation from './pages/EVCValuation';
import EVCTradeLogs from './pages/EVCTradeLogs';
import StockDetail from './pages/StockDetail';
import OptionsPositions from './pages/OptionsPositions';
import MonthlyAnalysis from './pages/MonthlyAnalysis';
import MarketSignalHistory from './pages/MarketSignalHistory';
import SystemLog from './pages/SystemLog';
import FearBacktest from './pages/fear/FearBacktest';
import LevETFBacktest from './pages/LevETFBacktest';
import AutomatedTrading from './pages/AutomatedTrading';
import IBKRAccountManager from './pages/IBKRAccountManager';
import AllWeatherBacktest from './pages/AllWeatherBacktest';
import PortfolioCopyTrading from './pages/PortfolioCopyTrading';
import LongPortAccountManager from './pages/LongPortAccountManager';
import SZDTAutoTrading from './pages/SZDTAutoTrading';
import ScheduledTasks from './pages/ScheduledTasks';
import EVCAccountManager from './pages/EVCAccountManager';
import SoxlFearBacktest from './pages/SoxlFearBacktest';
import SoxlFearStrategy from './pages/SoxlFearStrategy';
import W20MomentumBacktest from './pages/W20MomentumBacktest';
import W20MomentumLive from './pages/W20MomentumLive';

function App() {
  return (
    <AccountProvider>
      <Routes>
        <Route path="/" element={<AppLayout />}>
          <Route index element={<ETFReport />} />
          <Route path='/fear' element={<FearDashboard />} />
          <Route path="/fear/stocks" element={<FearStockList />} />
          <Route path="/evc" element={<EVCValuation />} />
          <Route path="/options" element={<OptionsPositions />} />
          <Route path="/profile" element={<Profile />} />
          <Route path="/fear/logs" element={<FearTradingLogs />} />
          <Route path="/fear/backtest" element={<FearBacktest />} />
          <Route path="/etf/:symbol" element={<ETFDetail />} />
          <Route path="/evc/strategy" element={<EVCStrategy />} />
          <Route path="/evc/valuation" element={<EVCValuation />} />
          <Route path="/evc/trade-logs" element={<EVCTradeLogs />} />
          <Route path="/stock/:symbol" element={<StockDetail />} />
          <Route path="/monthly-analysis" element={<MonthlyAnalysis />} />
          <Route path="/market-signal-history" element={<MarketSignalHistory />} />
          <Route path="/system-log" element={<SystemLog />} />
          <Route path="/lev-etf-backtest" element={<LevETFBacktest />} />
          <Route path="/automated-trading" element={<AutomatedTrading />} />
          <Route path="/ib-account-manager" element={<IBKRAccountManager />} />
          <Route path="/evc-account-manager" element={<EVCAccountManager />} />
          <Route path="/all-weather-backtest" element={<AllWeatherBacktest />} />
          <Route path="/portfolio-copy-trading" element={<PortfolioCopyTrading />} />
          <Route path="/longport-account-manager" element={<LongPortAccountManager />} />
          <Route path="/szdt-auto-trading" element={<SZDTAutoTrading />} />
          <Route path="/scheduled-tasks" element={<ScheduledTasks />} />
          <Route path="/soxl-fear-backtest" element={<SoxlFearBacktest />} />
          <Route path="/soxl-fear-strategy" element={<SoxlFearStrategy />} />
          <Route path="/w20-momentum-backtest" element={<W20MomentumBacktest />} />
          <Route path="/w20-momentum-live" element={<W20MomentumLive />} />
        </Route>
      </Routes>
    </AccountProvider>
  );
}

export default App;
