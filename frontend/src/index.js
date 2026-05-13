import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import './index.css';
import App from './App';
import appLogo from './logo';

const setFavicon = (href) => {
  const existingIcon = document.querySelector('link[rel="icon"]');
  const icon = existingIcon || document.createElement('link');

  icon.rel = 'icon';
  icon.type = 'image/svg+xml';
  icon.href = href;

  if (!existingIcon) {
    document.head.appendChild(icon);
  }
};

setFavicon(appLogo);

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>
);
