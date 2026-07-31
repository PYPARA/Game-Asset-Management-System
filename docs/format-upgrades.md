# Project、Manifest 与 SQLite 格式升级策略

格式升级遵循“读取旧格式、显式迁移、保留历史、可回滚”的原则。升级器不猜测目录、不原地改写不可变对象，也不把 SQLite 当事实源。

## 当前版本矩阵

| 格式 | 当前版本 | 兼容策略 | 写入策略 |
|---|---:|---|---|
| `project.yaml` | 1 | 只接受根目录 `format_version: 1`；未知版本 fail closed | 新 Project 仍写 v1 |
| Catalog/History/QA/Review | 1 | 由 `ProjectStore.scan_full()` 校验并重建 | 新记录沿用 v1 字段 |
| Release Manifest | 1、2 | v1 可检查；v2 要求 `snapshot_hash`，两者均不可变 | 新 Release 默认 v2 |
| Delivery receipt | 1 | 从 `history/deliveries` 读取并重建索引 | 收据保留完整状态和恢复信息 |
| SQLite 本机索引 | Alembic `0005_m4_agent_supervision` + 原地兼容列升级 | 可删除后从 Project 重建；不提交、不同步 | 只写当前模型，不承载事实 |

## Project 升级

Project v1 是唯一当前契约。未来如果引入 v2：

1. 先发布只读 scanner 和迁移预检，列出每个将要改变的文件、哈希和错误。
2. 在 `workspace/backups/<timestamp>` 保存迁移前清单；不改变 `history/objects` 的内容寻址路径。
3. 写入新的 `project.yaml` 临时文件并 fsync，验证完整 Project scan 为零错误后原子替换。
4. 迁移失败时恢复旧 `project.yaml`，保留报告和备份；禁止部分升级继续生产。
5. 在至少一个旧 Project 的副本上完成 scan、Release preflight、Delivery verify 和 SQLite rebuild 后，才允许用户确认迁移。

当前版本遇到 `format_version != 1` 会明确停止，而不是把未知字段当作 v1 继续写入。

## Manifest 升级

Manifest v1 保持读取兼容；Manifest v2 在资产列表之外绑定 Project、完整资产快照和 `sha256:` snapshot hash。升级 v1 不重写旧 Release：创建新的 v2 Release，旧 Release 仍可检查和回滚。v2 的 hash 计算输入是排序稳定的 JSON：

```json
{"format_version":2,"project_id":"…","assets":[…]}
```

Delivery 只接受通过预检的 v2（旧 v1 可在 API/索引中查看），`gams-lock.json` 记录实际 Release、snapshot hash 和每个受管文件的哈希。任何 hash 不符都阻止覆盖。

## SQLite/Alembic 升级

- 正式发布前先在数据库副本运行 `alembic upgrade head`，再用应用的 `Database.create_schema()` 验证 SQLite 原地兼容路径。
- 每个迁移只新增可重建表/列/索引；不得从 SQLite 反向生成或删除 Project 文件。
- 迁移失败保留原数据库副本并停止启动；如果 SQLite 无法打开，删除本机索引后由启动扫描重建。
- 升级后必须核对 assets、revisions、artifacts、reviews、releases、deliveries、jobs 和 agent audit 的数量及关键哈希。

验收命令：

```bash
uv run --project apps/api alembic -c apps/api/alembic.ini upgrade head
npm run acceptance:m6 -- --output /tmp/gams-m6-format.json
```

## 版本边界

格式版本和应用版本分开管理。增加字段可以在当前版本向后兼容时直接发布；改变身份、哈希语义、路径语义或状态含义必须增加格式版本和迁移记录。供应商模型下线、API 错误或凭据变化不是格式升级，必须按 [故障手册](troubleshooting.md) 人工处理。
