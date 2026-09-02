# 缠论回测研究入口

这里保留可复跑的脚本、方法说明和最终结论。行情/权重数据库只读打开，补充的历史指数成分以 CSV 注入，避免改写生产数据库。

## 推荐复跑链路

```bash
python lab/sync_chan_index_weights.py --output-csv lab/output/chan_signal_pair_backtest_20260829_120d/csi_missing_weights.csv
python lab/candidate_pool_audit.py --database /path/to/analytics.duckdb --supplemental-weights lab/output/chan_signal_pair_backtest_20260829_120d/csi_missing_weights.csv
python lab/chan_native_backtest.py --database /path/to/analytics.duckdb --start-date 2026-02-01 --end-date 2026-08-28 --supplemental-weights lab/output/chan_signal_pair_backtest_20260829_120d/csi_missing_weights.csv
python lab/chan_native_pair_backtest.py --database /path/to/analytics.duckdb --supplemental-weights lab/output/chan_signal_pair_backtest_20260829_120d/csi_missing_weights.csv
```

结构定义和未来函数约束见 `chan_native_methodology.md`；`chan_native_audit.py`、`chan_recursive_audit.py` 用于检查结构不变量和递归确认时间。`chan_signal_pair_backtest.py` 是旧 CZSC 对照基座；CZSC 对照适配层已迁到 `czsc_oracle.py`（`analyze_bars_czsc_legacy`），backend 生产代码不再依赖 CZSC 信号引擎。

> 结构引擎已升级到 v2（固定区间中枢 + 真实 GG/DD + 级别标签、线段第一/第二种划分、MACD 面积盘整/趋势背驰）。下方“已验证结论”里的数字先于 v2，需按本节命令重跑后更新。

## 已验证结论

- 扩展历史池（沪深300/中证500/中证1000/中证2000，去 ST、去流动性不足）共 4434 只；2026-02-01～2026-08-28 的严格自算 1m T+1 收盘结果：一买 43 笔，平均净收益 +0.34%、胜率 41.9%；二买 33 笔，+0.33%、57.6%；三买 391 笔，-1.22%、30.4%。
- 日线方向过滤后只剩 22/14/177 笔（一/二/三买）；30m 与日线普通过滤为 0/0/8 笔；严格递归交集为 0 笔。0 笔代表当前窗口结构样本不足，不代表过滤后策略有效。
- 买卖点配对中多数买点在样本结束前没有等到对应卖点，必须区分 `signal_closed` 和 `end_of_data`，不能把期末计价收益当作卖点胜率。
- 盘中最高价触达目标不能当作真实胜率；应按逐根 K 线先触发目标还是止损统计。

## 旧 CZSC 对照研究

`chan_t1_close_stats.py`、`chan_t1_first_hit_backtest.py`、`chan_t1_intraday_hit_stats.py`、`chan_1m_buy_strength_buckets.py`、`chan_1m_signal_thinning.py` 等脚本记录原 CZSC 信号的 T+1、首触、排序和降频实验，用于对照，不与严格自算结果混合解读。

## 结果文件

运行产物默认写入 `lab/output/chan_signal_pair_backtest_20260829_120d/`。最终叙述版见 `chan_backtest_final_report.md`；CSV 未纳入版本库，按 README 命令即可重建。
