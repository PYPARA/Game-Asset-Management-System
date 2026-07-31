# GAMS v1 故障手册

故障处理遵循 fail-closed：先保留证据和冻结快照，再决定重试、重做或人工恢复。不要删除 `history`、`releases`、`approved/objects` 或 `gams-lock.json` 来“清理”故障。

## 快速分流

| 现象 | 先检查 | 处理 | 禁止的自动动作 |
|---|---|---|---|
| `credentials_locked` | 供应商设置中的锁定状态、Job provider snapshot | 解锁同一供应商后 resume | 换供应商 |
| `rate_limit` | Attempt 的 `retry_after`、计划/供应商并发 | 等待并在剩余重试内恢复 | 无限重试 |
| `server` / `network` | 供应商事件、request id、网络连通性 | 在预算内重试，持续失败则人工处理 | 改写冻结模型 |
| `auth` | Base URL、供应商 API Key | 修正同一供应商凭据后重试 | 把请求发给另一供应商 |
| `quota` / `billing` | 供应商额度和预算事件 | 补充额度或创建新计划 | 自动降级模型 |
| `content_policy` | Prompt、供应商返回和策略事件 | 修改 Prompt 或人工决定 | JSON 模式回退重发 |
| `provider.model_unavailable` | Job 冻结模型、供应商模型目录 | 创建重做任务/新计划并重新确认预算 | 自动换模型 |
| `QA failed` | Finding、QA JSON、联系表/差异图 | 选择已批准的 remediation action | 直接批准 |
| Delivery `failed` | Delivery 收据、staging、旧 lock | 先 verify；需要时重新导出旧 Release | 删除未知文件 |
| Delivery `recovery_required` | 收据中的恢复错误和 staging 路径 | 停止新的 Delivery，按收据逐项人工恢复 | 覆盖 checkout |

## 服务或 Worker 崩溃

1. 保留 Project 和本机 SQLite，不要删除 `workspace/runs` 中的证据。
2. 重新启动 API；启动时会清理过期 lease，并将有有效落盘输出或确定性 Worker 状态的 Job 标成可恢复。
3. 使用 `gams run inspect <plan-id> --json` 检查 Attempt、阶段、事件和成本，再使用 `gams run resume <plan-id> --json`。
4. `awaiting_user` 且没有可证明输出的 Job 只能由用户选择新的 remediation action。

恢复以幂等键和输出哈希为准；看到同一 idempotency key 时不要手动再发供应商请求。

## 磁盘空间不足

- 生成阶段：先释放本机缓存或扩容，再从 `output_received`/Worker 可恢复状态继续。
- Release：应没有半个 Manifest 或半个 Catalog 指针；检查 `releases/<id>/manifest.json` 和 `release.json` 的哈希。
- Delivery：apply 会恢复旧受管文件和 lock；如果收据为 `recovery_required`，保留 staging，按收据恢复后再 verify。

不要把 `workspace` 删除当作磁盘恢复的第一步；批准媒体、Artifact、Release 和 Delivery 必须先能从 Project 复验。

## SQLite 删除或损坏

SQLite 是可重建索引。停止 API 后可删除本机 `index.sqlite3`，重新启动会扫描所有已发现 Project 的 Catalog、History、Release 和 Delivery。若扫描报告有错误：

1. 记录完整错误列表和 Project 路径。
2. 修复缺失/损坏的 Project 文件或 Blob。
3. 再执行 `POST /api/projects/<id>/scan`（或重启服务）。

永远不要用空 SQLite 覆盖 Project 历史，也不要从 SQLite 反向生成 Project 文件。

## Delivery 被中断

1. 不运行下一次 apply；先读取 `history/deliveries/<date>/*.json`。
2. 检查 checkout 的 `gams-lock.json` 是否仍指向旧 Release，并运行 `gams export verify`。
3. `failed` 表示已恢复，可在修复根因后重试；`recovery_required` 表示恢复本身失败，必须按收据中的备份/staging 路径人工处理。
4. 只有 verify 通过后才允许新的 Release 或 Delivery。

## 供应商故障矩阵验收

发布前运行离线协议矩阵：

```bash
npm run acceptance:m6 -- --output /tmp/gams-m6.json
```

它会测试成功、限流、5xx、鉴权、额度、内容策略和坏响应，并验证内容策略不会触发第二次请求。要把真实供应商纳入验收：

```bash
GAME_ASSETS_M6_API_KEY='…' \
npm run acceptance:m6 -- \
  --provider-url https://provider.example/v1 \
  --require-live-provider \
  --output /tmp/gams-m6-live.json
```

报告不会写入 API Key。真实供应商的模型、请求 ID、状态和停止原因应随报告一并人工复核。
