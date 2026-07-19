# Game Assets API

本地优先的游戏资产制作后端。项目目录中的 `.game-assets/` 是权威数据源，SQLite
仅保存可重建索引、持久任务和本地操作记录。

## 启动

```bash
uv sync --all-packages
uv run --project apps/api game-assets-api
```

服务只监听 `127.0.0.1:8787`，机器可读的 OpenAPI 文档位于
`http://127.0.0.1:8787/openapi.json`（为满足无第三方 CDN 的 CSP，不启用 Swagger CDN）。
可通过环境变量覆盖数据目录和端口：

```bash
GAME_ASSETS_DATA_DIR=/path/to/local-data GAME_ASSETS_PORT=8787 uv run game-assets-api
```

## 测试

```bash
uv run --project apps/api pytest
```

API Key 只通过 `/api/providers/{id}/unlock` 进入进程内存；它不会进入 SQLite、
项目文件或 API 响应。后端重启后必须重新解锁。
