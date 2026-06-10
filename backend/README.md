# 52etf_api


uvicorn src.app.main:app --host 0.0.0.0 --port 8001 --reload

## 接入文档

- [外部交易账号长连接接入文档](docs/external-trading-websocket-integration.md)

## SQLite 事务约束

线上主库使用 SQLite。写交易、机器人、定时任务、外部交易同步相关代码时，必须避免长事务：

- 不要在 `get_db_ctx()` / `get_external_trading_db_ctx()` 的 `with` 作用域里执行 `await`、网络请求、券商/外部交易执行器调用、邮件发送、长时间计算或批量数据同步。
- ORM 对象不要跨 session 长时间使用；需要事务外处理时，先在短事务里复制成普通 dict 或 `SimpleNamespace` 快照，再关闭 session。
- 推荐模式是：短事务读取配置快照 -> 事务外调用行情/券商/执行器/计算 -> 短事务写状态和日志。
- 如果必须同时操作主库和外部交易库，不要把两个 DB session 和外部 IO 包在同一个作用域里；先完成一个库的短写，再用另一个短事务回写状态。
- 高频机器人路径里，清理、日志、状态更新都要保持短小，必要时加 retry/降频，而不是靠拉长 SQLite timeout。

## 开发说明：死代码扫描

后端项目已经提供一套死代码检查入口：

- 安装开发依赖（首次运行）：

```bash
cd /Users/sevenshal/Dev/github/quant/52etf_api
make deadcode-tools
```

- 运行死代码扫描（会自动安装缺失工具）：

```bash
cd /Users/sevenshal/Dev/github/quant/52etf_api
make deadcode
```

默认执行 `balanced`（偏低噪音）模式，聚焦最关键问题。

- 全量校验（更严格）：

```bash
cd /Users/sevenshal/Dev/github/quant/52etf_api
make deadcode-strict
```

- 清理历史扫描结果：

```bash
cd /Users/sevenshal/Dev/github/quant/52etf_api
make deadcode-clean
```

脚本会依次执行：

1. `vulture src`：查找未引用函数/类/变量
2. `ruff check src`：检测未使用/未定义相关问题
3. `deptry src`：检测导入依赖是否存在未使用/缺失

### 扩展配置

扫描规则支持在 [scripts/deadcode.config](/Users/sevenshal/Dev/github/quant/52etf_api/scripts/deadcode.config:1) 调整，例如：

- 改变 `VULTURE_MIN_CONFIDENCE`
- 增加/减少 `VULTURE_EXCLUDE` 目录
- 调整 `RUFF_SELECT`
- 给命令追加额外参数（`*_EXTRA_ARGS`）

可通过环境变量切换：

- `DEADCODE_PROFILE=balanced`（默认，低噪音）
- `DEADCODE_PROFILE=strict`（完整规则）

运行结果会输出到：

```bash
/Users/sevenshal/Dev/github/quant/52etf_api/.artifacts/deadcode
```
