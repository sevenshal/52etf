import React, { createContext, useContext, useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import request from '../utils/request';

// 创建 context 时提供一个有效的默认值
const AccountContext = createContext({
  accountId: null,
  isAdmin: false,
  canViewAiStock: false,
  accountReady: false,
  login: () => {},
  logout: () => {}
});

// Provider 组件
export function AccountProvider({ children }) {
  const [accountId, setAccountId] = useState(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [canViewAiStock, setCanViewAiStock] = useState(false);
  const [accountReady, setAccountReady] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    const storedAccountId = localStorage.getItem('accountId');
    if (!storedAccountId) {
      setAccountReady(true);
      navigate('/profile');
      return;
    }

    setAccountId(storedAccountId);
    request.get('/api/profile/validate-account', { params: { account_id: storedAccountId } })
      .then(({ data }) => {
        if (data.valid) {
          setIsAdmin(Boolean(data.is_admin));
          setCanViewAiStock(Boolean(data.can_view_ai_stock));
        } else {
          localStorage.removeItem('accountId');
          setAccountId(null);
          navigate('/profile');
        }
      })
      .catch(() => {
        setIsAdmin(false);
        setCanViewAiStock(false);
      })
      .finally(() => setAccountReady(true));
  }, [navigate]);

  const login = (id, admin = false, aiStockViewer = false) => {
    localStorage.setItem('accountId', id);
    setAccountId(id);
    setIsAdmin(Boolean(admin));
    setCanViewAiStock(Boolean(aiStockViewer));
    setAccountReady(true);
  };

  const logout = () => {
    localStorage.removeItem('accountId');
    setAccountId(null);
    setIsAdmin(false);
    setCanViewAiStock(false);
    setAccountReady(true);
    navigate('/profile');
  };

  const value = {
    accountId,
    isAdmin,
    canViewAiStock,
    accountReady,
    login,
    logout
  };

  return (
    <AccountContext.Provider value={value}>
      {children}
    </AccountContext.Provider>
  );
}

// Hook
export function useAccount() {
  const context = useContext(AccountContext);
  if (context === undefined) {
    throw new Error('useAccount must be used within an AccountProvider');
  }
  return context;
}
