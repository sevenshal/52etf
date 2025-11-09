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
        </Route>
      </Routes>
    </AccountProvider>
  );
}

export default App;
