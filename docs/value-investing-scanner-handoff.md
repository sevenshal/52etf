# 价值投资选股扫描器 — 工作交接文档

**目的**：本文档面向 review 本次会话产出代码的另一个模型/工程师，不假设读者看过原始对话。
交付物是一条端到端的链路：tushare 财务报表数据同步 → DuckDB 缓存 → 质量闸门 + DCF 估值扫描 →
API → 前端页面。请重点核查「5. 需要重点 review 的设计决策」和「6. 已验证 vs 未验证」两节，
这是最可能出问题、也是我自己把握最不足的地方。

---

## 1. 背景与原始需求

用户希望用 tushare 的利润表 / 资产负债表 / 现金流量表数据做价值投资选股：找出基本面与股价
出现偏离的股票，并算出一个"潜在投资回报率 return%"。这个仓库（52ETF 量化交易系统）在本次
会话开始前：

- 只同步了利润表的极小一部分字段（`operate_income`、`rd_exp`），服务于一个不相关的"研发动量"因子；
- 完全没有同步资产负债表、现金流量表；
- 财务指标（ROE/毛利率/负债率等）只在展示个股信息时临时拉取 `eps`/`bps` 两个字段，不入库、无历史。

本次会话从零搭建了完整的数据管道和一个基于 DCF/ROIC-WACC 框架的扫描引擎，并在真实生产环境
试跑后修复了一个会让结果离谱的模型缺陷。

## 2. 交付物清单（已合并的 PR）

全部已合并到 `master`：

| PR | 内容 |
|---|---|
| [#14](https://github.com/sevenshal/52etf/pull/14) | 新增资产负债表/现金流量表/财务指标同步管道；利润表补全字段；价值投资扫描器 v1（估值均值回归/E-P/FCF收益率取中位数的简化版）；API + 前端页面；tushare 权限验证脚本 |
| [#15](https://github.com/sevenshal/52etf/pull/15) | 在"A股基础数据同步"任务的执行结果摘要里补上新增3张表的保存行数，方便从页面确认同步是否生效 |
| [#16](https://github.com/sevenshal/52etf/pull/16) / [#17](https://github.com/sevenshal/52etf/pull/17) | 扫描器方法论重写：ROIC-vs-WACC 质量闸门 + 两阶段 FCFF DCF 内在价值（原来的中位数打法废弃）；发现并修复 DCF 终值爆炸的模型缺陷；接入中债国债收益率曲线作为无风险利率 |
| [#19](https://github.com/sevenshal/52etf/pull/19) | 新增"内在价值同比闸门"：最新年报算出的内在价值必须不低于去年同期算出的内在价值 |

以及一个**发现但特意没有在本次改动范围内修的 bug**（已作为独立后台任务提出，未合并）：
`a_stock_innovation_momentum_virtual.py` 里研发动量因子用的 `operate_income` 字段在 tushare
利润表接口里根本不存在（已核对官方文档），推测该因子已经默默失效了很久；本次会话顺手把
`a_stock_income` 表补全的 `revenue` 字段本可以直接替换掉这个错误字段——**这部分已经在
`a_stock_innovation_momentum_virtual.py` 里修掉并随 PR 合并**，不是遗留项。

## 3. 数据层

### 3.1 新增/扩展的 DuckDB 表（`backend/src/core/analytics_database.py`）

- `a_stock_balancesheet`（新增）：`total_assets`/`total_liab`/`total_cur_assets`/`total_cur_liab`/
  `total_hldr_eqy_exc_min_int`/`money_cap`/`accounts_receiv`/`inventories`/`goodwill`/`fix_assets`/
  `lt_borr`/`st_borr`/`notes_payable`/`acct_payable`/`comp_type`（1一般工商业 2银行 3保险 4证券，
  用于区分金融/非金融口径）
- `a_stock_cashflow`（新增）：`net_profit`/`n_cashflow_act`/`c_pay_acq_const_fiolta`/`free_cashflow`/
  `n_cashflow_inv_act`/`n_cash_flows_fnc_act`/`c_fr_sale_sg`/`end_bal_cash`/`n_incr_cash_cash_equ`/
  `c_pay_dist_dpcp_int_exp`
- `a_stock_fina_indicator`（新增）：`eps`/`dt_eps`/`bps`/`ocfps`/`roe`/`roe_waa`/`roe_dt`/`roa`/`roic`/
  `grossprofit_margin`/`netprofit_margin`/`debt_to_assets`/`current_ratio`/`quick_ratio`/`profit_dedt`/
  `extra_item`/`netprofit_yoy`/`dt_netprofit_yoy`/`or_yoy`/`op_yoy`/`ocf_to_or`/`ocf_to_debt`/
  `interestdebt`/`fcff`/`fcfe`/`netdebt`/`ebit`/`ebitda`
- `a_stock_income`（扩展已有表）：补上 `revenue`/`n_income_attr_p`/`operate_profit`/`total_profit`/
  `total_cogs`/`basic_eps`/`income_tax`/`fin_exp_int_exp`（原来只有 `operate_income`/`rd_exp`）

**为什么直接拿 `fcff`/`fcfe`/`netdebt`/`ebit`/`ebitda`**：tushare 的 `fina_indicator` 接口本身就
按标准口径算好了这些指标，没有自己从现金流量表拿 OCF 减资本开支去近似——这样比自己拼凑更准，
也少写很多容易出错的代码。

### 3.2 同步引擎（`backend/src/robot/a_stock_base_data_sync.py`）

- 三张新表复用一套通用增量同步引擎 `_sync_statement_data`，通过 `fetch_range_fn`/`upsert_fn`
  参数化具体报表接口，而不是把已有 `sync_a_stock_income_data` 那套"逐symbol规划增量/回补范围、
  多线程抓取、批量落库"逻辑复制三份。
- **`force_full_refresh` 参数**（新增，加在 `sync_a_stock_income_data` 和 `_sync_statement_data`
  上）：正常的增量同步只在"该股票完全没有记录"或"最新公告已过期(超过45天)"时才会重新抓取，
  不会因为 schema 新增了列就重新拉取已经覆盖的历史年份。`force_full_refresh=True` 会假装每个
  symbol 都没有历史数据（把 `symbol_bounds` 传空字典），逼着重新抓取完整回溯窗口。这是在生产
  环境已经跑过一次旧字段列表的同步之后才发现需要的能力——**详见第 7 节遗留任务**。
- 配套脚本 `backend/scripts/backfill_financial_statement_new_fields.py`：对 4 张财务报表统一调用
  `force_full_refresh=True` 做一次性回填，支持 `--symbols` 参数先在少量股票上验证。

### 3.3 无风险利率的真实数据源（`backend/src/robot/a_stock_base_data_config.py`）

用户在 chinabond.com.cn 找到了"中债国债收益率曲线(到期)"的 `curve_id`
(`2c9081e50a2f9606010a3068cae70001`)，我用现有的 `searchYc` 端点（`backend/src/core/services/
chinabond.py`，抓信用债曲线用的同一套代码）实测确认了曲线名称和数值（10年期当时约1.68%），
把它加进了 `CHINABOND_CREDIT_CURVES` 配置列表，复用已有的通用同步逻辑，不需要新写爬虫代码。

### 3.4 已修复的两个数据 bug

1. **`operate_income` 字段不存在**：tushare 利润表接口没有这个字段（已用官方文档
   `https://tushare.pro/wctapi/documents/33.md` 核实），但 `a_stock_innovation_momentum_virtual.py`
   一直把它当"营业收入"用来算研发费用占比和三年营收复合增速。已改用真实的 `revenue` 字段。
2. **新增字段没有真正落库**：在第一次给 `a_stock_income` 加 `revenue` 等字段时，
   `_normalize_income_frame`/`_bulk_upsert_income_frame` 的列白名单没有同步更新，导致这些字段
   从 tushare 拉回来之后又在写库前被静默丢弃。已修复（现在从一个 `_INCOME_NUMERIC_COLUMNS`
   元组统一驱动，避免同类遗漏）。

## 4. 扫描引擎方法论（`backend/src/core/services/value_investing_scanner.py`）

只读查询，不写任何数据表。核心函数 `screen_value_investing_candidates()`。

### 4.1 数据窗口：只用年报

所有质量闸门和估值历史都只取 `end_date` 为 `12-31` 的年报，通过 `_annual_rows()` 实现，理由：
季报是累计但未审计口径，跨报告类型混用会污染"近5年平均"这类判断。`_annual_rows()` 内部还做了
**同一财年去重**：如果因为更正/追溯调整同一个 `end_date` 存在多条记录（`report_type` 不同），
只保留公告时间(`ann_date`)最新的一条，避免同一财年被当成两期，把"近5年"实际算成"近3-4年"。

### 4.2 质量闸门（`_quality_assessment`）

- **非金融公司**：核心判据是近5年平均 ROIC 是否跑赢 WACC（`min_roic_wacc_spread_pct`，默认
  `0.0`），而不是用 ROE——ROE 加杠杆就能做高，ROIC 是资本结构中性的，价差为负意味着"增长越快
  越在毁灭价值"。辅助检验：经营现金流/净利润比（`min_ocf_to_np`，默认 `0.6`）、FCFF为正的
  年数（`min_fcf_positive_years`，默认 `3`）、资产负债率上限（`max_debt_to_assets`，默认 `70`）。
- **金融公司（银行/保险/证券，`comp_type` in {2,3,4}）**：退回 ROE 闸门
  （`min_avg_roe_financial`，默认 `8.0`），因为"投入资本"概念对金融机构的资产负债表不适用。
- **内在价值同比闸门**（PR #19 新增，见 4.5）：最新年报算出的内在价值不能低于去年同期算出的，
  默认阈值 `min_value_growth_pct=0.0`。数据不够算出"去年那次快照"时按不通过处理。
- 年报数据不足 `MIN_ANNUAL_PERIODS_FOR_QUALITY=3` 期直接判定不通过。

### 4.3 WACC 估算（`_wacc_components`）

- **Beta**：用 `REGR_SLOPE` 一次性对全市场做批量回归（`_beta_by_symbol`），每只股票近2年
  (`BETA_LOOKBACK_DAYS=730`) 日收益率对中证全指(`000985.SH`)日收益率回归斜率，要求至少
  `MIN_BETA_OBSERVATIONS=200` 个重合交易日。
- **Blume 调整**（PR #17 新增，见 5.1）：`adjusted_beta = 2/3 * raw_beta + 1/3 * 1.0`，且
  `raw_beta` 先 clip 到 `[0.2, 3.0]`。
- **股权成本** = CAPM = 无风险利率 + adjusted_beta × 股权风险溢价。
- **无风险利率**：默认（`risk_free_rate=None`）现取中债国债收益率曲线10年期最新值；曲线未同步
  到时退回静态假设 `DEFAULT_RISK_FREE_RATE=0.025`；调用方可显式传值覆盖自动取值。返回结果的
  `assumptions.risk_free_rate_source` 会标注实际用的是 `chinabond_10y` / `default_fallback` /
  `explicit_override` 哪一种。
- **股权风险溢价**：仍是静态假设 `DEFAULT_EQUITY_RISK_PREMIUM=0.06`，没有找到 A 股公开权威的
  实时 ERP 数据源。
- **债权成本**：`利息费用(fin_exp_int_exp) / 有息负债(interestdebt)`，clip 到 `[0.5%, 15%]`，
  缺数据时退回 `DEFAULT_COST_OF_DEBT_PRETAX=0.045`；乘以 `(1 - 实际税率)` 得税后成本，实际税率
  = `income_tax / total_profit`，clip 到 `[5%, 33%]`，缺数据退回 `DEFAULT_EFFECTIVE_TAX_RATE=0.25`。
- 按市值(股权)与有息负债(债权)的账面权重加权得到 WACC。

### 4.4 内在价值（`_intrinsic_value_snapshot`）

- **非金融**：两阶段 FCFF DCF（`_two_stage_fcff_value`）。基准 FCFF = 近3年(或更少)平均值；
  近端增速 = FCFF 复合增速，缺失则退回净利润复合增速，都缺则为 0，clip 到
  `NEAR_TERM_GROWTH_BOUNDS=(-0.15, 0.30)`；显式预测期 `DCF_EXPLICIT_YEARS=5` 年，增速从近端
  线性衰减到永续增长率 `DEFAULT_TERMINAL_GROWTH_RATE=0.03`；终值用永续增长模型按 WACC 折现；
  企业价值减净债务（`netdebt` 字段，缺失时退回 `interestdebt - money_cap`）得股权价值；
  `expected_return_pct = 股权价值 / 当前市值 - 1`。
- **金融**：FCFF DCF 不适用（存贷款不是资本开支/营运资金变动），改用分析师覆盖银行/险资的标准
  替代框架"合理市净率"：`fair P/B = (ROE - g) / (股权成本 - g)`，
  `expected_return_pct = fair P/B / 当前PB - 1`。
- **`MIN_WACC_TERMINAL_SPREAD=0.03`**（PR #17 从 `0.01` 改上来，见 5.2）：WACC 与永续增长率的
  利差低于这个阈值时直接判定 DCF/合理PB 不可用，返回 `None` 而不是一个失真的数字。
- 同时给出悲观/乐观区间（`expected_return_pct_bear`/`_bull`）：贴现率 ±150bp
  (`SCENARIO_DISCOUNT_RATE_SPREAD`)，非金融的近端增速额外乘以 `(0.5, 1.3)`
  (`SCENARIO_GROWTH_MULTIPLIER`)。**注意：悲观/乐观区间没有联动调整永续增长率**，只调了
  贴现率和近端增速——见 5.3。
- 旧版的"估值均值回归"、"市盈率倒数(E/P)"、"trailing FCFF收益率"仍作为交叉验证字段
  （`reversion_return_pct`/`earnings_yield_pct`/`fcf_yield_pct`）返回，但不再参与
  `expected_return_pct` 的计算。

### 4.5 内在价值同比增长闸门（PR #19）

`_intrinsic_value_snapshot()` 分别用两段切片各调用一次：

- **当期**：用完整的年报历史（截止最新一期）
- **去年同期**：去掉最新一期年报（`fina_history.iloc[:-1]`、`income_history.iloc[:-1]`）

两次调用**用同一个 WACC / 股权成本**（不随切片重新计算），只让基本面输入（FCFF、净利润、ROE）
随切片变化——这是刻意的简化，目的是让这次对比只反映"公司自身基本面是在变好还是变差"，不掺入
"市场整体贴现率变了"这个因素。差值 `value_growth_pct = 当期价值/去年价值 - 1` 送进质量闸门。

## 5. 需要重点 review 的设计决策

按我自己认为风险从高到低排列：

### 5.1 Blume 调整权重 `2/3` 是否适用于 A 股单股票回归

Blume 调整是成熟市场（主要是美股）长期研究得出的经验公式，本意是修正"个股 beta 有向1回归的
长期趋势"这个统计现象。这里直接套用在 A 股、且只用 2 年日线数据做的单次回归上，**没有做任何
本地化验证**——权重是否应该更保守（比如更接近1，收缩力度更大）完全没有经过 A 股数据的实证检验，
纯粹是"业界通用做法"的直接搬用。

### 5.2 `MIN_WACC_TERMINAL_SPREAD=0.03` 是怎么定出来的

老实说：**这个值是看到"老凤祥"那一个案例算出 2548% 的离谱回报率之后，反推出来的一个工程补丁**，
不是从大样本统计里得出的阈值。原来是 `0.01`，观察到 `WACC-g=2.1pp` 时终值变成年FCFF的48倍这个
不合理的结果后，改成了 `0.03`（对应终值上限约34倍FCFF）。这个值背后的逻辑是"健康DCF常见的
WACC-g利差一般在4-7pp"，但这句话本身也只是我个人对教科书/实务经验的转述，**没有用这个仓库里
实际的、真实的 A 股数据去验证 3pp 这个阈值卡出来的样本分布是否合理**——有可能太松（依然放过一些
不合理的高估值）也有可能太紧（误伤一些真实的高成长低负债公司，这类公司WACC天然低、DCF利差
天然窄）。**强烈建议 reviewer 在生产数据跑出全市场结果后，把 `wacc_pct - terminal_growth_pct`
的分布拉出来看一眼，重新校准这个阈值。**

### 5.3 悲观/乐观区间没有联动调整永续增长率

我在最早设计时口头提过"三档都应该联动调整永续增长率"，但实际实现里悲观/乐观区间只调了贴现率
和近端增速，永续增长率三档保持不变（见 `screen_value_investing_candidates` 里
`bear_wacc`/`bull_wacc`/`bear_growth`/`bull_growth` 附近代码，没有 `bear_terminal_growth`/
`bull_terminal_growth`）。这是为了控制改动范围没有做完的简化，不是刻意的方法论决定——如果
reviewer 认为这个偏差没有材料影响可以不管，但这确实和我在会话里对用户的描述有出入，需要
如实指出。

### 5.4 金融公司"合理市净率"模型在 A 股银行股上可能系统性高估

用合成数据测试银行分支时，一个 ROE 13.8%、beta 0.88 的合成银行股算出了 290%+ 的潜在回报率。
这不是代码 bug（公式执行正确），而是模型本身的已知局限：中国银行股长期 PB < 1，很大程度是
市场在给报表体现不出来的资产质量/隐性风险定价，纯 CAPM + Gordon 增长框架完全捕捉不到这类
"市场认为的隐性折价"。**这意味着扫描结果里金融股的 return% 大概率系统性偏高，需要人工加一层
判断，不能直接采信。**

### 5.5 静态假设：股权风险溢价、部分兜底值

`DEFAULT_EQUITY_RISK_PREMIUM=0.06`、`DEFAULT_COST_OF_DEBT_PRETAX=0.045`、
`DEFAULT_EFFECTIVE_TAX_RATE=0.25`、`DEFAULT_TERMINAL_GROWTH_RATE=0.03` 都是没有实时数据源支撑
的静态假设，来自我对 A 股/中国宏观环境的估计，不是从这个仓库的数据里算出来的，也没有做敏感性
测试之外的验证。

### 5.6 `_intrinsic_value_snapshot` 用同一个 WACC 算"今年"和"去年"

见 4.5——这是有意识的简化（详见该节说明），但如果 reviewer 认为"去年那次估值也应该用去年的
（更早的）beta/市场环境算出的 WACC"，这是一个可以讨论、需要额外工程量的改进方向，目前没做。

## 6. 已验证 vs 未验证

### 已验证

- DCF/WACC/合理PB 三个核心公式手算校验过，代码输出与手算完全吻合（见 PR #16/#17 描述里的
  单元测试片段）。
- Beta 估算：用已知真实 beta（1.0、0.9等）生成带噪声的合成日收益率，回归估计值（1.03、0.88等）
  与真值接近，`REGR_SLOPE` 用法正确。
- 全流程用合成数据（自己造的 DuckDB 行）跑通，覆盖：高质量低估非金融股正确入选、
  ROIC<WACC"增长毁灭价值"股正确被拦、银行股正确走合理PB分支、5年平均达标但最新一年恶化的
  公司正确被"内在价值同比闸门"拦下、只有3年历史(最低要求)的边界情况不崩溃、`force_full_refresh`
  用真实哈希 id upsert 路径验证过不产生重复行。
- 后端全量测试套件（`pytest tests/`）在每次改动后都跑过，全部通过，无回归。
- 前端 `eslint` + 生产构建（`npm run build`）每次改动后都跑过，无报错。
- 无风险利率曲线抓取：直接对 chinabond.com.cn 真实接口发过请求，确认了 `curve_id` 对应
  "中债国债收益率曲线(到期)"、10年期数值约1.68%（当时的真实值）。

### 未验证 —— 这是最大的风险点

- **整个链路从未用真实 tushare 数据跑过一次完整的全市场扫描**。所有验证都是合成数据。真实
  A 股数据里 `fina_indicator.fcff`/`netdebt` 等字段的实际覆盖率、异常值分布、行业间差异
  完全未知。
- 生产环境的财务报表同步任务在本次会话期间**只确认执行过一次**（且是在新增
  `income_tax`/`fin_exp_int_exp`/`fcff`/`fcfe`/`netdebt`/`ebit`/`ebitda` 等字段**之前**跑的），
  这意味着截至本文档撰写时，生产 DuckDB 里这些新字段大概率仍是空的，`backfill_financial_
  statement_new_fields.py` 需要在生产环境执行一次（见第 7 节）。
- 唯一一次真实数据验证是用户手动跑了一次扫描，反馈"老凤祥"(600612.SH) 算出 2548% 的离谱回报率
  ——这暴露了 5.2 节的问题并已修复，但**修复后没有再用真实数据复核这一只、或任何其他股票**。
- ROIC-WACC 质量闸门、内在价值同比闸门在真实全市场数据上的"通过率"是多少（会不会把95%的股票
  都筛掉，或者反过来筛得太松）完全未知。

## 7. 遗留任务（需要人工在生产环境执行）

1. **跑一次性回填脚本**：`cd backend && python scripts/backfill_financial_statement_new_fields.py`
   （可先用 `--symbols` 跑少量股票验证），把新增字段补进已有的历史年份。这台机器的部署方式是
   CI 把代码打包进 `quant_server.pyz`，`scripts/` 目录不在打包产物里，需要手动把脚本文件放到
   `/home/quantd/quant_prod/backend/scripts/` 下（详见与用户对话中给出的具体命令，本文档不
   重复贴那部分运维细节）。
2. 回填完成后，跑一次真实的 `screen_value_investing_candidates()` 全市场扫描，重点看：
   - 通过质量闸门的股票数量是否合理（不能是0，也不能是"全市场都过"）
   - `wacc_pct - terminal_growth_pct` 的实际分布，重新校核 5.2 节的 `MIN_WACC_TERMINAL_SPREAD`
   - 金融股 return% 是否如 5.4 节预期的系统性偏高
   - `fcff`/`netdebt` 等字段在真实数据里的缺失率
3. 股权风险溢价（ERP）目前没有实时数据源，如果能找到可靠来源（第三方研究机构定期发布的 A 股
   ERP估计、或用沪深300隐含股权风险溢价算法）可以替换掉 5.5 节的静态假设。
4. 5.3 节提到的悲观/乐观区间未联动永续增长率，如果决定要做，改动点在
   `screen_value_investing_candidates()` 里计算 `bear_ev`/`bull_ev` 的那几行。

## 8. 关键文件清单

| 文件 | 作用 |
|---|---|
| `backend/src/core/services/value_investing_scanner.py` | 扫描引擎核心逻辑，本文档大部分内容对应这个文件 |
| `backend/src/app/api/value_investing.py` | `GET /api/value-investing/screen`，管理员专用 |
| `backend/src/core/services/tushare.py` | 新增的 balancesheet/cashflow/fina_indicator 拉取方法，income 拉取方法扩展字段 |
| `backend/src/core/analytics_database.py` | 新增/扩展的表结构 |
| `backend/src/robot/a_stock_base_data_sync.py` | 通用增量同步引擎 `_sync_statement_data`、`force_full_refresh` |
| `backend/src/robot/a_stock_base_data_config.py` | chinabond 曲线配置，含新增的国债曲线 |
| `backend/src/robot/a_stock_innovation_momentum_virtual.py` | 修复了 `operate_income` → `revenue` 的 bug |
| `backend/scripts/backfill_financial_statement_new_fields.py` | 一次性回填脚本 |
| `backend/scripts/verify_value_investing_tushare_access.py` | 验证 tushare 账号权限的诊断脚本 |
| `backend/scripts/inspect_a_stock_income_revenue_gap.py` | 验证 `operate_income` 历史是否为空的诊断脚本 |
| `frontend/src/pages/ValueInvestingScreen.jsx` / `.css` | 「研究」→「价值投资」页面 |
