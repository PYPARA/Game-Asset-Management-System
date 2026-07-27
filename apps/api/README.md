# API 后端

FastAPI 后端只监听本机回环地址。Project 根目录文件是权威数据；Project 默认从 iCloud Drive 的 `Game-Projects` 发现，SQLite 则保存在本机 `~/Library/Application Support/Game-Asset-Management-System`，避免同步数据库文件。

## Project 契约

- 根目录必须包含 `format_version: 1` 的 `project.yaml`。
- Catalog 使用分组 JSON 集合和统一 `snake_case` 字段。
- History 保存内容寻址修订、审核、rendition 和 QA 索引。
- Workspace 保存被忽略的候选、驳回、QA 报告与缓存。
- 系统不支持其他项目布局或外部媒体路径协议。

## 常用命令

```bash
uv run --project apps/api game-assets-api
uv run --project apps/api pytest
```

OpenAPI 在服务启动后位于 `http://127.0.0.1:8787/openapi.json`。
