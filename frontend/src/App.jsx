import React from 'react';
import { Routes, Route } from 'react-router-dom';
import { AccountProvider } from './contexts/AccountContext';
import AppLayout from './components/Layout';
import FearStockList from './components/FearStockList';
import Profile from './components/Profile';
import EVCDashboard from './components/EVCDashboard';
import FearDashboard from './components/FearDashboard';
import FearTradingLogs from './components/FearTradingLogs';
import ETFReport from './components/ETFReport';
import ETFDetail from './components/ETFDetail';
import EVCStrategy from './components/EVCStrategy';
import EVCValuation from './components/EVCValuation';
import EVCTradeLogs from './components/EVCTradeLogs';
import StockDetail from './components/StockDetail';
import OptionsPositions from './components/OptionsPositions';
import MonthlyAnalysis from './components/MonthlyAnalysis';
import MarketSignalHistory from './components/MarketSignalHistory';

function App() {
  return (
    <AccountProvider>
      <Routes>
        <Route path="/" element={<AppLayout />}>
          <Route index element={<ETFReport />} />
          <Route path='/fear' element={<FearDashboard />} />
          <Route path="/fear/stocks" element={<FearStockList />} />
          <Route path="/evc" element={<EVCDashboard />} />
          <Route path="/options" element={<OptionsPositions />} />
          <Route path="/profile" element={<Profile />} />
          <Route path="/fear/logs" element={<FearTradingLogs />} />
          <Route path="/fear/backtest" element={<FearDashboard />} />
          <Route path="/etf/:symbol" element={<ETFDetail />} />
          <Route path="/evc/strategy" element={<EVCStrategy />} />
          <Route path="/evc/valuation" element={<EVCValuation />} />
          <Route path="/evc/trade-logs" element={<EVCTradeLogs />} />
          <Route path="/stock/:symbol" element={<StockDetail />} />
          <Route path="/monthly-analysis" element={<MonthlyAnalysis />} />
          <Route path="/market-signal-history" element={<MarketSignalHistory />} />
        </Route>
      </Routes>
    </AccountProvider>
  );
}

export default App;
