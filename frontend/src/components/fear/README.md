# FearDashboard 重构说明

## 重构概述

原始的 `FearDashboard.jsx` 文件过于复杂，包含了1153行代码，混合了多种功能。现在已经成功重构为模块化的结构。

## 新的文件结构

```
fear/
├── README.md                           # 重构说明文档
├── utils.js                           # 工具函数
├── hooks/                             # 自定义 hooks
│   ├── useFearGreedData.js           # 恐贪指数数据
│   ├── useFedRateData.js             # 联邦利率数据
│   ├── useBondData.js                # 美债数据
│   └── useAutoTrading.js             # 自动交易控制
└── components/                        # 组件
    ├── FearGreedCurrent.jsx          # 当前恐贪指数显示
    ├── FearGreedHistorical.jsx       # 历史走势图表
    ├── FearGreedPrediction.jsx       # 走势预测图表
    ├── FearGreedAiae.jsx             # AIAE图表
    ├── BondFearGreed.jsx             # 美债贪恐指数
    └── AutoTradingPanel.jsx          # 自动交易控制面板
```

## 重构后的主文件

`FearDashboard.jsx` 现在只有约70行代码，主要负责：
- 状态管理（activeTab）
- 使用自定义 hooks 获取数据
- 渲染各个子组件

## 功能模块说明

### 1. 工具函数 (utils.js)
- `TIME_RANGES`: 时间范围选项
- `getFearGreedColor`: 获取恐贪指数颜色
- `getFearGreedStatus`: 获取恐贪指数状态
- `formatQuarter`: 格式化季度
- `fitExponentialCurve`: 计算拟合曲线
- `getCellColor`: 获取表格单元格颜色

### 2. 自定义 Hooks

#### useFearGreedData
- 获取恐贪指数数据
- 返回 `fearGreedData` 和 `loading` 状态

#### useFedRateData
- 获取联邦利率相关数据
- 返回当前利率、预测区间、表格数据等

#### useBondData
- 获取美债数据和计算贪恐值
- 依赖联邦利率数据进行计算

#### useAutoTrading
- 管理自动交易状态
- 提供切换自动交易的方法

### 3. 组件

#### FearGreedCurrent
- 显示当前恐贪指数
- 包含罗盘和历史对比数据

#### FearGreedHistorical
- 历史走势图表
- 支持时间范围筛选
- 包含CNN、守逮、股价、VIX、趋势线

#### FearGreedPrediction
- 走势预测图表
- 对比历史平均和今年实际数据

#### FearGreedAiae
- AIAE数据图表
- 显示家庭和非营利组织股权占比

#### BondFearGreed
- 美债贪恐指数显示
- 联邦概率预测表格

#### AutoTradingPanel
- 自动交易控制面板
- 包含各种操作按钮和设置

## 重构优势

1. **可维护性**: 每个功能模块独立，便于维护和调试
2. **可复用性**: 组件和 hooks 可以在其他地方复用
3. **可测试性**: 每个模块可以独立测试
4. **可读性**: 代码结构清晰，职责分明
5. **性能优化**: 按需加载数据，避免不必要的请求

## 使用方式

重构后的组件使用方式保持不变，外部调用接口完全兼容：

```jsx
import FearDashboard from './components/FearDashboard';

// 使用方式不变
<FearDashboard />
```

## 注意事项

1. 所有导入路径已更新，确保正确引用
2. 数据获取逻辑已封装到 hooks 中
3. 组件间通过 props 传递数据
4. 保持了原有的所有功能特性
