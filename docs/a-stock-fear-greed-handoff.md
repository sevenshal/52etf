# A 股自算贪恐计算与复现交接

本文只描述项目当前实际运行的 A 股自算贪恐实现，目标是让没有上下文的工程师或智能体能够：

1. 从代码和数据库还原任意已配置 A 股指数的日频贪恐值；
2. 判断复算结果为什么与生产不一致；
3. 安全地增加一个指数或替换某个分项；
4. 区分“贪恐分数”“盘中快照”和“曲线顶底信号”三层逻辑。

本文对应的核心实现是
`backend/src/core/services/a_stock_fear_greed_clone_service.py`。项目把结果叫做
“clone”，因为它借用了 CNN Fear & Greed 的七分项结构，但 A 股原始指标和归一化都是项目自己的，
不是 CNN 未公开公式的逆向结果。

---

## 1. 一句话口径

对目标 A 股指数的 7 个原始情绪指标分别做“滚动 z-score → 标准正态 CDF → 0～100 分”，
再对当日可用的分项分数做等权平均；至少 6 个分项有效才产出最终分数。

分数越低越恐惧，越高越贪婪。评级区间为：

| 分数 | rating |
|---:|---|
| `< 25` | `extreme fear` |
| `25 <= score < 45` | `fear` |
| `45 <= score <= 55` | `neutral` |
| `55 < score <= 75` | `greed` |
| `> 75` | `extreme greed` |

默认参数：

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `history_days` | 550 | 每次增量复算向前取的自然日数 |
| `score_window` | 252 | 每个分项归一化的最大交易日窗口 |
| `min_periods` | 120 | 该分项开始产生归一化分数所需的最少有效观察数 |
| `A_STOCK_MIN_COMPONENT_COUNT` | 6 | 当日最终分数至少需要的有效分项数 |

定时任务默认每天只回写最近 3 个自然日，但会从输出起点再向前取 550 个自然日做预热计算。

---

## 2. 代码与数据流

### 2.1 单一入口和配置

| 职责 | 文件/对象 |
|---|---|
| 指数清单、期权代理、场内 ETF 代理 | `backend/src/robot/a_stock_base_data_config.py` |
| A 股计算器、7 个原始指标、归一化、入库 | `backend/src/core/services/a_stock_fear_greed_clone_service.py` |
| 通用正态 CDF、评级 | `backend/src/core/services/fear_greed_clone_service.py` |
| 日频历史读取、代理 ETF 量价替换、顶底信号 | `backend/src/core/services/etf_fear_greed_clone_service.py` |
| 定时任务 | `backend/src/robot/scheduled_tasks.py` |
| API | `backend/src/app/api/cnn.py` |
| SQLite 结果模型 | `backend/src/core/database.py` |

当前配置由 64 个公开指数和 2 个项目自算指数组成：

- 公开指数登记在 `A_STOCK_INDEX_FEAR_GREED_TARGETS`；
- 自算指数是 `INNO100.CN`（A 创 100）和 `MICRO400.CN`（微盘 400）；
- 汇总后的唯一注册表是 `A_STOCK_FEAR_GREED_TARGETS`，不要在别处维护第二份指数清单；
- 每项至少包含 `symbol/ticker/label/index_name/option_underlyings`，多数公开指数另有
  `proxy_etf`。完整清单经常变化，应以注册表为准，不要把本文中的数量当永久常量。

### 2.2 日频主链路

```text
A股基础数据同步 / 自算指数刷新
  ├─ 指数日线
  ├─ 历史成分权重
  ├─ 成分股前复权日线
  ├─ 期权合约与日成交量
  ├─ 中债收益率曲线
  └─ 中证国债指数日线
        ↓
AStockInnovation100FearGreedCloneCalculator.calculate_history()
  ├─ 按每个指数交易日选择当时已经生效的成分快照
  ├─ 计算 7 个 raw signal
  ├─ 每项 rolling z-score → normal CDF → 0~100
  ├─ 可用项等权平均，要求 >= 6 项
  └─ backfill_to_db() merge 入 SQLite
        ↓
etf_fear_greed_clone_history（主键 symbol + date）
        ↓
/api/cnn/etf-fear-greed-clone/history|summaries
```

公开指数的行情和成分来自 DuckDB；两个自算指数的点位、调仓和成分来自 SQLite 中各自的三张表。
无论哪种来源，后续 7 分项算法一致。

---

## 3. 七个原始分项的精确公式

以下所有滚动窗口都是目标指数交易日序列上的窗口。`L_t` 是目标指数点位，`w_i,t` 是在 t 日有效的
成分权重。权重配置以百分数存储，加载时除以 100 转成 0～1。

### 3.1 市场动量 `market_momentum`

```text
raw_t = L_t / mean(L[t-124:t]) - 1
```

- 使用目标指数自己的收盘点位；
- 125 日均线没有显式 `min_periods`，必须满 125 个观察值；
- 正值代表指数在长期均线上方，方向为贪婪。

### 3.2 成分价格强度 `stock_price_strength`

先对每只成分股计算其在 52 周高低区间的位置：

```text
high252_i,t = max(high_i, last 252 days, min_periods=120)
low252_i,t  = min(low_i,  last 252 days, min_periods=120)
position_i,t = clip((close_i,t - low252_i,t) /
                    (high252_i,t - low252_i,t), 0, 1)
raw_t = sum(position_i,t * w_i,t) / sum(valid w_i,t)
```

关键细节：

- 成分股行情读 DuckDB 视图 `a_stock_market_daily_qfq`；
- `high/low/close` 对目标指数交易日重建索引后向前填充；
- 一只股票在计算区间内的 `close` 有效数少于 120 时整只剔除；
- 当日只对能算出 `position` 的成分重新归一化权重，不把缺数据股票按 0 分处理；
- 分母为 0 时该分项为缺失。

### 3.3 成分价格广度 `stock_price_breadth`

先按涨跌方向汇总成分股“权重 × 成交额”：

```text
up_t   = sum(amount_i,t * w_i,t for pct_chg_i,t > 0)
down_t = sum(amount_i,t * w_i,t for pct_chg_i,t < 0)
ratio_t = up_t / (up_t + down_t)
raw_t = mean(ratio[t-4:t], min_periods=3)
```

关键细节：

- 平盘股不进入 `up` 或 `down`；
- 缺失成交额在加载时填 0，`pct_chg` 不向前填充；
- 这是 5 日平均的上涨成交额占比，不是上涨家数占比；
- 分子分母都带成分权重；最终做比值，所以总权重不必恰好等于 1。

### 3.4 认沽/认购期权 `put_call_options`

单条被跟踪指数的日 PCR：

```text
PCR_g,t = sum(put volume for tracked-index group g) /
          sum(call volume for tracked-index group g)
```

目标指数需要借多个期权指数代理时，按目标指数成分在各代理指数中的权重覆盖率混合：

```text
coverage_g,s = sum(target constituent weight covered by proxy g at snapshot s)
blended_PCR_t = sum(PCR_g,t * coverage_g,s) / sum(available coverage_g,s)
raw_t = -mean(blended_PCR[t-4:t], min_periods=3)
```

方向取负号，因为 put/call 越高通常越恐惧。

期权映射在 `OPTION_UNDERLYING_TRACKED_INDEX`。同一指数的多个期权标的先把 call、put 成交量分别
加总后再相除，例如沪深 300 的 IO、沪市 300ETF 期权、深市 300ETF 期权属于同组。

三种特殊情况：

1. 有自己期权的指数只使用自己的组，例如中证 1000 只用 `OP000852.SH`；
2. 中证全指配置哨兵 `"*"`，直接以全 A 股期权市场总 put 成交量除以总 call 成交量；
3. 借代理的指数与所有代理成分重叠均为 0 时，该项缺失，不能退回等权凭空造信号。北证 50 是典型。

只有在完全拿不到目标成分快照、无法计算覆盖率时，多代理 PCR 才退回“代理组等权”，不是按期权
成交量加权。日 PCR 对齐到指数交易日后最多向前填 3 天。

### 3.5 市场波动 `market_volatility`

```text
r_t = pct_change(L_t)
rv20_t = std(r[t-19:t]) * sqrt(252)       # pandas 默认样本标准差 ddof=1
raw_t = -(rv20_t / mean(rv20[t-49:t]) - 1)
```

- 20 日已实现波动率相对自身 50 日均值越高越恐惧，因此取负号；
- 20 日和 50 日窗口都必须满窗。

### 3.6 避险需求 `safe_haven_demand`

```text
raw_t = pct_change(L_t, 20) - pct_change(B_t, 20)
```

`B_t` 固定使用 `H11006.CSI` 中证国债指数收盘点位，并对目标指数交易日向前填充。股票指数
20 日收益跑赢国债代表 risk-on，方向为贪婪。

### 3.7 信用利差需求 `junk_bond_demand`

从中债收益率曲线取期限恰好为 3 年的三类曲线：

- 中期票据 `medium_note`；
- 企业债 `enterprise_bond`；
- 城投债 `urban_investment_bond`。

每类先算 `AA - AAA`，再对当天可用类别求均值：

```text
spread_pair,t = yield_AA,pair,t - yield_AAA,pair,t
credit_spread_t = mean(spread_pair,t across 3 pairs)
raw_t = -credit_spread_t
```

利差越窄越贪婪，所以取负号。对齐到指数交易日后最多向前填 3 天。

---

## 4. 从 raw signal 到最终 0～100 分

对每个分项 `x` 独立处理：

```text
mu_t    = rolling_mean(x, window=252, min_periods=120)
sigma_t = rolling_std(x, window=252, min_periods=120, ddof=0)
z_t     = (x_t - mu_t) / sigma_t
component_score_t = clip(100 * Phi(z_t), 0, 100)
```

其中 `Phi` 是标准正态累积分布函数：

```text
Phi(z) = 0.5 * (1 + erf(z / sqrt(2)))
```

注意这些容易导致“公式看起来一样、结果却对不上”的细节：

- 归一化窗口包含当日；
- 标准差明确使用总体标准差 `ddof=0`；
- 不做 winsorize、不截 z-score，只把 CDF 结果限制到 0～100；
- 每个分项按自己的非空值满足 `min_periods=120`，不是要求 120 个连续日；
- 原始指标的预热窗口还会叠加在 120 日归一化预热之前，因此只有 120 日原始行情远远不够；
- 原始序列标准差为 0 时该分项分数是缺失，不是 50；
- 最终分数按当日非空分项直接算算术平均，不重新设置固定权重。

最终公式：

```text
available_t = {component_score_k,t | finite}
score_t = mean(available_t), only if len(available_t) >= 6
```

数据库保存 4 位小数；每个 component payload 中的分项分数显示为 2 位小数，但复合分数使用舍入前的
浮点值计算。

---

## 5. point-in-time 成分股处理

复现历史时绝不能用当前成分覆盖全历史。

### 5.1 公开指数

`a_stock_index_weight` 按 `index_code + trade_date` 保存成分快照。对每个目标指数交易日 t，选择
`trade_date <= t` 的最新一份快照。`holdings_as_of` 就是实际采用的快照日。

### 5.2 自算指数

`INNO100.CN` 和 `MICRO400.CN` 分别从 SQLite 的 level/rebalance/constituent 三张表读取。
按 `effective_date` 生效；若为空则退回 `rebalance_date`。加载时只取覆盖计算窗口起点的最近一期和
窗口内后续调仓，避免把高频调仓指数的全表读入内存。

### 5.3 成分权重缺失

- 整个区间没有可用成分快照：直接报错，不产出结果；
- 某日还没有任何已生效快照：该日没有成分相关分项；
- 强度分项会对当日有效股票重新归一化；
- 广度分项只累计有效行情，不能把缺行情股票算跌或算平。

---

## 6. 数据表和字段来源

### 6.1 DuckDB 分析库

| 表/视图 | 用途 | 关键列 |
|---|---|---|
| `a_stock_index_daily` | 公开指数 OHLC/点位/量额；中证国债点位 | `ts_code, trade_date, open, high, low, close, pct_chg, vol, amount` |
| `a_stock_index_weight` | point-in-time 指数成分与权重 | `index_code, trade_date, con_code, weight` |
| `a_stock_market_daily_qfq` | 成分股前复权日线 | `trade_date, ts_code, high, low, close, pct_chg, amount` |
| `a_stock_option_basic` | 期权合约对应的期权标的和 C/P | `ts_code, opt_code, call_put` |
| `a_stock_option_daily` | 期权合约日成交量 | `trade_date, ts_code, vol` |
| `a_stock_chinabond_yield_curve_defs` | 中债曲线类别、评级 | `curve_id, pair_key, rating` |
| `a_stock_chinabond_yield_curve_daily` | 曲线期限和收益率 | `trade_date, curve_id, term, yield_rate` |
| `a_stock_fund_daily[_qfq]` | 历史接口展示和量能信号使用的代理 ETF 日线 | `ts_code, trade_date, OHLC, vol, amount` |

### 6.2 SQLite 主库

最终日频结果统一写入 `etf_fear_greed_clone_history`，主键是 `(symbol, date)`。表中同时保存：

- 最终 `score/rating`；
- 7 个分项各自的 raw 和 score；
- 完整 `components` JSON；
- 当日指数 OHLC/量额（字段名沿用了通用 ETF 表的 `etf_*`）；
- `holdings_as_of/count/weight_used`；
- 本次计算参数和 warnings。

盘中结果单独写 `a_stock_fear_greed_intraday`，不会覆盖日频最终值。

两个自算指数另有各自的 level/rebalance/constituent 表；具体 ORM 由
`CUSTOM_INDEX_SOURCES` 注册，不要在计算器里按 symbol 写分支。

---

## 7. 日频、盘中和曲线信号不是一回事

### 7.1 日频最终值

18:40 的定时任务 `a_stock_etf_fear_greed_backfill` 默认执行全部注册指数：

- 结束日默认今天；
- 输出起点默认今天减 3 个自然日；
- 计算起点 = 输出起点减 550 个自然日；
- 每个指数独立捕获错误，一个失败不会阻断后面的指数；
- `backfill_to_db()` 用 `merge` 幂等覆盖 `(symbol, date)`。

### 7.2 12:00 盘中快照

盘中调用同一套 `_build_raw_signals()` 和 `_score_raw_signals()`，不是另一套近似公式。区别只是把今天
这一行替换为：

- 指数点位优先取 Tushare `rt_idx_k`；必要时可用代理 ETF 映射；
- 成分股 high/low/close/pct_chg/amount 用实时行情补今天；
- 期权 PCR、信用利差、中证国债等日频源沿用最近可用值。

`INNO100.CN`、`MICRO400.CN` 和 `899050.BJ` 当前不跑盘中任务，因为没有可用的实时指数源或场内
代理。盘中结果独立入库，仅在当日摘要卡上叠加展示。

### 7.3 历史曲线的“均线底/顶、放量底、缩量顶”

这些是分数算完以后由 `compute_turn_signals()` 派生的交易标记，不参与贪恐分数本身。
历史 API 会先加载完整历史、计算信号和冷却，再应用请求的日期过滤。

默认规则（实际值优先读全局表 `fear_greed_signal_configs`）：

- 均线底：固定 MA5 在昨日形成局部低点，最近 5 个交易日中分数曾 `<= 25`；
- 均线顶：固定 MA5 在昨日形成局部高点，最近 5 个交易日中分数曾 `>= 75`；
- 放量底：分数 `<= 30`，且当日 log(volume) 相对之前 20 个有效交易日的样本标准差 z-score `> 1.25`；
- 缩量顶：分数 `>= 75`，且同口径 z-score `< -0.25`；
- 四种 signal kind 分别独立冷却，默认信号后 5 个交易日内同类不重复。

有 `proxy_etf` 的 A 股指数，历史详情的 K 线与量能信号优先使用代理 ETF：

- 普通指数：完整覆盖历史区间时，用代理 ETF 前复权 OHLC 和量额替换展示数据；
- 中证全指：价格仍用指数点位，成交量用沪深 300 + 中证 500 + 中证 1000 + 中证 2000 四只
  宽基 ETF 的成交额加总；
- 代理 ETF 不能完整覆盖整个历史区间时，不做价格替换，避免指数点位与 ETF 价格在同图混用；
- 这层替换不反向改变已入库的 7 分项和最终贪恐值，只影响历史展示、量比和量能顶底信号。

---

## 8. 可复现步骤

### 8.1 前置条件

先确保“A股基础数据同步”已经覆盖目标区间，至少包含第 6 节列出的 DuckDB 表。自算指数还要先执行
对应的“A股创新 100 指数刷新”或“A股微盘 400 指数刷新”。

本地运行必须显式指定开发库，避免误连生产路径：

```bash
cd backend
export QUANT_SQLITE_PATH="$HOME/.local/share/quant_dev/evc_stocks.db"
export ANALYTICS_DB_PATH="$HOME/.local/share/quant_dev/analytics.duckdb"
```

### 8.2 只计算、不写库

以下例子复算沪深 300。要精确复现某个生产日，必须固定 `start_date/end_date/output_start_date` 和三个
计算参数，不能依赖“今天”。

```bash
../.venv/bin/python - <<'PY'
from datetime import date, timedelta
from src.core.services.a_stock_fear_greed_clone_service import (
    AStockInnovation100FearGreedCloneCalculator,
)

symbol = "000300.SH"
output_start = date(2026, 9, 1)
end = date(2026, 9, 30)
history_days = 550

result = AStockInnovation100FearGreedCloneCalculator(symbol).calculate_history(
    start_date=output_start - timedelta(days=history_days),
    end_date=end,
    output_start_date=output_start,
    history_days=history_days,
    score_window=252,
    min_periods=120,
)
for row in result["records"][-5:]:
    print(row["date"], row["score"], row["component_count"], row["components_used"])
PY
```

### 8.3 计算并幂等入库

确认连接的是开发库后，把上例的 `calculate_history()` 换成：

```python
calculator.backfill_to_db(
    start_date=output_start - timedelta(days=550),
    end_date=end,
    output_start_date=output_start,
    history_days=550,
    score_window=252,
    min_periods=120,
)
```

生产环境不要手工运行脚本或 SQL；应由“A 股指数贪恐回跑入库”定时任务或部署后的既有调度执行。

### 8.4 校验一条结果

对某个 `(symbol, date)` 至少核对：

1. `holdings_as_of` 是否为当日之前最近一份，而不是未来或当前最新成分；
2. `components` 中每个 `raw_value/score/used_in_score`；
3. `component_count >= 6`；
4. 重新计算的最终 `score` 是否等于所有非空 component score 的未舍入等权均值；数据库 JSON 中的
   component score 已保留 2 位小数，不能直接用这些展示值要求 4 位小数完全相等；
5. 期权项是自有期权、全市场，还是按成分重叠加权的代理；
6. 复算起点是否留足原始指标和归一化的双重预热期；
7. 请求历史 API 时是否发生了代理 ETF 展示替换，不要拿展示 K 线反推指数动量分项。

---

## 9. 最小等价伪代码

另一个实现只要数据输入一致，下面的流程应能复现生产日频分数：

```python
levels = load_target_index_levels(calc_start, end)
holdings_by_day = latest_effective_snapshot_on_or_before_each_index_day()
stocks = load_qfq_constituent_bars(holdings_by_day, calc_start, end)

raw = DataFrame(index=levels.index)
raw["market_momentum"] = levels.close / levels.close.rolling(125).mean() - 1
raw["stock_price_strength"] = constituent_weighted_252d_range_position(
    stocks, holdings_by_day, min_periods=120
)
raw["stock_price_breadth"] = constituent_weighted_up_amount_ratio(
    stocks, holdings_by_day
).rolling(5, min_periods=3).mean()
raw["put_call_options"] = -blended_pcr.rolling(5, min_periods=3).mean()
rv20 = levels.close.pct_change().rolling(20).std() * sqrt(252)
raw["market_volatility"] = -(rv20 / rv20.rolling(50).mean() - 1)
raw["safe_haven_demand"] = levels.close.pct_change(20) - bond.close.pct_change(20)
raw["junk_bond_demand"] = -credit_spread_3y_aa_minus_aaa

for key in seven_components:
    mu = raw[key].rolling(252, min_periods=120).mean()
    sd = raw[key].rolling(252, min_periods=120).std(ddof=0)
    z = (raw[key] - mu) / sd.replace(0, NaN)
    score[key] = clip(100 * normal_cdf(z), 0, 100)

component_count = score.notna().sum(axis=1)
fear_greed = score.mean(axis=1, skipna=True)
valid = fear_greed.notna() & (component_count >= 6)
```

---

## 10. 高频误区

1. **把代理 ETF 当作基础分数的价格源。** 基础 7 分项的动量、波动和避险收益使用目标指数点位；
   代理 ETF 主要用于历史展示和量能信号。
2. **全历史使用当前成分。** 这会引入严重幸存者偏差，尤其会污染强度、广度和期权代理权重。
3. **把所有代理期权成交量直接相加。** 代理指数间必须按目标成分覆盖率混合；成交量大的期权不能天然
   获得更高权重。
4. **强制要求 7 项齐全。** A 股生产口径允许 6/7；北证 50 等可能天然没有有效期权项。
5. **把缺失分项填 50 或 0。** 生产实现保持 NaN，最终只平均可用项。
6. **归一化使用 `ddof=1`。** 分项 z-score 的标准差必须是 `ddof=0`；只有已实现波动率和量能信号
   各自使用样本标准差。
7. **只向前取 120 日。** 原始指标本身有 125/252 日窗口，然后才进入至少 120 点的 score 窗口。
8. **把量能顶底信号混进分数。** 放量/缩量只是在分数之后打标，不是第八个 component。
9. **按自然日解释所有窗口。** 550 是自然日回溯参数；125/252/20/50/5 和 cooldown 都是数据序列中的
   交易日观察数。
10. **用 API 返回的代理 ETF K 线重算 market momentum。** API 可能替换展示价格，入库 component
    仍来自指数点位。

---

## 11. 修改或新增指数的检查清单

新增公开指数时：

1. 在 `A_STOCK_INDEX_FEAR_GREED_TARGETS` 登记，不要直接改汇总表；
2. 确认 `a_stock_index_daily` 能同步该指数且有足够历史；
3. 确认 `a_stock_index_weight` 有 point-in-time 权重；
4. 有自己的期权就精确配置 `option_underlyings`；没有则使用统一代理候选集，让运行时按成分重叠定权；
5. 有合适场内 ETF 时配置 `proxy_etf`，并确认基础数据同步的代理 ETF 集合随配置更新；
6. 至少验证正常 7 项、缺期权 6 项、历史成分切换、代理 ETF 上市较晚四种情况；
7. 更新 `backend/tests/test_a_stock_fear_greed_config.py`，期权口径变化还要更新
   `backend/tests/test_a_stock_fear_greed_option_pcr.py`；
8. 不要因为新增指数而复制计算器。

新增项目自算指数时，仿照 `CUSTOM_INDEX_SOURCES` 登记 level/rebalance/constituent ORM 和刷新任务，
继续复用同一个计算器。

---

## 12. 现有测试与推荐验收

相关测试：

- `backend/tests/test_a_stock_fear_greed_config.py`：指数注册、期权和代理 ETF 配置；
- `backend/tests/test_a_stock_fear_greed_option_pcr.py`：全市场、自有期权、成分重叠加权、零重叠、等权兜底；
- `backend/tests/test_a_stock_fear_greed_intraday.py`：盘中复用同一计算链和 7 分项；
- `backend/tests/test_etf_fear_greed_signals.py`：MA5/量能顶底信号和冷却；
- `backend/tests/test_a_stock_micro400.py`：自算指数接入和定时任务参数。

从 `backend/` 运行：

```bash
../.venv/bin/python -m pytest \
  tests/test_a_stock_fear_greed_config.py \
  tests/test_a_stock_fear_greed_option_pcr.py \
  tests/test_a_stock_fear_greed_intraday.py \
  tests/test_etf_fear_greed_signals.py \
  tests/test_a_stock_micro400.py -q
```

建议再选三类真实数据做数值验收：

- `000300.SH`：有自己的多标的期权组，应有 7 项；
- `000985.SH`：全市场期权 + 聚合 ETF 成交额特殊口径；
- `899050.BJ`：与代理期权指数零重叠，应稳定以 6 项计算且不跑盘中。

如果只是重构，验收标准应是固定数据库快照、固定日期和参数下，每日 raw、component score、最终 score
逐字段一致，而不只是趋势看起来相似。
