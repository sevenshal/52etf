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

## 数据库只读模式（`QUANT_DB_READ_ONLY`）

三个库（SQLite 主库 `evc_stocks.db`、SQLite 外部交易库 `external_trading.db`、DuckDB 分析库
`analytics.duckdb`）的建表/补列/建索引/建视图都发生在 **import 阶段**——「只要 import 就会写库」。
本地开发想直接读线上库或线上库副本、或者写只读分析脚本时，这既不安全（可能写坏数据），
也经常因为目录不可写或拿不到写锁直接炸在 import 上。

开关就是 `QUANT_DB_READ_ONLY`，**默认关闭**：

```bash
QUANT_DB_READ_ONLY=true python your_readonly_script.py
```

开启后：

- 上表所有初始化写操作全部跳过，每个步骤只记一条 warning，方便确认「确实没写」；
- 连 `os.makedirs(<库目录>)` 都不执行，import 完全不碰文件系统；
- DuckDB 一律用 `read_only=True` 打开，不会再去抢写锁；
- `connect_duckdb_for_write()` 直接抛 `ReadOnlyModeError`，避免「以为写进去了其实没写」的静默失败；
- 读路径完全不受影响：查询、`get_analytics_db_ctx()`、只读脚本照常用。

### 代码约定

**新增任何 import 阶段的初始化写操作，必须用开关包住**，否则这个约定就会重新失守：

```python
from .read_only_mode import skip_init_writes

def ensure_table_columns():
    if skip_init_writes("主库补列 ensure_table_columns"):
        return
    ...

if not skip_init_writes("主库建表 Base.metadata.create_all"):
    Base.metadata.create_all(engine)
```

`skip_init_writes(step)` 在非只读模式下恒返回 `False`（零副作用）；`step` 是日志里显示的
中文步骤名，同一个名字只打一次日志。

当前已覆盖：

| 模块 | 被开关包住的写操作 |
| --- | --- |
| `core/database.py` | 目录 makedirs、`Base.metadata.create_all`、`migrate_system_service_credentials`、`drop_deprecated_tables`、`drop_deprecated_columns`、`ensure_performance_indexes`、`ensure_table_columns`、`ensure_soxl_fear_strategy_multi_config_schema`、`ensure_a_stock_fear_strategy_schema` |
| `core/external_trading_database.py` | 目录 makedirs、`create_all`、`ensure_external_trading_columns`、`drop_deprecated_external_trading_columns`、`ensure_external_trading_indexes` |
| `core/analytics_database.py` | 目录 makedirs、`ensure_analytics_schema`（含 `create_all`、补列、建索引、重建视图、`DROP TABLE us_stock_basic`） |
| `core/services/stock_system/storage.py` | `ensure_tables` 的 `create_all` |

### 注意事项

- 只读模式**不是**「只读副本」：同步/迁移任务照样会去调 `connect_duckdb_for_write()`，
  会被直接拒绝并报错，这是预期行为。
- SQLite 侧只跳过 DDL，不强制把连接设成 `mode=ro`（WAL 库用只读连接容易踩坑），
  想彻底只读请把路径指向副本文件而不是线上文件。
- 加新的库/新的初始化入口时，记得同时补一条「子进程 import 后库结构没变」的测试，
  参考 `backend/tests/test_read_only_mode.py`。

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
