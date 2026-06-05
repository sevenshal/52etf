import React, { createContext, useContext, useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';

// 创建 context 时提供一个有效的默认值
const AccountContext = createContext({
  accountId: null,
  login: () => {},
  logout: () => {}
});

// Provider 组件
export function AccountProvider({ children }) {
  const [accountId, setAccountId] = useState(null);
  const navigate = useNavigate();

  useEffect(() => {
    const storedAccountId = localStorage.getItem('accountId');
    setAccountId(storedAccountId);
    
    if (!storedAccountId) {
      navigate('/profile');
    }
  }, [navigate]);

  const login = (id) => {
    localStorage.setItem('accountId', id);
    setAccountId(id);
  };

  const logout = () => {
    localStorage.removeItem('accountId');
    setAccountId(null);
    navigate('/profile');
  };

  const value = {
    accountId,
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
