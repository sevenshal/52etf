# 国君191因子库记录

本文档整理当前已加入因子计算库的国泰君安 Alpha191 因子。因子实现集中在 `src/core/services/factor_engine.py`，前端显示名称使用“编号 + 含义”的格式。

## 筛选口径

- 样本池：中证500动态成分、A股创新100动态成分
- 时间范围：A创100 Rank IC 更新为 2021-01-01 之后；中证500 Rank IC 沿用此前 2024-01-01 之后筛选结果
- 上市过滤：上市满 365 天
- 预测目标：T+20 forward return
- Rank IC：原始因子值与未来收益的截面 Spearman IC 均值
- 方向说明：`高值更优`/`低值更优` 是因子库内用于方向调整的配置；低值更优因子在计算分析时会乘以 -1 后进入标准化/排序流程

## 已加入因子

| 因子编号 | 库内显示名称 | 计算公式 | 含义 | 库内方向 | 中证500 Rank IC | A创100 Rank IC |
| --- | --- | --- | --- | --- | ---: | ---: |
| Alpha005 | Alpha005：高点量能共振背离 | `-TSMAX(CORR(TSRANK(VOLUME,5), TSRANK(HIGH,5), 5), 3)` | 高点与成交量短期时序排名相关性的反向最大值，刻画高点量能同步性/背离。 | 高值更优 | 0.0286 | 0.0220 |
| Alpha021 | Alpha021：6日均价趋势斜率 | `REGBETA(MA(CLOSE,6), SEQUENCE(6))` | 6 日均价对时间序列的回归斜率，刻画短期均价趋势。 | 低值更优 | -0.0298 | -0.0165 |
| Alpha024 | Alpha024：5日平滑反转动量 | `SMA(CLOSE-DELAY(CLOSE,5), 5, 1)` | 5 日收盘价差的中式 SMA 平滑值，刻画短期价格变化。 | 低值更优 | -0.0363 | -0.0266 |
| Alpha027 | Alpha027：短周期动量摆动 | `DECAYLINEAR(RET3*100 + RET6*100, 12)` | 3 日与 6 日收益率之和的线性衰减均值，刻画短周期动量摆动。 | 低值更优 | -0.0337 | -0.0220 |
| Alpha042 | Alpha042：高点量价背离 | `-RANK(STD(HIGH,10)) * CORR(HIGH,VOLUME,10)` | 高点波动横截面排名与高点-成交量相关性的乘积，刻画高点波动和量能相关性背离。 | 高值更优 | 0.0505 | 0.0392 |
| Alpha046 | Alpha046：多均线乖离反转 | `(MA(CLOSE,3)+MA(CLOSE,6)+MA(CLOSE,12)+MA(CLOSE,24))/(4*CLOSE)` | 多条均线相对收盘价的均值，价格低于均线时取值更高。 | 高值更优 | 0.0288 | 0.0157 |
| Alpha052 | Alpha052：上下推动强弱比 | `SUM(MAX(0,HIGH-DELAY(TP,1)),26)/SUM(MAX(0,DELAY(TP,1)-LOW),26)*100` | TP 为典型价，刻画上行推动相对下行推动的强弱。 | 低值更优 | — | -0.0324 |
| Alpha059 | Alpha059：价格摆动累积 | `SUM(IF(CLOSE=DELAY(CLOSE,1),0,IF(CLOSE>DELAY(CLOSE,1),CLOSE-MIN(LOW,DELAY(CLOSE,1)),CLOSE-MAX(LOW,DELAY(CLOSE,1)))),20)` | 按涨跌状态累计收盘价相对低点/前收的摆动。 | 低值更优 | — | -0.0525 |
| Alpha088 | Alpha088：20日涨幅反转 | `(CLOSE-DELAY(CLOSE,20))/DELAY(CLOSE,20)*100` | 20 日收益率，筛选结果显示近期涨幅越低越占优。 | 低值更优 | -0.0397 | -0.0303 |
| Alpha093 | Alpha093：开盘下探强度 | `SUM(IF(OPEN>=DELAY(OPEN,1),0,MAX(OPEN-LOW,OPEN-DELAY(OPEN,1))),20)` | 开盘低于前开后，累计当日下探/跳空幅度。 | 低值更优 | -0.0139 | -0.0352 |
| Alpha095 | Alpha095：成交额波动率 | `STD(AMOUNT,20)` | 20 日成交额标准差，刻画成交额波动。 | 低值更优 | -0.0302 | -0.0251 |
| Alpha106 | Alpha106：20日价差反转 | `CLOSE-DELAY(CLOSE,20)` | 收盘价相对 20 日前的绝对价差，刻画中短期价差动量/反转。 | 低值更优 | -0.0447 | -0.0332 |
| Alpha118 | Alpha118：上影下影强弱比 | `SUM(HIGH-OPEN,20)/SUM(OPEN-LOW,20)*100` | 20 日上影强度相对下影强度的比例，刻画上行影线压力。 | 低值更优 | -0.0362 | -0.0213 |
| Alpha122 | Alpha122：三重平滑对数趋势 | `(SMA3(LOG(CLOSE))-DELAY(SMA3(LOG(CLOSE)),1))/DELAY(SMA3(LOG(CLOSE)),1)` | SMA3 表示三次 `SMA(...,13,2)`，刻画平滑后的对数价格趋势变化。 | 低值更优 | — | -0.0336 |
| Alpha129 | Alpha129：12日下跌幅累积 | `SUM(IF(CLOSE-DELAY(CLOSE,1)<0,ABS(CLOSE-DELAY(CLOSE,1)),0),12)` | 近 12 日下跌价差累计，刻画回撤幅度暴露。 | 低值更优 | -0.0177 | -0.0361 |
| Alpha132 | Alpha132：20日成交额均值 | `MA(AMOUNT,20)` | 20 日平均成交额，偏流动性/规模暴露；长样本中偏低值更优。 | 低值更优 | -0.0123 | -0.0211 |
| Alpha134 | Alpha134：12日价量反转 | `(CLOSE-DELAY(CLOSE,12))/DELAY(CLOSE,12)*VOLUME` | 12 日涨跌幅乘成交量，刻画价量复合强度。 | 低值更优 | -0.0297 | -0.0102 |
| Alpha135 | Alpha135：平滑20日动量 | `SMA(DELAY(CLOSE/DELAY(CLOSE,20),1), 20, 1)` | 延迟 1 日的 20 日价格比值再做 SMA 平滑，刻画平滑动量。 | 低值更优 | -0.0398 | -0.0356 |
| Alpha139 | Alpha139：开盘量价背离 | `-CORR(OPEN,VOLUME,10)` | 开盘价与成交量 10 日相关性的相反数，刻画开盘量价背离。 | 高值更优 | 0.0280 | 0.0143 |
| Alpha145 | Alpha145：成交量均线背离 | `(MA(VOLUME,9)-MA(VOLUME,26))/MA(VOLUME,12)*100` | 成交量 9 日均线与 26 日均线之差，相对 12 日均线归一化。 | 低值更优 | -0.0304 | -0.0116 |
| Alpha147 | Alpha147：12日均价趋势斜率 | `REGBETA(MA(CLOSE,12), SEQUENCE(12))` | 12 日均价对时间序列的回归斜率，刻画中短期趋势。 | 低值更优 | — | -0.0304 |
| Alpha151 | Alpha151：平滑20日价差反转 | `SMA(CLOSE-DELAY(CLOSE,20),20,1)` | 20 日绝对价差的平滑值，刻画平滑后的中短期价差动量/反转。 | 低值更优 | — | -0.0341 |
| Alpha158 | Alpha158：日内振幅率 | `(HIGH-LOW)/CLOSE` | 日内高低价差相对收盘价，刻画日内振幅。 | 低值更优 | -0.0451 | -0.0286 |
| Alpha160 | Alpha160：下跌波动平滑 | `SMA(IF(CLOSE<=DELAY(CLOSE,1),STD(CLOSE,20),0),20,1)` | 下跌日 20 日收盘波动率的平滑值，刻画下跌波动暴露。 | 低值更优 | -0.0176 | -0.0351 |
| Alpha161 | Alpha161：12日真实波幅 | `MA(MAX(MAX(HIGH-LOW,ABS(DELAY(CLOSE,1)-HIGH)),ABS(DELAY(CLOSE,1)-LOW)),12)` | 12 日平均真实波幅，刻画跳空后的实际波动。 | 低值更优 | -0.0272 | -0.0413 |
| Alpha167 | Alpha167：12日上涨幅累积 | `SUM(IF(CLOSE>DELAY(CLOSE,1),CLOSE-DELAY(CLOSE,1),0),12)` | 近 12 日上涨价差累计，刻画上涨幅度暴露。 | 低值更优 | — | -0.0442 |
| Alpha169 | Alpha169：平滑差分动量 | `SMA(MA(DELAY(SMA(CLOSE-DELAY(CLOSE,1),9,1),1),12)-MA(...,26),10,1)` | 收盘价一阶差分经过多层 SMA/均线处理后的动量差。 | 低值更优 | -0.0347 | -0.0381 |
| Alpha174 | Alpha174：上涨波动平滑 | `SMA(IF(CLOSE>DELAY(CLOSE,1),STD(CLOSE,20),0),20,1)` | 上涨日 20 日收盘波动率的平滑值，刻画上涨波动暴露。 | 低值更优 | — | -0.0381 |
| Alpha175 | Alpha175：6日真实波幅 | `MA(MAX(MAX(HIGH-LOW,ABS(DELAY(CLOSE,1)-HIGH)),ABS(DELAY(CLOSE,1)-LOW)),6)` | 6 日平均真实波幅，刻画短期实际波动。 | 低值更优 | — | -0.0426 |
| Alpha187 | Alpha187：开盘向上突破强度 | `SUM(IF(OPEN<=DELAY(OPEN,1),0,MAX(HIGH-OPEN,OPEN-DELAY(OPEN,1))),20)` | 20 日开盘向上突破强度累计，刻画开盘跳升和盘中上行延续。 | 低值更优 | -0.0295 | -0.0440 |
| Alpha189 | Alpha189：6日均价偏离度 | `MA(ABS(CLOSE-MA(CLOSE,6)),6)` | 收盘价相对 6 日均价的平均偏离，刻画短期乖离/震荡幅度。 | 低值更优 | -0.0266 | -0.0372 |

## 备注

- A创100 全量筛选脚本为 `lab/screen_gtja191_inno100.py`；最近一次结果留存在 `/private/tmp/gtja_alpha191_inno100_screen.csv`，不可计算/无有效IC的编号留存在 `/private/tmp/gtja_alpha191_inno100_errors.csv`。
- 中证500 Rank IC 来自 `/private/tmp/gtja_alpha_screen.csv`；未在该文件覆盖的新增因子用 `—` 标记。
- 本次补充优先选择公式较简单、且不依赖 `VWAP=AMOUNT/VOLUME` 与复权价混算的因子；`Alpha012/013/120/126/164/173` 等长样本也强，但涉及 VWAP/价格尺度口径，暂不作为默认新增。
- `Alpha132` 已从短样本高值更优改为长样本低值更优；`Alpha006` 因 A创100长样本 IC 接近 0、`Alpha144` 因方向冲突，已从默认因子库剔除。
