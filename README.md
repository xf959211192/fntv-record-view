# 飞牛影视观看记录与 Trakt 同步

用于读取飞牛影视 `trimmedia.db`，在页面中浏览观看记录，并将电影/剧集观看历史同步到 Trakt。

![页面示例](docs/sample-1.png)

## 功能概览

- 播放历史浏览
  - 用户筛选、标题搜索、时间范围筛选、分页浏览
  - 展示剧名、季集号、播放进度、分辨率、播放时间等信息
- Trakt 设备授权
  - 服务端保存 `Client ID`、`Client Secret`、`access_token`、`refresh_token`
  - 前端只负责发起授权和展示验证码
- Trakt 历史同步
  - 支持预览同步和正式同步
  - 电影与剧集分开匹配
  - 剧集主路径为 `show -> season -> episode -> trakt episode id`
  - 支持失败队列、人工指定、重新匹配、同步状态面板
- 自动同步
  - 默认每 30 分钟执行一次
  - 支持选择自动同步用户，留空表示所有用户
  - 支持“已看完或达到阈值”筛选

## 目录说明

- `main.py`：Flask 后端与 Trakt 同步逻辑
- `templates/index.html`：前端页面
- `tests/test_trakt_sync.py`：Trakt 相关单元测试
- `database/trimmedia.db`：飞牛影视数据库挂载位置
- `runtime/`：容器推荐的运行时目录，用于保存 Trakt token、缓存库和临时数据库副本

## 本地运行

### 1. 安装依赖

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
```

### 2. 准备数据库

将飞牛影视数据库放到：

```text
database/trimmedia.db
```

### 3. 配置 `.env`

```env
TRAKT_CLIENT_ID=你的 Trakt Client ID
TRAKT_CLIENT_SECRET=你的 Trakt Client Secret
TRAKT_REDIRECT_URI=urn:ietf:wg:oauth:2.0:oob

TRAKT_AUTO_SYNC_ENABLED=true
TRAKT_AUTO_SYNC_INTERVAL_SECONDS=1800
TRAKT_AUTO_SYNC_WATCHED_THRESHOLD=90
TRAKT_AUTO_SYNC_LIMIT=1000
```

### 4. 启动服务

```bash
.venv/Scripts/python main.py
```

访问：`http://127.0.0.1:5000`

## Docker 部署

### 目录准备

```text
fntv-record-view/
├─ database/
│  └─ trimmedia.db
├─ runtime/
└─ .env
```

### 启动

```bash
docker compose up -d --build
```

### 停止

```bash
docker compose down
```

### 容器中的关键路径

- 只读数据库：`/app/database/trimmedia.db`
- 运行时目录：`/app/runtime`
  - `trakt_tokens.json`
  - `trakt_last_sync.json`
  - `trakt_settings.json`
  - `trakt_sync.db`
  - `trimmedia_tmp.db`

### Docker 环境变量

`docker-compose.yml` 已预留这些变量：

- `TRAKT_CLIENT_ID`
- `TRAKT_CLIENT_SECRET`
- `TRAKT_REDIRECT_URI`
- `TRAKT_AUTO_SYNC_ENABLED`
- `TRAKT_AUTO_SYNC_INTERVAL_SECONDS`
- `TRAKT_AUTO_SYNC_WATCHED_THRESHOLD`
- `TRAKT_AUTO_SYNC_LIMIT`
- `APP_RUNTIME_DIR`

## GitHub Actions 镜像打包

仓库已提供工作流：

- `.github/workflows/build.yml`

默认行为：

- `push` 到 `main` 时构建并推送镜像
- 打 `v*` 标签时构建并推送版本镜像
- `pull_request` 仅校验构建，不推送
- 镜像发布到 `GHCR`：`ghcr.io/<owner>/<repo>`

如果仓库是私有仓库，需要确保包权限允许读取。

## 运行时文件说明

以下文件属于运行产物，不建议提交：

- `app.log`
- `trimmedia_tmp.db*`
- `trakt_tokens.json`
- `trakt_last_sync.json`
- `trakt_settings.json`
- `trakt_sync.db`
- `runtime/`

## 测试

```bash
.venv/Scripts/python -m py_compile main.py
.venv/Scripts/python -m unittest -v tests/test_trakt_sync.py
```

## 许可证

本项目基于 [MIT](LICENSE) 许可证发布。
