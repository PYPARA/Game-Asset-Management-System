# GAMS v1 用户操作手册

本文面向第一次运行 v1 的制作者。Project 文件是事实源，SQLite 只是本机索引；删除 SQLite 不会删除 Project 历史。GAMS 只监听回环地址，也不会替用户执行 Git 提交或推送。

## 启动与检查

```bash
npm run dev
curl -s http://127.0.0.1:8787/api/health
```

首次启动会发现 `GAME_ASSETS_PROJECTS_ROOT` 下的 Project。Project 根目录必须有 `project.yaml`，且 `format_version: 1`。如果项目来自 iCloud，先确认目录已下载，再在同一台 Mac 上使用制作台。

## 配置供应商

1. 在“供应商”设置中新增 OpenAI-compatible 配置，填写名称、Base URL、文字/图片默认模型、并发、重试和价格。
2. 保存后刷新模型目录；模型返回的模态元数据优先，未知模型必须手动标记为文字或图片。模型刷新失败会保留上一次缓存。
3. 在浏览器逐个解锁供应商。API Key 只存在浏览器加密存储和当前 API 进程内存，不会写入 Project、SQLite、Job 或事件。
4. 设置全局文字默认路由和图片默认路由。图片编辑沿用图片路由。

归档只阻止新计划分配，不会删除历史 Job 或已确认快照。已确认任务不会因为后续修改供应商而换模型或自动回退。

## 创建、确认和运行计划

1. 新建计划，按任务类型编辑 Prompt、尺寸、Schema、依赖和参考任务。
2. 每项任务可单独选择供应商和模型；切换任务类型会重新使用该类型的全局默认，修改全局默认不会改写已存在草稿。
3. 在预算复核中逐任务检查供应商、模型、预计调用量和价格。未知价格会明确标记，不要把它当作零成本。
4. 点击“检查计划”后再执行“确认”。没有路由、模型为空或模型被明确标记为不兼容时无法确认；未分类或手填模型允许确认但带警告。
5. 确认时，Prompt、DAG、模型、供应商运行配置、并发、重试、价格和预算写入 Job 冻结快照。之后的设置编辑不会改变这份快照。

运行阶段按以下顺序查看：`output_received` → `hard_qa` → `semantic_qa` → `candidate_ready`。供应商返回本身不等于候选可用；QA 失败会进入 Finding 和 `awaiting_user`。

## 处理停止和恢复

- 凭据锁定：只暂停对应供应商的任务，解锁后在运行检查器点击恢复；跨供应商下游继续等待上游。
- 限流或 5xx：Runner 只在冻结预算和重试上限内重试，检查事件中的 `retry_after`、Attempt 和实际成本。
- 鉴权、额度、计费、内容策略、模型不可用：不会自动换供应商或模型，按停止原因处理后创建重做任务或新计划。
- QA/语义问题：选择确定性 Worker 修复、重试、重新生成、图片编辑或等待人工；每个动作都要重新检查证据哈希。

CLI 等价操作：

```bash
gams run inspect <plan-id> --json
gams run resume <plan-id> --json
gams agent diagnose <job-id> --json
```

`resume` 只恢复有已落盘输出、确定性 Worker 或明确可重试状态的 Job；未知交付不会被猜测为成功。

## 审核、Release 和 Delivery

1. 只批准通过硬 QA 且引用耐久 Artifact 的候选。批准会创建不可变 promotion Revision。
2. 执行 Release 预检；预检是全有或全无，任何 Blob 缺失、哈希不符、依赖失效或路径碰撞都会阻止创建。
3. 对目标游戏 checkout 先执行 preview，再 apply。apply 在 checkout 内 staging，最后写入 `gams-lock.json`；未知文件不删除，受管文件被手工修改时阻止覆盖。
4. apply 后执行 verify。重复 apply 在目标未变化时产生 `no_op` 收据；回滚通过重新导出旧 Release 完成，不改写 Release 历史。

```bash
gams release preflight <project> --json
gams release create <project> --name v1.0.0 --json
gams export preview --project <project-id> --release <release-id> --json
gams export apply --project <project-id> --release <release-id> --run-commands --json
gams export verify --project <project-id> --release <release-id> --run-commands --json
```

Delivery 收据保存在 `history/deliveries`，目标 checkout 只保存可独立使用的导出文件和 `gams-lock.json`。
