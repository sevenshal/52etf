const { createProxyMiddleware } = require('http-proxy-middleware');

// The public development hostname is served by the React dev server.  Proxy
// API requests locally so the same HTTPS origin can reach FastAPI through FRP.
module.exports = function setupProxy(app) {
  app.use(
    '/api',
    createProxyMiddleware({
      target: 'http://127.0.0.1:8001',
      changeOrigin: true,
    }),
  );
};
