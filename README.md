# 游戏资产管理系统

本地优先的游戏内容与媒体生产工作台。`Game-Projects` 父仓库统一记录所有正式 Project；游戏仓库只接收确定性导出的内容模块和已批准运行资源。

## 架构

- `apps/web`：React/Vite 制作台。
- `apps/api`：仅监听本机回环地址的 FastAPI 服务。
- Project 根目录：唯一权威数据源，必须包含 `format_version: 1` 的 `project.yaml`。
- SQLite：位于本机 `~/Library/Application Support/Game-Asset-Management-System`，不进入 iCloud 或 Git，只保存可重建索引和任务状态。

Project 使用以下固定分区：

```text
project.yaml
catalog/       按领域分组的内容、实体、媒体和关系
schemas/       项目数据 Schema
production/    风格、Prompt 和 PNG 制作母版
approved/      普通 Git 管理的批准 rendition
history/       内容寻址修订与审核决定
releases/      不可变发布清单
workspace/     被 Git 忽略的候选、驳回、QA、缓存与日志
```

系统只支持这一种项目契约，不探测隐藏目录、外部媒体协议或字段别名。

## 当前状态

截至 2026-07-30，M0–M2 已完成。系统已具备 Project 发现/扫描、资产与不可变修订、关系、供应商、持久生成计划、硬 QA、人工审核和 Project 内 Release v1；Web 制作台已接入真实 Project 数据，并提供生成计划编辑器与运行检查器。

M2 Controller 会冻结 DAG、Prompt、尺寸、调用预算和并发约束；Runner 实施 provider/plan 双重并发、lease、heartbeat、幂等 Attempt、上游产物注入和崩溃恢复。供应商输出依次经过 `output_received`、硬 QA、证据生成和 `candidate_ready`；QA fail 进入结构化 Finding 与 `awaiting_user`，用户可以在没有 Codex 的情况下选择 Worker 修复、重试、重新生成或图像编辑。未知供应商交付不会自动重试。

尚未完成的是 M3 的 Release Manifest v2、snapshot hash、外部 checkout Delivery、`gams-lock.json` 与 preview/apply/verify/rollback，以及 M4 的 Codex 监督 Agent、自动语义诊断和 ChangeSet。Provider 返回图片不等于批准、Release 或 Delivery。

实施顺序、P0 风险和每个里程碑的完成标准见 [路线图](docs/roadmap.md)。

## 安装与启动

要求 Node.js 20+、Python 3.13+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
npm --prefix apps/web install
uv sync --all-packages
npm run dev
```

默认 Project 容器位于 `~/Library/Mobile Documents/com~apple~CloudDocs/Game-Projects`。可通过 `GAME_ASSETS_PROJECTS_ROOT` 和 `GAME_ASSETS_STATE_DIR` 环境变量覆盖项目与本地状态目录。

- Web：`http://127.0.0.1:4173`
- API：`http://127.0.0.1:8787`
- OpenAPI：`http://127.0.0.1:8787/openapi.json`

## 测试

```bash
npm run build
npm test
```

## 数据与安全边界

- iCloud 中的 `Game-Projects` 应保持“已下载”状态；不要在多台 Mac 上同时运行会写入同一 Project 的制作台，文件锁只在本机生效。
- Project 文件可提交；`workspace`、`project.local.yaml` 和系统 SQLite 不提交。
- PNG 母版与批准 WebP 作为普通 Git 文件由 `Game-Projects` 父仓库记录。
- 浏览器加密保存供应商凭据；API Key 只在解锁后进入后端进程内存。
- 发布操作使用临时文件、`fsync` 和原子替换；只有批准修订进入 Release。
- 应用不会自动 commit、push 或修改 Git 历史。

## 文档

- [路线图](docs/roadmap.md)
- [系统主逻辑](docs/system-logic.md)
- [架构说明](docs/architecture.md)
- [Codex 监督式智能生产](docs/agentic-production.md)
- [Release 与游戏项目交付](docs/release-delivery.md)
- [资产库分类](docs/asset-library.md)
- [API 说明](apps/api/README.md)
- [设计参考](docs/design/README.md)
- [设计与交互验收基线](design-qa.md)
