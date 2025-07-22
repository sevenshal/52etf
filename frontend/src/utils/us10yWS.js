// 52etf_fe/src/utils/us10yWS.js
// 封装Investing.com美债10Y收益率WebSocket行情推送

function randomSockjsId(len = 8) {
  const chars = 'abcdefghijklmnopqrstuvwxyz0123456789';
  let id = '';
  for (let i = 0; i < len; i++) {
    id += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return id;
}

export class US10YWS {
  constructor({ onYieldUpdate } = {}) {
    this.channel = 761;
    this.sessionId = randomSockjsId();
    this.wsUrl = `wss://streaming.forexpros.com/echo/${this.channel}/${this.sessionId}/websocket`;
    this.ws = null;
    this.onYieldUpdate = onYieldUpdate;
    this.connected = false;
    this._subscribed = false;
  }

  connect() {
    this.ws = new WebSocket(this.wsUrl);
    this.ws.onopen = () => {
      this.connected = true;
      this._subscribed = false;
    };
    this.ws.onmessage = (event) => {
      // 1. 等待握手 'o'
      if (event.data === 'o' && !this._subscribed) {
        // 2. 发送 bulk-subscribe
        const bulkMsg = [
          JSON.stringify({
            _event: "bulk-subscribe",
            tzID: 8,
            message: "pid-23705:"
          })
        ];
        this.ws.send(JSON.stringify(bulkMsg));
        // 3. 发送 UID
        const uidMsg = [
          JSON.stringify({
            _event: "UID",
            UID: Math.floor(Math.random() * 1e9)
          })
        ];
        this.ws.send(JSON.stringify(uidMsg));
        this._subscribed = true;
        return;
      }
      var dataStr = event.data;
      // 4. 只处理行情推送
      if (typeof dataStr === 'string' && dataStr.includes('pid-23705::')) {
        try {
          if (dataStr.startsWith('a[')) {
            dataStr = dataStr.substring(1);
          }
          const outer = JSON.parse(JSON.parse(dataStr)[0]);
          if (outer.message && outer.message.startsWith('pid-23705::')) {
            const inner = JSON.parse(outer.message.replace('pid-23705::', ''));
            if (inner.last_numeric && this.onYieldUpdate) {
              this.onYieldUpdate(Number(inner.last_numeric));
            }
          }
        } catch (e) {
          // ignore
        }
      }
    };
    this.ws.onclose = () => {
      this.connected = false;
    };
    this.ws.onerror = (e) => {
      this.connected = false;
    };
  }

  disconnect() {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
      this.connected = false;
    }
  }
} 