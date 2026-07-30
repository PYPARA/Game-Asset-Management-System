# API 后端

FastAPI 后端只监听本机回环地址。Project 根目录文件是权威数据；Project 默认从 iCloud Drive 的 `Game-Projects` 发现，SQLite 则保存在本机 `~/Library/Application Support/Game-Asset-Management-System`，避免同步数据库文件。

## Project 契约

- 根目录必须包含 `format_version: 1` 的 `project.yaml`。
- Catalog 使用分组 JSON 集合和统一 `snake_case` 字段。
- History 保存内容寻址修订、Artifact 元数据、审核和 QA 记录。
- 媒体候选批准时，原始来源提升到 `production/sources`，运行媒体提升到 `approved/objects`；正式修订不引用 workspace。
- Release v1 在写 Manifest 前执行全量预检，任一批准失效、QA fail、路径碰撞或 Blob 损坏都会阻止整次 Release。
- SQLite 中的 Artifact、审核和 Release 都能由 Project 文件重新扫描建立索引。
- Workspace 保存被忽略的候选、驳回、QA 报告与缓存。
- M2 生成计划冻结 DAG、预算与并发；Runner 使用 lease/heartbeat、持久 Attempt 和事件流恢复执行，并为人工返工生成 Finding 与视觉证据。
- M2.1 支持多个 OpenAI 兼容供应商、全局文字/图片默认路由和任务级供应商/模型覆盖；确认后的 Job 保存冻结供应商快照且不自动回退。
- 系统不支持其他项目布局或外部媒体路径协议。

## 供应商与模型路由

- `GET/POST/PATCH /api/providers` 管理供应商配置；`POST /api/providers/{id}/archive|restore` 软停用或恢复配置。
- `GET/PUT /api/provider-defaults` 管理新任务使用的全局文字、图片默认路由。
- `GET /api/providers/{id}/models` 读取缓存；`POST .../models/refresh` 拉取供应商模型列表；`PATCH .../models` 保存人工模态分类。
- 模型刷新失败不会覆盖旧缓存。归档供应商保留历史 Job 和快照，但不能分配给新计划。
- `GenerationTask` 的 `provider_profile_id` 与 `model` 是新计划的实际路由；计划顶层 `provider_profile_id` 仅作为旧客户端兼容回退。
- API Key 不进入请求响应、SQLite、Project、事件或 Job 快照；后端仅在当前进程内按供应商 ID 持有已解锁明文。

## 常用命令

```bash
uv run --project apps/api game-assets-api
uv run --project apps/api pytest
uv run --project apps/api gams run inspect <plan-id> --json
uv run --project apps/api gams run resume <plan-id> --json
```

OpenAPI 在服务启动后位于 `http://127.0.0.1:8787/openapi.json`。
