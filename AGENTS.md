# AGENTS — 52etf monorepo 开发约定与避坑（供 AI Agent 阅读）

本文补充上级 `quant/AGENTS.md`（部署架构、目录约定、dev.52etf.vip 访问方式）。这里只写代码层最容易重复踩的坑。

## 提交约定（务必遵守）

1. **不要自行直接提交推送**：改动完成后先向用户说明改了什么、验证结果，**问是否提交**，得到确认后才 `git commit` / `git push`。
2. **commit message 必须写清楚描述**：说明改动内容和原因（例如 `feat(executor): 参考价优先走 tushare，未开盘/停牌标的自动跳过`），不要用无描述/不相关的提交信息。

## SQLAlchemy Session 生命周期（高频坑，务必先读）

项目统一用 `get_db_ctx()` / `get_external_trading_db_ctx()` 提供短事务，退出时 `commit()` 并 `close()`。
`SessionLocal = sessionmaker(bind=engine)` 没有关闭 `expire_on_commit`（默认 `True`），
所以 **commit 之后，该 session 内所有 ORM 对象的全部属性（包括主键 `id`）都会过期**。

由此而来的三条铁律：

1. **不要在 `with get_db_ctx() as db:` 作用域之外访问 ORM 对象属性。**
   哪怕只是 `run.id`，也会触发对已关闭 session 的 lazy refresh，报错：
   `Instance <...> is not bound to a Session; attribute refresh operation cannot proceed`
   （即 `DetachedInstanceError`）。主键 `id` 也一样会过期，不要以为它安全。

2. **不要把 ORM 对象传出短事务、也不要跨 session 复用。**
   需要会话外处理时，在短事务里复制成普通 `dict` / `SimpleNamespace` 快照，再关 session。

3. **会话外要用的值，在 `with` 块内就取成局部变量。** 正确姿势：

   ```python
   with get_db_ctx() as db:
       run = db.get(AIStockRecommendationRun, run_id)
       if run:
           run.status = "SUCCESS"
       run_id_out = run.id          # 块内捕获，会话外只用 run_id_out
   # 会话外绝不再访问 run.id / run.status / run.*
   ```

   推荐模式：短事务读快照 → 会话外做 IO/计算 → 新短事务写回。不要在 session 作用域里
   执行 `await`、网络请求、券商调用、邮件、长计算或批量同步。

### 真实案例（2026-08-13，就是这类坑）

`AIStockRecommendationService.run_recommendation()` 第 4 步“AI 持仓评估”在写入批次
（session 已关闭）之后，用已脱离的 `run.id` 调用 `evaluate_paper_holdings(...)`，
触发 `DetachedInstanceError`，又被兜底 `except` 吞成一条 warning，导致持仓评估永远不落库，
`/api/ai-stock/paper/hold-evaluations` 一直返回空数组。修复 = 改用函数开头捕获的局部变量 `run_id`。

教训：这类“兜底 try/except 把 DetachedInstanceError 吞掉”的 bug 不会报错，只会静默丢失功能。

### 排查方法

- 日志关键词：`is not bound to a Session`、`attribute refresh operation cannot proceed`、`DetachedInstanceError`。
- 注意被兜底吞掉的 warning，例如 `AI hold evaluation step skipped: ... not bound to a Session`。
- 复查时全局扫一遍 session 作用域外的主键访问（`run.id`、`portfolio.id` 等），对照上面第 3 条。

## 本地测试

- venv 位置：本机 `52etf/.venv`（`.gitignore` 已忽略 `.venv/`；Mac 开发机按上级 `quant/AGENTS.md` 用 `quant/.venv`）。
- 创建（首次）：

  ```bash
  cd 52etf
  ~/.local/bin/uv venv .venv --python 3.12
  ~/.local/bin/uv pip install -p .venv -r backend/requirements.txt pytest
  ```

- 跑后端测试：

  ```bash
  cd 52etf/backend
  ../.venv/bin/python -m pytest -q
  ```

  单文件：`../.venv/bin/python -m pytest tests/test_ai_stock.py -q`
