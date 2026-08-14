# 外部交易账号长连接接入文档

版本：2026-06-06

本文档面向外部交易接入方，说明外部交易客户端如何通过 WebSocket 长连接接入 52ETF 后端，接收交易、行情、资产查询等指令，并向 52ETF 回传响应、订单状态、成交回报、交割单和券商持仓快照。

## 1. 接入总览

外部交易账号采用“外部客户端主动连入 52ETF 后端”的反向长连接模式。

流程如下：

1. 52ETF 管理端先创建并启用一个外部交易账号，配置 `account_id`、`name`、`identifier`、`market_type` 等信息。
2. 接入方客户端使用该账号的 `account_id` 和 `identifier` 生成签名，主动连接 52ETF WebSocket 地址。
3. WebSocket 握手通过后，双方业务消息全部通过加密信封传输。
4. 52ETF 后端通过该连接下发 `command` 指令。
5. 接入方执行指令后回传 `result`。
6. 接入方在订单状态、成交、交割单、持仓快照变化时，可以主动上报事件。

核心要求：

- WebSocket 是单账号单连接。相同外部账号新连接成功后，旧连接会被服务端关闭。
- `client_order_id` 是 52ETF 本地订单和外部回报匹配的最重要字段。只要 52ETF 下发的订单里包含该字段，接入方就必须在委托响应、订单事件和成交事件中原样回传。
- 业务消息必须加密。除 WebSocket 握手 query 外，连接建立后的所有 JSON 消息都必须使用 `secure` 信封。
- A股账号的交易、行情、资产、订单、交割查询指令会被服务端限制在交易时段内下发；美股账号当前不走这层拦截。

## 2. 环境和地址

WebSocket 路径：

```text
/api/external-trading-accounts/ws
```

生产示例：

```text
ws://api.52etf.vip/api/external-trading-accounts/ws?account_id=...&identifier=...&ts=...&nonce=...&signature=...
```

如果部署环境启用了 TLS，则使用：

```text
wss://api.52etf.vip/api/external-trading-accounts/ws?account_id=...&identifier=...&ts=...&nonce=...&signature=...
```

具体协议、域名和端口以 52ETF 提供的联调环境为准。项目内 PTrade 示例客户端当前默认配置为：

```text
API_HOST = "api.52etf.vip"
USE_HTTPS = False
```

## 3. 账号准备

接入前需要 52ETF 先在“外部交易账号”页面创建账号。

账号字段：

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `account_id` | string | 是 | 52ETF 内部用户账号 ID。由 52ETF 提供，WebSocket 握手会校验其合法性。 |
| `name` | string | 是 | 外部交易账号展示名称。只由服务端维护，连接成功后会返回给客户端。 |
| `identifier` | string | 是 | 外部接入方唯一标识，例如券商资金账号、PTrade 账号标识等。同一 `account_id` 下唯一。 |
| `market_type` | string | 是 | 市场类型。支持 `A_STOCK`、`US_STOCK`。 |
| `enabled` | boolean | 是 | 是否启用。禁用账号会拒绝 WebSocket 连接。 |
| `executor_price_level` | integer | 否 | 默认执行价格档位，见“价格档位说明”。 |
| `executor_lot_size` | integer | 否 | 默认交易批量。A股默认 100，美股默认 1。 |
| `executor_order_timeout_seconds` | integer | 否 | 默认订单等待超时时间，单位秒，默认 120。 |
| `executor_max_replace_count` | integer | 否 | 最大重定价次数，默认 3。 |
| `executor_max_slippage_pct` | number | 否 | 基于参考价的保护限价滑点百分比，默认 0.5。 |
| `commission_rate_pct` | number | 否 | 佣金费率百分比，默认 0.025。 |
| `min_commission` | number | 否 | 最低佣金，默认 5.0。 |
| `stamp_tax_rate_pct` | number | 否 | 印花税费率百分比，A股默认 0.05，美股默认 0。 |

`market_type` 兼容输入会被服务端归一化：

| 归一化结果 | 兼容输入示例 |
| --- | --- |
| `A_STOCK` | `A`、`CN`、`CHINA`、`A股`、`A_SHARE`、`ASHARE` |
| `US_STOCK` | `US`、`USA`、`US_STOCK`、`美股`、`AMERICA` |

## 4. WebSocket 握手参数

客户端连接时必须通过 query string 传递以下参数：

| 参数 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `account_id` | string | 是 | 52ETF 内部用户账号 ID。 |
| `identifier` | string | 是 | 外部交易账号唯一标识，必须与管理端配置一致。 |
| `ts` | string | 是 | 当前 Unix 秒级时间戳。服务端允许最大时间偏差 90 秒。 |
| `nonce` | string | 是 | 一次性随机串，建议 16 字节随机数再做 base64url 编码。服务端会防重放。 |
| `signature` | string | 是 | 对规范化握手 payload 的 RSA-SHA256 签名，base64url 编码，不带 `=` padding。 |

握手示例：

```text
GET /api/external-trading-accounts/ws?account_id=vNKp...&identifier=GS66301027527&ts=1780732800&nonce=2gS...&signature=QY...
```

服务端校验顺序：

1. `account_id`、`identifier` 是否存在。
2. `account_id` 是否为合法 52ETF 账号。
3. `ts`、`nonce`、`signature` 是否完整。
4. `ts` 是否在 90 秒容忍范围内。
5. `nonce` 是否已使用过。
6. RSA 签名是否正确。
7. 是否存在 `account_id + identifier` 对应的外部交易账号。
8. 账号是否启用。

失败时服务端会关闭连接，close code 通常为 `1008`，reason 可能为：

| reason | 含义 |
| --- | --- |
| `account_id and identifier are required` | 缺少账号参数。 |
| `invalid account_id` | `account_id` 非法。 |
| `signature, ts and nonce are required` | 缺少签名相关参数。 |
| `invalid timestamp` | 时间戳格式错误。 |
| `signature timestamp expired` | 时间戳过期或本机时间偏差过大。 |
| `signature nonce replayed` | `nonce` 被重复使用。 |
| `invalid signature` | 签名无效。 |
| `external trading account not found` | 管理端未创建对应账号。 |
| `external trading account disabled` | 账号已禁用。 |

## 5. 握手签名算法

### 5.1 规范化 payload

签名前先构造如下 JSON，字段按 key 排序，使用紧凑分隔符，不保留空格：

```json
{"account_id":"vNKpHJkLMnBQRSTUVWXYZabcdefghijkl","identifier":"GS66301027527","nonce":"2gS3xvBt7CqY0Cbm3S_m3Q","ts":"1780732800"}
```

规范化规则：

- 字段固定为 `account_id`、`identifier`、`nonce`、`ts`。
- `ts` 必须转成字符串。
- JSON 序列化必须 `sort_keys=true`。
- 分隔符必须为 `,` 和 `:`，不能包含额外空格。
- 字符集为 UTF-8。

Python 等价写法：

```python
message = json.dumps(
    {
        "account_id": account_id,
        "identifier": identifier,
        "nonce": nonce,
        "ts": str(ts),
    },
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
```

### 5.2 RSA-SHA256 签名

签名方式：

| 项 | 值 |
| --- | --- |
| 算法 | RSA-SHA256 |
| Padding | PKCS#1 v1.5 |
| Digest | SHA-256 |
| 输出编码 | base64url，无 `=` padding |

接入方需要使用 52ETF 提供的私钥或示例客户端中的签名实现生成 `signature`。52ETF 后端使用对应公钥验签。

## 6. 业务消息加密信封

WebSocket 连接成功后，双方发送的文本消息都必须是加密信封。明文 JSON 不会被服务端接受。

信封格式：

```json
{
  "type": "secure",
  "alg": "CHACHA20-HMAC-SHA256",
  "nonce": "base64url-12-byte-nonce",
  "ciphertext": "base64url-ciphertext",
  "mac": "base64url-hmac"
}
```

字段说明：

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `type` | string | 是 | 固定为 `secure`。 |
| `alg` | string | 是 | 固定为 `CHACHA20-HMAC-SHA256`。 |
| `nonce` | string | 是 | 12 字节消息随机数，base64url 编码，无 padding。 |
| `ciphertext` | string | 是 | 明文 JSON 用 ChaCha20 加密后的密文，base64url 编码，无 padding。 |
| `mac` | string | 是 | HMAC-SHA256 校验值，base64url 编码，无 padding。 |

密钥派生：

```text
shared_key = base64_decode(SHARED_KEY_B64)
enc_key = sha256(b"external-trading-enc:" + shared_key)
mac_key = sha256(b"external-trading-mac:" + shared_key)
```

加密：

1. 将明文业务 JSON 用 UTF-8 序列化，分隔符使用 `,` 和 `:`。
2. 生成 12 字节 `nonce`。
3. 使用 ChaCha20 加密明文字节，counter 从 1 开始，常量为 `expand 32-byte k`。
4. 计算 `mac = HMAC-SHA256(mac_key, nonce + ciphertext)`。
5. 组装 `secure` 信封并发送为 WebSocket 文本消息。

解密：

1. 解析 `secure` 信封。
2. base64url 解码 `nonce`、`ciphertext`、`mac`。
3. 重新计算 HMAC，并使用常量时间比较。
4. HMAC 通过后，用 ChaCha20 解密 `ciphertext`。
5. 将明文字节按 UTF-8 JSON 解析为业务消息。

`SHARED_KEY_B64`、RSA 私钥等敏感材料由 52ETF 单独提供。生产接入时不要把密钥写入日志、错误信息或普通聊天工具。

## 7. 连接建立和心跳

连接成功后，服务端会立即发送一条加密的 `connected` 消息：

```json
{
  "type": "connected",
  "account_id": "vNKpHJkLMnBQRSTUVWXYZabcdefghijkl",
  "name": "券商实盘账号",
  "identifier": "GS66301027527",
  "market_type": "A_STOCK",
  "connected_at": "2026-06-06T10:30:00.123456"
}
```

字段说明：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `type` | string | 固定为 `connected`。 |
| `account_id` | string | 52ETF 内部用户账号 ID。 |
| `name` | string | 管理端配置的外部交易账号名称。 |
| `identifier` | string | 外部交易账号唯一标识。 |
| `market_type` | string | `A_STOCK` 或 `US_STOCK`。 |
| `connected_at` | string | 服务端连接时间，ISO 8601。 |

客户端建议每 10 秒发送一次 `heartbeat`：

```json
{
  "type": "heartbeat",
  "ts": "2026-06-06T10:30:10.123456"
}
```

也可以发送 `ping`：

```json
{
  "type": "ping",
  "ts": "2026-06-06T10:30:10.123456"
}
```

服务端收到 `heartbeat` 或 `ping` 后返回：

```json
{
  "type": "pong",
  "ts": "2026-06-06T10:30:10.234567"
}
```

心跳的作用：

- 更新服务端运行态 `last_seen_at`。
- 帮助双方及时发现连接断开。
- 避免代理、网关或券商运行环境长时间空闲断线。

断线后客户端应自动重连。示例客户端默认 5 秒后重连。

## 8. 服务端下发指令

除连接确认 `connected` 和心跳响应 `pong` 外，服务端业务指令只会下发 `command` 类型消息：

```json
{
  "type": "command",
  "id": "7e8b7117dd284732b8944e5116d8a0de",
  "action": "place_orders",
  "payload": {
    "orders": []
  },
  "ts": "2026-06-06T10:30:12.123456"
}
```

字段说明：

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `type` | string | 是 | 固定为 `command`。 |
| `id` | string | 是 | 服务端生成的请求 ID。客户端返回 `result` 时必须原样带回。 |
| `action` | string | 是 | 指令名称。 |
| `payload` | object | 是 | 指令参数。无参数时为空对象 `{}`。 |
| `ts` | string | 是 | 服务端发送时间，ISO 8601。 |

客户端处理要求：

- 收到 `command` 后必须尽快执行，并用同一个 `id` 返回 `result`。
- 指令执行失败也必须返回 `result`，设置 `ok=false`，并填写 `error` 或 `message`。
- 不支持的 `action` 必须返回失败，不要静默忽略。
- 如果接入方运行环境要求交易 API 必须在特定回调中调用，可以先把命令放入本地队列，在合规回调中执行。示例 PTrade 客户端就是这样处理的。

## 9. 客户端返回结果

所有 `command` 都必须返回 `result`：

```json
{
  "type": "result",
  "id": "7e8b7117dd284732b8944e5116d8a0de",
  "ok": true,
  "data": {},
  "ts": "2026-06-06T10:30:12.456789"
}
```

失败示例：

```json
{
  "type": "result",
  "id": "7e8b7117dd284732b8944e5116d8a0de",
  "ok": false,
  "error": "Unsupported command action: unknown_action",
  "ts": "2026-06-06T10:30:12.456789"
}
```

字段说明：

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `type` | string | 是 | 固定为 `result`。 |
| `id` | string | 是 | 对应 `command.id`。 |
| `ok` | boolean | 是 | 指令是否执行成功。 |
| `data` | object | 成功时是 | 指令结果。失败时可为空对象。 |
| `error` | string | 失败时建议 | 失败原因。 |
| `message` | string | 否 | 补充说明。服务端在 `error` 为空时会读取 `message`。 |
| `ts` | string | 是 | 客户端响应时间，ISO 8601。 |

服务端等待超时后会认为该指令失败，并向业务侧返回“等待外部交易账号响应超时”。

## 10. 指令清单

### 10.1 `get_quotes`

用途：查询标的简要行情。

请求：

```json
{
  "type": "command",
  "id": "req-1",
  "action": "get_quotes",
  "payload": {
    "symbols": ["510300.SH", "159915.SZ"]
  },
  "ts": "2026-06-06T10:30:00"
}
```

`payload` 字段：

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `symbols` | string[] | 是 | 52ETF 标准证券代码列表，格式通常为 `代码.市场`，例如 `510300.SH`、`159915.SZ`。 |

响应：

```json
{
  "quotes": [
    {
      "symbol": "510300.SH",
      "client_symbol": "510300.XSHG",
      "ok": true,
      "price": 3.93,
      "bid": 3.929,
      "bid_size": 10000,
      "ask": 3.93,
      "ask_size": 12000,
      "bid_levels": [{"level": 1, "price": 3.929, "volume": 10000}],
      "ask_levels": [{"level": 1, "price": 3.93, "volume": 12000}],
      "trade_status": "TRADE",
      "timestamp": "2026-06-06 10:30:00"
    }
  ]
}
```

响应字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `quotes` | object[] | 行情数组，与请求标的顺序尽量一致。 |
| `quotes[].symbol` | string | 52ETF 标准证券代码。 |
| `quotes[].client_symbol` | string | 接入方或券商侧证券代码。PTrade A股通常为 `510300.XSHG`、`159915.XSHE`。 |
| `quotes[].ok` | boolean | 单个标的查询是否成功。 |
| `quotes[].error` | string | 单个标的失败原因。 |
| `quotes[].price` | number | 最新价或可用参考价。 |
| `quotes[].bid` | number | 买一价。 |
| `quotes[].bid_size` | integer | 买一量。 |
| `quotes[].ask` | number | 卖一价。 |
| `quotes[].ask_size` | integer | 卖一量。 |
| `quotes[].bid_levels` | object[] | 买盘档位。 |
| `quotes[].ask_levels` | object[] | 卖盘档位。 |
| `quotes[].trade_status` | string | 交易状态，接入方原样或归一化返回。 |
| `quotes[].timestamp` | string | 行情时间。 |

### 10.2 `get_snapshots`

用途：查询标的完整行情快照。比 `get_quotes` 多返回 `raw` 原始字段。

请求：

```json
{
  "type": "command",
  "id": "req-2",
  "action": "get_snapshots",
  "payload": {
    "symbols": ["510300.SH", "159915.SZ"]
  },
  "ts": "2026-06-06T10:30:00"
}
```

响应：

```json
{
  "snapshots": [
    {
      "symbol": "510300.SH",
      "client_symbol": "510300.XSHG",
      "ok": true,
      "price": 3.93,
      "bid": 3.929,
      "ask": 3.93,
      "bid_levels": [{"level": 1, "price": 3.929, "volume": 10000}],
      "ask_levels": [{"level": 1, "price": 3.93, "volume": 12000}],
      "trade_status": "TRADE",
      "timestamp": "2026-06-06 10:30:00",
      "raw": {
        "prod_code": "510300.XSHG",
        "last_px": 3.93
      }
    }
  ]
}
```

响应字段同 `get_quotes`，另包含：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `snapshots[].raw` | object | 接入方原始行情字段，必须保证可 JSON 序列化。 |

### 10.3 `place_orders`

用途：批量提交订单。

请求：

```json
{
  "type": "command",
  "id": "req-3",
  "action": "place_orders",
  "payload": {
    "orders": [
      {
        "client_order_id": "f8a02f8f65044d04b22f532d3ec86f17",
        "symbol": "510300.SH",
        "side": "BUY",
        "quantity": 1000,
        "order_type": "LIMIT",
        "price_level": 1,
        "protection_limit_price": 3.95,
        "remark": "netted_executor"
      }
    ]
  },
  "ts": "2026-06-06T10:30:00"
}
```

订单参数：

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `client_order_id` | string | 有则必须回传 | 52ETF 本地订单 ID。策略执行器下发的订单通常都会带该字段；接入方必须原样回传，不能自行改写。 |
| `symbol` | string | 是 | 52ETF 标准证券代码，例如 `510300.SH`、`159915.SZ`、`AAPL.US`。 |
| `side` | string | 是 | 买卖方向，`BUY` 或 `SELL`。 |
| `quantity` | integer | 是 | 委托数量，必须大于 0。卖出时客户端可按可卖数量裁剪。 |
| `order_type` | string | 否 | `LIMIT` 或 `MARKET`。默认 `LIMIT`。`MKT` 会被视为 `MARKET`。 |
| `price` | number | 否 | 显式委托价格。限价单中可作为 `limit_price` 使用。 |
| `limit_price` | number | 否 | 显式限价。优先级高于自动档位定价。 |
| `price_level` | integer | 否 | 自动定价档位和保护限价模式，支持 `-1`、`0`、`1`、`2`、`3`、`4`、`5`。见“价格档位说明”。 |
| `protection_limit_price` | number | 否 | 保护限价。买入价格不得高于该值，卖出价格不得低于该值。 |
| `protection_limit_source` | string | 否 | 保护限价来源，例如 `reference_price_limit`、`reference_price_with_executor_slippage`。接入方可记录，风控以 `protection_limit_price` 数值为准。 |
| `max_buy_price` | number | 否 | 买入保护限价别名。 |
| `min_sell_price` | number | 否 | 卖出保护限价别名。 |
| `market_limit_price` | number | 否 | 市价单保护限价。 |
| `market_type` | integer | 否 | PTrade 市价委托类型，取值 `0` 到 `5`，默认 `0`。上交所允许 `0/1/2/4`，深交所允许 `0/2/3/4/5`。 |
| `clip_sell_to_available` | boolean | 否 | 是否将卖出数量裁剪到券商可卖数量。当前 52ETF 执行策略和 PTrade 示例客户端都固定按“卖出裁剪到可卖数量”处理。 |
| `replace_count` | integer | 否 | 52ETF 执行器重定价次数，接入方可原样记录。 |
| `deadline_at` | string | 否 | 本轮订单期望超时时间，ISO 8601。 |
| `signal_version` | string | 否 | 52ETF 策略信号版本。 |
| `execution_policy` | object | 否 | 执行策略元数据。接入方通常不需要解析，可原样记录。 |
| `execution_pricing` | string | 否 | 52ETF 执行器定价模式标记，例如 `PTRADE_SNAPSHOT_AT_ORDER_TIME`。接入方可记录或忽略。 |
| `allocations` | object[] | 否 | 52ETF 虚拟子账户分配元数据。真实券商侧只提交当前父订单一笔；接入方不要按 `allocations` 再拆单，除非双方另行约定。 |
| `remark` | string | 否 | 备注。 |

价格档位说明：

这里有两层逻辑需要区分：

- 52ETF 执行器生成订单时，如果能取得有效策略 `reference_price`，会给订单附加 `protection_limit_price`。
- PTrade 示例客户端收到订单后，如果没有显式 `limit_price`，会按 `price_level` 从实时盘口计算一个提交价，然后再用 `protection_limit_price` 做保护。

| `price_level` | 含义 |
| --- | --- |
| `-1` | 被动价。买入取买一价，卖出取卖一价；如果盘口不可用，尝试使用涨跌停价兜底。 |
| `0` | reference price 保护限价模式。执行器如果有有效 `reference_price`，会按 0 滑点生成 `protection_limit_price`。PTrade 示例客户端没有显式 `limit_price` 时，仍会先取对手方最优价（买入取卖一、卖出取买一），再用保护限价约束：买入提交价不高于 `protection_limit_price`，卖出提交价不低于 `protection_limit_price`。如果订单没有 `protection_limit_price`，则只剩盘口即时定价行为。 |
| `1` | 取目标侧 1 档。买入取卖一，卖出取买一。执行器如果有有效 `reference_price`，会按 `executor_max_slippage_pct` 生成保护限价。 |
| `2` | 取目标侧 1 到 2 档中能覆盖数量的最后一档。 |
| `3` | 取目标侧 1 到 3 档中能覆盖数量的最后一档。 |
| `4` | 取目标侧 1 到 4 档中能覆盖数量的最后一档。 |
| `5` | 取目标侧 1 到 5 档中能覆盖数量的最后一档。 |

因此，对执行器下发且带有 `protection_limit_price` 的订单，`price_level=0` 的关键语义不是“单纯追对手方最优价”，而是“用 `reference_price` 做零滑点保护限价”。如果接入方自己实现客户端，应以 `protection_limit_price` 为最终风控边界：买入不得高于它，卖出不得低于它。

默认重定价序列：

```json
[1, 2, 3, 5, -1]
```

如果 `place_orders` 是 A股账号指令，服务端会在下发前按最小价格单位处理价格字段：

- 大多数 A股标的最小价格单位为 `0.01`。
- ETF、可转债等部分前缀标的使用 `0.001`，包括 `10`、`11`、`12`、`13`、`15`、`16`、`18`、`50`、`51`、`52`、`53`、`56`、`58`、`59`。
- 买入价格向下取整到 tick，卖出价格向上取整到 tick。

响应：

```json
{
  "orders": [
    {
      "ok": true,
      "client_order_id": "f8a02f8f65044d04b22f532d3ec86f17",
      "status": "SUCCESS",
      "symbol": "510300.SH",
      "client_symbol": "510300.XSHG",
      "side": "BUY",
      "quantity": 1000,
      "requested_quantity": 1000,
      "submitted_quantity": 1000,
      "quantity_clipped": false,
      "clip_sell_to_available": true,
      "sellable_quantity": null,
      "position_quantity": null,
      "order_type": "LIMIT",
      "protection_limit_price": 3.95,
      "calculated_price": 3.93,
      "price_source": "ask_level_1",
      "price_level": 1,
      "snapshot_time": "2026-06-06T10:30:00",
      "submitted_price": 3.93,
      "order_id": "60535",
      "entrust_no": "60535",
      "raw_status": "0",
      "filled_quantity": 0,
      "avg_fill_price": null,
      "raw_order_info": {},
      "message": "BUY 510300.XSHG, 数量: 1000, 价格: 3.93"
    }
  ]
}
```

响应字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `orders` | object[] | 每笔订单的提交结果。 |
| `orders[].ok` | boolean | 该订单提交是否成功。 |
| `orders[].client_order_id` | string | 如果请求订单包含 `client_order_id`，必须原样回传。 |
| `orders[].status` | string | 提交结果状态。常见为 `SUCCESS`、`FAILED`、`REJECTED`、`NOT_SUPPORTED`。 |
| `orders[].error_code` | string | 失败代码，例如 `BROKER_REJECTED`、`INVALID_LOT_SIZE`、`UNSUPPORTED_MARKET`。 |
| `orders[].retryable` | boolean | 是否可由 52ETF 后续重试。 |
| `orders[].symbol` | string | 52ETF 标准证券代码。 |
| `orders[].client_symbol` | string | 接入方或券商侧证券代码。 |
| `orders[].side` | string | `BUY` 或 `SELL`。 |
| `orders[].quantity` | integer | 实际提交数量。 |
| `orders[].requested_quantity` | integer | 52ETF 原始请求数量。 |
| `orders[].submitted_quantity` | integer | 实际提交给券商的数量。 |
| `orders[].quantity_clipped` | boolean | 是否发生卖出数量裁剪。 |
| `orders[].sellable_quantity` | integer | 券商侧可卖数量。 |
| `orders[].position_quantity` | integer | 券商侧总持仓数量。 |
| `orders[].order_type` | string | `LIMIT` 或 `MARKET`。 |
| `orders[].market_type` | integer | 市价单类型，仅市价单需要。 |
| `orders[].protection_limit_price` | number | 使用的保护限价。 |
| `orders[].calculated_price` | number | 自动计算价格或显式价格。 |
| `orders[].price_source` | string | 价格来源，例如 `explicit_limit_price`、`ask_level_1`、`bid_level_1`、`best_price`。 |
| `orders[].price_level` | integer | 实际使用的价格档位。 |
| `orders[].snapshot_time` | string | 定价所用行情时间。 |
| `orders[].submitted_price` | number | 实际提交价格。 |
| `orders[].order_id` | string | 接入方或券商返回的订单号。 |
| `orders[].entrust_no` | string | 券商委托编号。 |
| `orders[].raw_status` | string | 券商原始状态码。PTrade 常见状态见“订单状态码”。 |
| `orders[].filled_quantity` | integer | 已成交数量。 |
| `orders[].avg_fill_price` | number | 平均成交价。 |
| `orders[].raw_order_info` | object | 券商原始订单信息，必须可 JSON 序列化。 |
| `orders[].message` | string | 给 52ETF 和用户看的说明。 |

接入方必须保证：

- 批量中每笔订单都返回一个结果。
- 不能提交的订单也要返回失败结果。
- 如果发生卖出数量裁剪，要返回 `requested_quantity`、`submitted_quantity`、`quantity_clipped`、`sellable_quantity`、`position_quantity`。
- 如果券商已返回订单号，要填 `order_id` 和尽量填 `entrust_no`。

### 10.4 `cancel_orders`

用途：批量撤单。

请求：

```json
{
  "type": "command",
  "id": "req-4",
  "action": "cancel_orders",
  "payload": {
    "orders": [
      {
        "order_id": "60535",
        "client_order_id": "f8a02f8f65044d04b22f532d3ec86f17"
      }
    ]
  },
  "ts": "2026-06-06T10:30:00"
}
```

请求字段：

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `orders` | object[] | 是 | 撤单列表。 |
| `orders[].order_id` | string | 是 | 券商或接入方订单号。 |
| `orders[].client_order_id` | string | 建议 | 52ETF 本地订单 ID。 |

响应：

```json
{
  "orders": [
    {
      "client_order_id": "f8a02f8f65044d04b22f532d3ec86f17",
      "order_id": "60535",
      "ok": true,
      "status": "CANCEL_REQUESTED",
      "message": "撤单指令已提交"
    }
  ]
}
```

响应字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `orders[].client_order_id` | string | 请求中的本地订单 ID。 |
| `orders[].order_id` | string | 被撤订单号。 |
| `orders[].ok` | boolean | 撤单指令是否提交成功。 |
| `orders[].status` | string | 成功时通常为 `CANCEL_REQUESTED`，失败时为 `FAILED`。 |
| `orders[].message` | string | 说明。 |

### 10.5 `get_account_snapshot`

用途：一次性查询账号快照，包含当日订单、持仓和资产。

请求：

```json
{
  "type": "command",
  "id": "req-5",
  "action": "get_account_snapshot",
  "payload": {},
  "ts": "2026-06-06T10:30:00"
}
```

响应：

```json
{
  "account_id": "vNKpHJkLMnBQRSTUVWXYZabcdefghijkl",
  "identifier": "GS66301027527",
  "backtest": false,
  "current_time": "2026-06-06 10:30:00",
  "orders": [],
  "positions": [],
  "portfolio": {
    "portfolio_value": 1000000.0,
    "available_cash": 200000.0,
    "locked_cash": 0.0,
    "total_cash": 200000.0,
    "total_positions_value": 800000.0,
    "returns": 0.0,
    "starting_cash": 1000000.0
  }
}
```

### 10.6 `get_positions`

用途：查询券商真实持仓。

请求：

```json
{
  "type": "command",
  "id": "req-6",
  "action": "get_positions",
  "payload": {},
  "ts": "2026-06-06T10:30:00"
}
```

响应：

```json
{
  "current_time": "2026-06-06 10:30:00",
  "positions": [
    {
      "symbol": "510300.SH",
      "client_symbol": "510300.XSHG",
      "quantity": 10000,
      "available_quantity": 8000,
      "cost_price": 3.82,
      "last_price": 3.93,
      "market_value": 39300.0,
      "profit": 1100.0,
      "profit_ratio": 0.0288
    }
  ]
}
```

持仓字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `current_time` | string | 持仓快照时间。 |
| `positions[].symbol` | string | 52ETF 标准证券代码。 |
| `positions[].client_symbol` | string | 接入方或券商侧证券代码。 |
| `positions[].quantity` | integer | 总持仓数量。 |
| `positions[].available_quantity` | integer | 可卖数量。A股 T+1 下该字段很重要。 |
| `positions[].cost_price` | number | 成本价。 |
| `positions[].last_price` | number | 最新价。 |
| `positions[].market_value` | number | 市值。 |
| `positions[].profit` | number | 持仓盈亏。 |
| `positions[].profit_ratio` | number | 持仓收益率。 |

### 10.7 `get_assets`

用途：查询资产。

请求：

```json
{
  "type": "command",
  "id": "req-7",
  "action": "get_assets",
  "payload": {},
  "ts": "2026-06-06T10:30:00"
}
```

响应：

```json
{
  "current_time": "2026-06-06 10:30:00",
  "assets": {
    "portfolio_value": 1000000.0,
    "available_cash": 200000.0,
    "locked_cash": 0.0,
    "total_cash": 200000.0,
    "total_positions_value": 800000.0,
    "returns": 0.0,
    "starting_cash": 1000000.0
  }
}
```

资产字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `assets.portfolio_value` | number | 组合总资产。 |
| `assets.available_cash` | number | 可用现金。 |
| `assets.locked_cash` | number | 冻结或不可用现金。 |
| `assets.total_cash` | number | 总现金。 |
| `assets.total_positions_value` | number | 持仓总市值。 |
| `assets.returns` | number | 收益率。 |
| `assets.starting_cash` | number | 起始资金。 |

### 10.8 `get_today_orders`

用途：查询当日订单。

请求：

```json
{
  "type": "command",
  "id": "req-8",
  "action": "get_today_orders",
  "payload": {},
  "ts": "2026-06-06T10:30:00"
}
```

响应：

```json
{
  "current_time": "2026-06-06 10:30:00",
  "orders": [
    {
      "client_order_id": "f8a02f8f65044d04b22f532d3ec86f17",
      "symbol": "510300.SH",
      "client_symbol": "510300.XSHG",
      "side": "BUY",
      "quantity": 1000,
      "price": 3.93,
      "status": "0",
      "order_id": "60535",
      "entrust_no": "60535",
      "filled_quantity": 0,
      "avg_fill_price": null,
      "submitted_at": "2026-06-06 10:30:00",
      "event_time": "2026-06-06T10:30:00",
      "raw": {}
    }
  ]
}
```

订单字段和 `order_event.orders[]` 一致。

### 10.9 `get_deliver`

用途：查询交割单，用于费用和成交对账。

请求：

```json
{
  "type": "command",
  "id": "req-9",
  "action": "get_deliver",
  "payload": {
    "start_date": "20260515",
    "end_date": "20260515"
  },
  "ts": "2026-06-06T10:30:00"
}
```

请求字段：

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `start_date` | string | 是 | 起始日期，支持 `YYYYMMDD` 或 `YYYY-MM-DD`。 |
| `end_date` | string | 是 | 结束日期，支持 `YYYYMMDD` 或 `YYYY-MM-DD`。 |

响应：

```json
{
  "start_date": "20260515",
  "end_date": "20260515",
  "records": [
    {
      "entrust_no": "60535",
      "stock_code": "510300",
      "business_name": "证券买入",
      "business_amount": 1000,
      "business_price": 3.93,
      "business_balance": 3930.0,
      "fare0": 5.0,
      "fare1": 0.0,
      "business_time": 103000
    }
  ]
}
```

`records[]` 可以是券商原始交割单字段，但必须可 JSON 序列化。52ETF 会优先根据 `order_id`/`entrust_no` 匹配本地订单；如果没有订单号，会再尝试用 `symbol`、`side`、交易日期和数量匹配。港股通、组合费、股息税补缴等非证券买卖流水会被记录但跳过订单费用对账。

## 11. 客户端主动上报事件

除响应服务端指令外，客户端可以主动推送以下事件。

主动上报事件当前是 fire-and-forget 模式：服务端收到后写日志、落库并处理，但不会像 `command` 一样返回逐条确认消息。联调时需要通过管理端状态、事件日志、订单状态或服务端日志确认处理结果。

### 11.1 `order_event`

用途：上报订单状态变化。

```json
{
  "type": "order_event",
  "source": "on_order_response",
  "orders": [
    {
      "client_order_id": "f8a02f8f65044d04b22f532d3ec86f17",
      "symbol": "510300.SH",
      "client_symbol": "510300.XSHG",
      "side": "BUY",
      "quantity": 1000,
      "price": 3.93,
      "status": "2",
      "order_id": "60535",
      "entrust_no": "60535",
      "entrust_bs": "1",
      "entrust_type": "0",
      "filled_quantity": 0,
      "avg_fill_price": null,
      "submitted_at": "2026-06-06 10:30:00",
      "event_time": "2026-06-06T10:30:05",
      "raw": {}
    }
  ],
  "ts": "2026-06-06T10:30:05"
}
```

字段说明：

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `type` | string | 是 | 固定为 `order_event`。 |
| `source` | string | 否 | 事件来源，例如 `on_order_response`、`status_sync`。 |
| `orders` | object[] | 是 | 订单事件列表。 |
| `ts` | string | 是 | 上报时间。 |
| `orders[].client_order_id` | string | 有则必须回传 | 52ETF 本地订单 ID。如果原订单或委托响应里已有该字段，必须原样回传。 |
| `orders[].order_id` | string | 建议 | 券商订单号。 |
| `orders[].entrust_no` | string | 建议 | 券商委托编号。 |
| `orders[].symbol` | string | 是 | 52ETF 标准证券代码。 |
| `orders[].client_symbol` | string | 否 | 接入方或券商侧证券代码。 |
| `orders[].side` | string | 是 | `BUY` 或 `SELL`。 |
| `orders[].quantity` | integer | 是 | 委托数量。 |
| `orders[].price` | number | 否 | 委托价格。 |
| `orders[].status` | string | 是 | 券商原始状态码。当前后端按 PTrade 状态码表解释该字段；非 PTrade 接入也应返回兼容状态码，或在联调前约定服务端适配。 |
| `orders[].filled_quantity` | integer | 否 | 已成交数量。 |
| `orders[].avg_fill_price` | number | 否 | 平均成交价。 |
| `orders[].submitted_at` | string | 否 | 委托提交时间。 |
| `orders[].event_time` | string | 否 | 事件发生时间。 |
| `orders[].raw` | object | 否 | 原始订单对象。 |

匹配规则：

1. 优先用 `client_order_id` 匹配本地订单。
2. 如果没有 `client_order_id`，再用 `order_id`、`entrust_no` 匹配。
3. 未匹配事件会被记录为 `UNMATCHED`，不会更新账本。

`order_event` 只用于更新订单生命周期、券商订单号、委托编号等状态信息。即使事件中带了 `filled_quantity`，52ETF 也不会仅凭 `order_event` 生成成交或更新虚拟子账户账本；真实成交必须通过 `trade_event` 上报。

### 11.2 `trade_event`

用途：上报成交回报。该事件会写入成交表，并驱动虚拟子账户账本变化。

```json
{
  "type": "trade_event",
  "source": "on_trade_response",
  "trades": [
    {
      "client_order_id": "f8a02f8f65044d04b22f532d3ec86f17",
      "symbol": "510300.SH",
      "client_symbol": "510300.XSHG",
      "side": "BUY",
      "quantity": 1000,
      "price": 3.93,
      "amount": 3930.0,
      "status": "8",
      "order_id": "60535",
      "entrust_no": "60535",
      "entrust_bs": "1",
      "entrust_type": "0",
      "business_no": "0103000044262638",
      "business_time": "2026-06-06 10:30:05",
      "traded_at": "2026-06-06 10:30:05",
      "raw": {}
    }
  ],
  "ts": "2026-06-06T10:30:05"
}
```

字段说明：

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `type` | string | 是 | 固定为 `trade_event`。 |
| `source` | string | 否 | 事件来源，例如 `on_trade_response`。 |
| `trades` | object[] | 是 | 成交事件列表。 |
| `ts` | string | 是 | 上报时间。 |
| `trades[].client_order_id` | string | 有则必须回传 | 52ETF 本地订单 ID。如果原订单或委托响应里已有该字段，必须原样回传。 |
| `trades[].order_id` | string | 建议 | 券商订单号。 |
| `trades[].entrust_no` | string | 建议 | 券商委托编号。 |
| `trades[].symbol` | string | 是 | 52ETF 标准证券代码。 |
| `trades[].side` | string | 是 | `BUY` 或 `SELL`。 |
| `trades[].quantity` | integer | 是 | 本笔成交数量，必须为正数绝对值。也兼容 `business_amount`、`filled_quantity`。 |
| `trades[].price` | number | 是 | 本笔成交价格。也兼容 `business_price`、`avg_fill_price`。 |
| `trades[].amount` | number | 否 | 本笔成交金额。也兼容 `business_balance`。 |
| `trades[].status` | string | 否 | 券商原始状态码。当前后端只有在该字段为空，或为 PTrade 成交状态 `7`/`8` 时，才会把该条 `trade_event` 当作成交入账；非 PTrade 接入如果没有兼容状态码，建议省略该字段并提供有效 `quantity`、`price` 和稳定流水号。 |
| `trades[].business_no` | string | 强烈建议 | 成交流水号。用于成交去重。也兼容 `business_id`、`deal_no`、`match_no`、`serial_no`。 |
| `trades[].business_time` | string | 否 | 券商成交时间。 |
| `trades[].traded_at` | string | 否 | 成交时间。 |
| `trades[].raw` | object | 否 | 原始成交对象。 |

成交去重规则：

- 优先使用 `fill_key`。
- 然后依次使用 `business_id`、`business_no`、`deal_no`、`match_no`、`serial_no`。
- 如果上述字段都没有，服务端会对整个事件 JSON 计算哈希作为去重键。

因此，接入方应尽量提供稳定唯一的 `business_no` 或 `business_id`。

### 11.3 `deliver_event`

用途：主动上报交割单，用于费用对账。PTrade 示例客户端在 `before_trading_start` 阶段会主动推送最近 15 天到上一日的交割单。

```json
{
  "type": "deliver_event",
  "data": {
    "start_date": "20260501",
    "end_date": "20260515",
    "records": []
  },
  "ts": "2026-06-06T09:25:00"
}
```

字段说明：

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `type` | string | 是 | 固定为 `deliver_event`。 |
| `data.start_date` | string | 建议 | 交割单起始日期，`YYYYMMDD` 或 `YYYY-MM-DD`。 |
| `data.end_date` | string | 建议 | 交割单结束日期，`YYYYMMDD` 或 `YYYY-MM-DD`。 |
| `data.records` | object[] | 推荐 | 交割单原始记录。服务端也兼容 `deliver_records`、`delivers`、`deliveries`、`data` 作为记录数组字段。 |
| `ts` | string | 是 | 上报时间。 |

交割单记录中建议包含：

| 字段 | 含义 |
| --- | --- |
| `order_id`、`entrust_no`、`report_no` | 券商订单或委托编号。 |
| `stock_code` | 证券代码。 |
| `stock_name` | 证券名称。 |
| `business_name`、`business_flag` | 业务名称和业务代码。 |
| `business_amount` | 成交数量。 |
| `business_price` | 成交价格。 |
| `business_balance` | 成交金额。 |
| `fare0`、`fare1`、`exchange_fare*`、`clear_fare0` | 佣金、印花税、交易规费等费用字段。 |
| `business_no`、`business_id`、`serial_no` | 交割或成交流水号。 |
| `business_time`、`entrust_date`、`init_date` | 业务时间和日期。 |

### 11.4 `broker_positions_event`

用途：主动上报券商真实持仓快照。PTrade 示例客户端在 `after_trading_end` 阶段会主动推送收盘持仓。

```json
{
  "type": "broker_positions_event",
  "data": {
    "current_time": "2026-06-06 15:05:00",
    "snapshot_kind": "close",
    "positions": [
      {
        "symbol": "510300.SH",
        "client_symbol": "510300.XSHG",
        "quantity": 10000,
        "available_quantity": 8000,
        "cost_price": 3.82,
        "last_price": 3.93,
        "market_value": 39300.0,
        "profit": 1100.0,
        "profit_ratio": 0.0288
      }
    ]
  },
  "ts": "2026-06-06T15:05:00"
}
```

字段说明同 `get_positions` 响应。`snapshot_kind` 建议取值：

| 值 | 含义 |
| --- | --- |
| `close` | 收盘后快照。 |
| `intraday` | 盘中刷新快照。 |

## 12. 订单状态码

PTrade 示例客户端按以下映射理解原始状态码：

| 原始状态 | 52ETF 生命周期状态 | 含义 |
| --- | --- | --- |
| `0` | `SUBMITTED` | 已提交。 |
| `1` | `SUBMITTED` | 已提交或已报。 |
| `2` | `ACKNOWLEDGED` | 已确认。 |
| `3` | `CANCEL_PENDING` | 撤单中。 |
| `4` | `CANCEL_PENDING` | 撤单中。即使订单事件附带成交数量，成交入账仍依赖 `trade_event`。 |
| `5` | `PARTIALLY_CANCELED` | 部分成交后撤单。 |
| `6` | `CANCELED` | 已撤单。 |
| `7` | `PARTIALLY_FILLED` | 部分成交。 |
| `8` | `FILLED` | 全部成交。 |
| `9` | `FAILED` | 失败或被拒绝。 |
| `+` | `ACKNOWLEDGED` | 已确认。 |
| `-` | `FAILED` | 失败。 |
| `V` | `ACKNOWLEDGED` | 已确认。 |

52ETF 内部活跃状态：

```text
CREATED, SUBMITTED, ACKNOWLEDGED, PARTIALLY_FILLED, CANCEL_PENDING
```

52ETF 内部终态：

```text
FILLED, CANCELED, PARTIALLY_CANCELED, REJECTED, NOT_SUPPORTED, FAILED, EXPIRED,
BLOCKED_INSUFFICIENT_SELLABLE, BLOCKED_INSUFFICIENT_POSITION, BLOCKED_NON_RETRYABLE_REJECTION
```

当前后端按上表解释 `order_event.status` 和 `trade_event.status`。如果接入方不是 PTrade，应优先返回兼容 PTrade 语义的状态码：

- 订单状态：用 `0/1/2/3/4/5/6/7/8/9` 表示提交、确认、撤单、部分成交、全部成交、失败等状态。
- 成交事件：如果填写 `status`，成交必须用 `7` 或 `8`；否则服务端会把它当作非成交状态事件。非 PTrade 接入也可以在 `trade_event` 中省略 `status`，只要 `quantity > 0` 且 `price > 0`。
- 如果接入方必须使用自己的状态码，需要在联调前同步给 52ETF 做服务端适配，不能直接自定义后发送。

## 13. 证券代码规范

52ETF 标准代码：

| 市场 | 示例 | 含义 |
| --- | --- | --- |
| A股沪市 | `510300.SH` | 代码在前，市场后缀为 `SH`。 |
| A股深市 | `159915.SZ` | 代码在前，市场后缀为 `SZ`。 |
| 北交所 | `430000.BJ` | 代码在前，市场后缀为 `BJ`。当前 PTrade 执行器不支持北交所下单。 |
| 美股 | `AAPL.US` | 代码在前，市场后缀为 `US`。 |

对接 52ETF 时，美股 `symbol` 必须使用 `.US` 后缀，例如 `AAPL.US`、`SOXL.US`、`QQQ.US`。裸代码如 `AAPL` 只适合作为接入方内部或券商侧代码，不建议作为传给 52ETF 的标准 `symbol`。

PTrade A股常见代码：

| 52ETF | PTrade |
| --- | --- |
| `600000.SH` | `600000.XSHG` |
| `510300.SH` | `510300.XSHG` |
| `000001.SZ` | `000001.XSHE` |
| `159915.SZ` | `159915.XSHE` |
| `430000.BJ` | `430000.XBSE` |

接入方可以使用自己的 `client_symbol`，但返回给 52ETF 的 `symbol` 应始终使用 52ETF 标准代码。例如富途侧可以用 `US.AAPL` 作为 `client_symbol`，IB 侧可以用 `AAPL` 作为券商合约代码，但回传给 52ETF 的 `symbol` 应为 `AAPL.US`。

## 14. 时间和日期格式

建议格式：

| 场景 | 格式 | 示例 |
| --- | --- | --- |
| WebSocket 握手 `ts` | Unix 秒级时间戳字符串 | `1780732800` |
| 普通事件时间 | ISO 8601 或 `YYYY-MM-DD HH:MM:SS` | `2026-06-06T10:30:00` |
| 交割单日期 | `YYYYMMDD` 或 `YYYY-MM-DD` | `20260515` |
| 纯时间 | `HH:MM:SS` | `10:30:00` |

服务端可解析：

- `YYYY-MM-DD HH:MM:SS`
- `YYYYMMDDHHMMSS`
- `HH:MM:SS`
- ISO 8601
- `YYYY-MM-DD`
- `YYYYMMDD`
- `YYYY/MM/DD`

## 15. 交易时段限制

服务端对以下指令会做交易时段检查：

```text
get_quotes
get_snapshots
place_orders
cancel_orders
get_account_snapshot
get_positions
get_assets
get_today_orders
get_deliver
```

A股账号：

- 交易日 09:30 到 11:30。
- 交易日 13:00 到 15:00。
- 交易日判断优先使用项目内交易日历，失败时工作日按交易日处理。
- 非交易时段服务端拒绝下发指令，业务侧会收到类似“当前非A股开盘时段，拒绝发送 ... 指令到外部交易客户端”的错误。

美股账号：

- 当前 `send_command` 对 `US_STOCK` 不做这层拦截。
- 具体下单和行情限制由接入方或券商环境自行控制。

## 16. 错误处理和重连

客户端建议策略：

- 连接失败或断开后 5 秒重连。
- 本机时间必须和标准时间同步，否则握手签名会过期。
- 每次重连必须生成新的 `ts`、`nonce`、`signature`。
- 收到未知 `command.action` 时返回 `ok=false`。
- 指令执行异常时返回 `ok=false` 和清晰的 `error`。
- 券商 API 返回的原始对象要放入 `raw` 或 `raw_order_info`，但必须确保可 JSON 序列化。
- 不要在日志里打印完整 `signature`、密钥、私钥或共享密钥。

服务端连接行为：

| 场景 | 行为 |
| --- | --- |
| 新连接使用相同外部账号 | 新连接替换旧连接，旧连接 close code `4000`。 |
| 服务端主动断开账号 | close code `4001`。 |
| 鉴权失败 | close code `1008`。 |
| WebSocket 发送失败 | 服务端标记账号断开。 |
| 等待响应超时 | 服务端清理 pending 请求，业务侧报超时。 |

## 17. 推荐实现步骤

1. 从 52ETF 获取联调环境地址、`account_id`、`identifier`、RSA 私钥或示例客户端、共享密钥。
2. 实现 WebSocket 握手签名。
3. 实现 `secure` 加解密信封。
4. 连上 WebSocket，确认收到 `connected`。
5. 实现心跳，确认能收到 `pong`。
6. 实现 `get_positions`、`get_assets`、`get_snapshots` 三个只读指令。
7. 实现 `place_orders`，先使用模拟账号或极小测试单验证返回字段。
8. 实现 `cancel_orders`。
9. 实现 `order_event` 和 `trade_event` 主动上报，重点验证 `client_order_id` 匹配。
10. 实现 `deliver_event` 和 `broker_positions_event`。
11. 做断线重连、重复事件、订单失败、撤单、部分成交等异常场景联调。

## 18. 最小联调用例

### 18.1 连接和心跳

预期：

- 客户端连接成功。
- 服务端返回 `connected`。
- 客户端发送 `heartbeat`。
- 服务端返回 `pong`。
- 管理端账号状态显示已连接，`last_seen_at` 持续更新。

### 18.2 查询持仓

服务端下发：

```json
{
  "type": "command",
  "id": "req-positions",
  "action": "get_positions",
  "payload": {},
  "ts": "2026-06-06T10:30:00"
}
```

客户端返回：

```json
{
  "type": "result",
  "id": "req-positions",
  "ok": true,
  "data": {
    "current_time": "2026-06-06 10:30:00",
    "positions": []
  },
  "ts": "2026-06-06T10:30:01"
}
```

### 18.3 限价下单

服务端下发 `place_orders`，客户端返回：

- `ok=true`
- `client_order_id` 原样回传
- `order_id` 有值
- `entrust_no` 有值
- `raw_status` 有值

随后客户端主动上报 `order_event`，服务端能匹配并更新订单状态。

### 18.4 成交回报

客户端上报 `trade_event`，至少包含：

- `client_order_id`
- `order_id` 或 `entrust_no`
- `symbol`
- `side`
- `quantity`
- `price`
- `business_no` 或 `business_id`
- `traded_at`

预期：

- 服务端写入成交记录。
- 虚拟子账户账本持仓和现金发生变化。
- 重复发送同一 `business_no` 不会重复入账。

## 19. PTrade 示例客户端

项目内已有 PTrade 示例客户端：

```text
52etf/ptrade/ptrade_client.py
```

本地模拟运行器：

```text
52etf/ptrade/ptrade_client_simulator.py
```

模拟器示例：

```bash
cd /Users/sevenshal/Dev/github/quant/52etf/ptrade
../../.venv/bin/python ptrade_client_simulator.py --host localhost:8001
```

自检：

```bash
cd /Users/sevenshal/Dev/github/quant/52etf/ptrade
../../.venv/bin/python ptrade_client_simulator.py --self-test --log-level ERROR
```

PTrade 正式接入时需要修改示例客户端中的：

| 常量 | 含义 |
| --- | --- |
| `USE_HTTPS` | 是否使用 `wss`。 |
| `API_HOST` | 52ETF API 域名。 |
| `DEFAULT_ACCOUNT_ID` | 52ETF 内部用户账号 ID。 |
| `DEFAULT_IDENTIFIER` | 外部交易账号唯一标识。 |
| `HEARTBEAT_INTERVAL_SECONDS` | 心跳间隔。 |
| `RECONNECT_DELAY_SECONDS` | 重连等待时间。 |
| `DISABLE_AUTO_WEBSOCKET` | 是否禁用自动 WebSocket。 |
| `COMMAND_QUEUE_TIMEOUT_SECONDS` | 命令在本地队列等待 PTrade 回调处理的最长时间。 |
| `RSA_N_HEX`、`RSA_D_HEX` | 握手签名 RSA 密钥材料。 |
| `SHARED_KEY_B64` | 业务消息加密共享密钥。 |

## 20. 接入方交付清单

接入方联调完成前，请确认以下项目：

- 已能连接 52ETF WebSocket 并收到 `connected`。
- 心跳稳定，断线能自动重连。
- 所有业务消息都使用 `secure` 信封。
- `get_snapshots` 能返回买卖盘和最新价。
- `get_positions` 能返回总持仓和可卖数量。
- `get_assets` 能返回可用现金和总资产。
- `place_orders` 能正确返回 `client_order_id`、`order_id`、`entrust_no`、`raw_status`。
- `cancel_orders` 能正确返回撤单结果。
- `order_event` 能主动上报订单状态变化。
- `trade_event` 能主动上报成交，且包含稳定唯一的成交流水号。
- `deliver_event` 能在交易日前或开盘前推送交割单。
- `broker_positions_event` 能在收盘后推送券商真实持仓。
- 重复成交事件不会重复入账。
- 非交易时段、券商拒单、部分成交、撤单、断线重连等异常场景已联调。
