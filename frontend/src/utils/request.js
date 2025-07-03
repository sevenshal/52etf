import axios from 'axios';

const request = axios.create({
  baseURL: process.env.REACT_APP_API_URL,
  timeout: 60000
});

request.interceptors.request.use(config => {
  const accountId = localStorage.getItem('accountId');
  
  // 允许 profile 和 validate-account 相关的请求通过
  if (!accountId && 
      !config.url.includes('/profile') && 
      !config.url.includes('/validate-account')) {
    return Promise.reject(new Error('No account ID'));
  }
  
  if (accountId) {
    config.headers['X-Account-ID'] = accountId;
  }
  return config;
});

export default request; 