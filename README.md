# 52etf

美股 / A 股量化研究与交易的前后端一体化仓库（monorepo）。

| 目录 | 说明 |
| --- | --- |
| `backend/` | 后端服务（Python / FastAPI），部署产物为 `src.tgz`（源码）+ `deps.tgz`（依赖，仅依赖变化时上传） |
| `frontend/` | 前端（React），构建产物为静态文件 `build/` |

## 部署

推送到 `master` 后，GitHub Actions（`.github/workflows/deploy.yml`）会自动构建并部署到本机（经云服 frps 转发）：

- **自动变更检测**：只改了 `backend/**` 就只发后端，只改了 `frontend/**` 就只发前端，都改了则一起发。
- **手动全量部署**：在 Actions 页面点 "Run workflow"（`workflow_dispatch`）会强制前后端都部署一次。

部署所需的仓库 secret：`ECS_SSH_KEY`（对应 `quantd` 用户的 deploy key，公钥在 `~quantd/.ssh/authorized_keys`）。

## 本地开发

见项目根目录 `AGENTS.md`（含 `dev.52etf.vip` 本地访问方式、SQLite 事务约束等约定）。

### 后端

```bash
cd backend
../../.venv/bin/uvicorn src.app.main:app --host 127.0.0.1 --port 8001
```

### 前端

```bash
cd frontend
PORT=3000 \
HOST=0.0.0.0 \
BROWSER=none \
REACT_APP_API_URL=https://dev.52etf.vip \
DANGEROUSLY_DISABLE_HOST_CHECK=true \
WDS_SOCKET_HOST=dev.52etf.vip \
WDS_SOCKET_PORT=443 \
WDS_SOCKET_PROTOCOL=wss \
npm start
```
