# AGENTS — 52etf 代码约定与高频避坑

本文只补充上级 `quant/AGENTS.md`；部署架构、提交约定、开发环境和访问方式以上级文件为准，不在这里重复。

## 数据库结构升级

数据库新增字段必须通过代码内的幂等升级逻辑完成，不能要求用户在生产环境手工执行 SQL：

- SQLite 主库：把新增字段登记到 `ensure_table_columns()` 一类的集中升级入口，先用
  `PRAGMA table_info(table_name)` 检查，仅对缺失字段执行 `ALTER TABLE ... ADD COLUMN`。
- DuckDB 分析库：把新增字段登记到 `ensure_analytics_table_columns()`，由
  `ensure_analytics_schema()` 在启动时调用。若表被视图依赖，只在确有缺失字段时临时删除依赖视图，
  加列后必须在同一次 schema 初始化流程中重建视图。
- ORM/SQLAlchemy 模型、写入列清单、查询和视图必须与新增字段同步更新。
- 升级逻辑必须可重复执行，并补一条“旧表结构 → 自动补列 → 依赖视图可用”的测试。

改表语句属于过渡代码，不应永久累积：

- 每次只保留尚未在生产环境完成的新增改表语句。
- 发布并确认生产库已成功升级后，后续改动应删除已经生效的旧 `ALTER TABLE`、临时删视图等迁移分支；
  模型字段和最终建表/建视图定义继续保留。
- 删除旧迁移前必须确认所有生产实例均已运行过对应版本，不能仅凭本地库结构判断。
- 删除、改类型、重建大表等破坏性迁移不能套用自动加列规则，必须单独评估和获得用户确认。

## SQLAlchemy Session 生命周期（高频坑，务必先读）

项目统一用 `get_db_ctx()` / `get_external_trading_db_ctx()` 提供短事务，退出时 `commit()` 并 `close()`。
`SessionLocal = sessionmaker(bind=engine)` 没有关闭 `expire_on_commit`（默认 `True`），
所以 **commit 之后，该 session 内所有 ORM 对象的全部属性（包括主键 `id`）都会过期**。

铁律：

1. **不要在 session 作用域之外访问 ORM 对象属性。**
   哪怕只是 `run.id`，也会触发对已关闭 session 的 lazy refresh，报错：
   `Instance <...> is not bound to a Session; attribute refresh operation cannot proceed`
   （即 `DetachedInstanceError`）。主键 `id` 也一样会过期，不要以为它安全。

2. **不要把 ORM 对象传出短事务或跨 session 复用。**
   需要会话外处理时，在短事务里复制成普通 `dict` / `SimpleNamespace` 快照，再关 session。

3. **会话外要用的值，在 `with` 块内取成局部变量或普通快照。**

   ```python
   with get_db_ctx() as db:
       run = db.get(AIStockRecommendationRun, run_id)
       if run:
           run.status = "SUCCESS"
       run_id_out = run.id          # 块内捕获，会话外只用 run_id_out
   # 会话外绝不再访问 run.id / run.status / run.*
   ```

推荐模式：短事务读快照 → 会话外做 IO/计算 → 新短事务写回。不要在 session 作用域里执行
`await`、网络请求、券商调用、邮件、长计算或批量同步。

### 典型案例

`AIStockRecommendationService.run_recommendation()` 第 4 步“AI 持仓评估”在写入批次
（session 已关闭）之后，用已脱离的 `run.id` 调用 `evaluate_paper_holdings(...)`，
触发 `DetachedInstanceError`，又被兜底 `except` 吞成一条 warning，导致持仓评估永远不落库，
`/api/ai-stock/paper/hold-evaluations` 一直返回空数组。修复 = 改用函数开头捕获的局部变量 `run_id`。

这类异常可能被兜底 `except` 吞成 warning，表现为功能静默失效。

### 排查方法

- 日志关键词：`is not bound to a Session`、`attribute refresh operation cannot proceed`、`DetachedInstanceError`。
- 注意被兜底吞掉的 warning，例如 `AI hold evaluation step skipped: ... not bound to a Session`。
- 复查 session 作用域外的属性访问，主键（`run.id`、`portfolio.id`）也不能例外。

## 测试

从 `52etf/backend` 运行：

```bash
../.venv/bin/python -m pytest -q
```

单文件：`../.venv/bin/python -m pytest tests/test_ai_stock.py -q`。数据库升级至少覆盖存量结构迁移测试。
