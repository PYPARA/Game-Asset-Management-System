# API 后端

FastAPI 后端只监听本机回环地址。Project 根目录文件是权威数据；Project 默认从 iCloud Drive 的 `Game-Projects` 发现，SQLite 则保存在本机 `~/Library/Application Support/Game-Asset-Management-System`，避免同步数据库文件。

## Project 契约

- 根目录必须包含 `format_version: 1` 的 `project.yaml`。
- Catalog 使用分组 JSON 集合和统一 `snake_case` 字段。
- History 保存内容寻址修订、Artifact 元数据、审核和 QA 记录。
- 媒体候选批准时，原始来源提升到 `production/sources`，运行媒体提升到 `approved/objects`；正式修订不引用 workspace。
- Release v1 保持兼容；M3 的 `gams release create` 和 `format_version: 2` 请求写入带 snapshot hash 的 Manifest v2，并在写文件前执行全量预检。
- SQLite 中的 Artifact、审核、Release 和 Delivery 都能由 Project 文件重新扫描建立索引；Delivery 收据位于 `history/deliveries`。
- Workspace 保存被忽略的候选、驳回、QA 报告与缓存。
- M2 生成计划冻结 DAG、预算与并发；Runner 使用 lease/heartbeat、持久 Attempt 和事件流恢复执行，并为人工返工生成 Finding 与视觉证据。
- M2.1 支持多个 OpenAI 兼容供应商、全局文字/图片默认路由和任务级供应商/模型覆盖；确认后的 Job 保存冻结供应商快照且不自动回退。
- M3 支持 `gams release preflight|create`、`gams export preview|apply|verify|rollback`，以及对应 `/api/exports/*`、`/api/deliveries` API。Apply 使用 checkout 内 staging、argv 验证命令、受管文件哈希和最后写入的 `gams-lock.json`；失败会恢复上一版本，重复交付产生 `no_op` 收据。
- M4 提供隔离的 Codex stdio 适配器（通过 `GAME_ASSETS_CODEX_COMMAND` 显式配置）、只读上下文包、AgentSession/AgentEvent 审计、结构化诊断和 `/api/changesets` 审批记录；适配器不可用、输出异常、输入过期、越权或预算触顶均降级人工处理，绝不执行 Git 或修改 SQLite/审核/Release/Delivery 事实。
- M5 提供 `POST /api/projects/{id}/legacy-media-promotions` 的显式旧媒体升级、Release 的 `asset_keys` 子集和 CLI `--asset` 选项；`scripts/m5_pilot.py` 可在不调用供应商、不执行 Git 的前提下重放 Emperor 试点并记录失败 Delivery 验证。
- 系统不支持其他项目布局或外部媒体路径协议。

## 供应商与模型路由

- `GET/POST/PATCH /api/providers` 管理供应商配置；`POST /api/providers/{id}/archive|restore` 软停用或恢复配置。
- `GET/PUT /api/provider-defaults` 管理新任务使用的全局文字、图片默认路由。
- `GET /api/providers/{id}/models` 读取缓存；`POST .../models/refresh` 拉取供应商模型列表；`PATCH .../models` 保存人工模态分类。
- 模型刷新失败不会覆盖旧缓存。归档供应商保留历史 Job 和快照，但不能分配给新计划。
- `GenerationTask` 的 `provider_profile_id` 与 `model` 是新计划的实际路由；计划顶层 `provider_profile_id` 仅作为旧客户端兼容回退。
- API Key 不进入请求响应、SQLite、Project、事件或 Job 快照；后端仅在当前进程内按供应商 ID 持有已解锁明文。

## Codex 监督

- `POST /api/jobs/{job_id}/agent/diagnose` 生成一次只读上下文并请求结构化 Agent 提案；`GET /api/jobs/{job_id}/agent/context` 可检查脱敏上下文哈希。
- `GET /api/agent/sessions`、`GET /api/agent/sessions/{id}/events` 提供线程、turn、预算、Finding、动作和人工降级理由。
- `GET /api/changesets`、`GET /api/changesets/{id}/patch`、`POST .../approve|reject` 只记录审批，不应用补丁或执行 Git。

## 常用命令

```bash
uv run --project apps/api game-assets-api
uv run --project apps/api pytest
uv run --project apps/api gams run inspect <plan-id> --json
uv run --project apps/api gams run resume <plan-id> --json
uv run --project apps/api gams agent diagnose <job-id> --json
```

OpenAPI 在服务启动后位于 `http://127.0.0.1:8787/openapi.json`。

Codex 适配器默认关闭。只有在本机提供经过固定和审核的 stdio JSON 命令时才设置
`GAME_ASSETS_CODEX_COMMAND`；命令只收到脱敏上下文，所有提案仍由 Controller 校验。
