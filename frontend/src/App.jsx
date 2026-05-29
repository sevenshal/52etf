import React from 'react';
import { Routes, Route } from 'react-router-dom';
import { AccountProvider } from './contexts/AccountContext';
import AppLayout from './pages/Layout';
import FearDashboard from './pages/fear/FearDashboard';
import FearStockList from './pages/fear/FearStockList';
import FearTradingLogs from './pages/fear/FearTradingLogs';
import Profile from './pages/Profile';
import ETFReport from './pages/ETFReport';
import ETFDetail from './pages/ETFDetail';
import EVCValuation from './pages/EVCValuation';
import StockDetail from './pages/StockDetail';
import OptionsPositions from './pages/OptionsPositions';
import MonthlyAnalysis from './pages/MonthlyAnalysis';
import SystemLog from './pages/SystemLog';
import FearBacktest from './pages/fear/FearBacktest';
import LevETFBacktest from './pages/LevETFBacktest';
import AutomatedTrading from './pages/AutomatedTrading';
import IBKRAccountManager from './pages/IBKRAccountManager';
import AllWeatherBacktest from './pages/AllWeatherBacktest';
import PortfolioCopyTrading from './pages/PortfolioCopyTrading';
import LongPortAccountManager from './pages/LongPortAccountManager';
import ExternalTradingAccountManager from './pages/ExternalTradingAccountManager';
import ExecutorStatusPage from './pages/ExecutorStatusPage';
import SZDTAutoTrading from './pages/SZDTAutoTrading';
import ScheduledTasks from './pages/ScheduledTasks';
import EVCAccountManager from './pages/EVCAccountManager';
import SoxlFearBacktest from './pages/SoxlFearBacktest';
import SoxlFearStrategy from './pages/SoxlFearStrategy';
import FactorLab from './pages/FactorLab';

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
          <Route path="/executor-status" element={<ExecutorStatusPage />} />
          <Route path="/db" element={<FactorLab initialTab="db" />} />
          <Route path="/factor-lab" element={<FactorLab />} />
          <Route path="/factor-lab/live" element={<FactorLab initialTab="live" />} />
          <Route path="/profile" element={<Profile />} />
          <Route path="/fear/logs" element={<FearTradingLogs />} />
          <Route path="/fear/backtest" element={<FearBacktest />} />
          <Route path="/etf/:symbol" element={<ETFDetail />} />
          <Route path="/evc/valuation" element={<EVCValuation />} />
          <Route path="/stock/:symbol" element={<StockDetail />} />
          <Route path="/monthly-analysis" element={<MonthlyAnalysis />} />
          <Route path="/system-log" element={<SystemLog />} />
          <Route path="/lev-etf-backtest" element={<LevETFBacktest />} />
          <Route path="/automated-trading" element={<AutomatedTrading />} />
          <Route path="/ib-account-manager" element={<IBKRAccountManager />} />
          <Route path="/evc-account-manager" element={<EVCAccountManager />} />
          <Route path="/all-weather-backtest" element={<AllWeatherBacktest />} />
          <Route path="/portfolio-copy-trading" element={<PortfolioCopyTrading />} />
          <Route path="/longport-account-manager" element={<LongPortAccountManager />} />
          <Route path="/external-trading-accounts" element={<ExternalTradingAccountManager />} />
          <Route path="/szdt-auto-trading" element={<SZDTAutoTrading />} />
          <Route path="/scheduled-tasks" element={<ScheduledTasks />} />
          <Route path="/soxl-fear-backtest" element={<SoxlFearBacktest />} />
          <Route path="/soxl-fear-strategy" element={<SoxlFearStrategy />} />
        </Route>
      </Routes>
    </AccountProvider>
  );
}

export default App;
