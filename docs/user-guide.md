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

## 使用 Agent 生成中心

1. 在资产制作台或叙事地图顶栏点击“生成中心”。它只打开全局会话列表，不会自动创建空会话；在左侧“最近会话”标题旁点击“新建会话”后，才会创建一个无上下文会话。
2. 用自然语言说明目标、用途、风格和需要参考的资产。中间区域显示可见 Agent 消息与只读工具事件；右侧方案检查器持续更新资源、Prompt、模型、参考图、依赖、落地路径和预算。
3. 信息不足时，Agent 会在聊天流中显示 1–3 个问题并暂停当前回合；选择或填写答案后点击“提交回答”，同一回合会继续规划。浏览器刷新会恢复问题卡片；API 重启后卡片会切换为“提交并继续新回合”。右侧“需要你的判断”只用于汇总和定位问题。结构化文字必须引用项目内 JSON Schema；未解决问题、非法路径、循环依赖、未保存草案或未接受的警告都会阻止确认。
4. 使用“预览确认”核对候选暂存位置与批准后落地路径，勾选明确授权后再点击“确认并执行”。确认前不会登记新资产、创建 GenerationPlan 或调用供应商。
5. 确认会原子登记 `new` 资源、创建并确认一个 GenerationPlan，然后自动打开运行检查器。后续继续使用现有 Runner、硬 QA 和人工审核流程。

生成中心是可收起的非模态侧边栏，宽度约 820px，不会锁定制作台滚动或操作。收起后会话实例继续订阅事件流，多个会话可以并行运行并独立切换；重新打开生成中心即可看到最新消息和草案。Codex/App Server 不可用、超时或返回无效草案时，同一会话会切换到人工编排，已经读取的上下文和草案不会丢失。生成会话统一停留在制作台 `/`，不再维护旧的 `/generation/*` 页面。

## 使用叙事地图

1. 在顶栏工作台切换中选择“叙事地图”。左侧章节树来自 `chapter`/`story_arc` 与 `scene`/`event` 资产；`contains` 关系和故事弧的 `node_keys` 都会用于归档场景，无法证明归属的场景保留在“未分章”。
2. 选择场景后，中部读取当前候选修订（没有候选时读取批准修订），显示概述、对白与类型化关系；右侧按场景的 `asset_requirements`、参与角色和引用计算覆盖率。
3. 点击铅笔编辑场景。保存会创建新的不可变候选 Revision，不会覆盖批准修订，也不会自动批准。
4. 在资产需求中使用普通稳定 Key，例如 `media.cg.court_confrontation`。缺失项可先预览生产方案，再进入预算确认。
5. “生成缺失资产”仍作为带场景上下文的快捷入口，会在生成中心创建会话并固定相关资产。新资源只在最终确认时登记为普通 Catalog Asset；仍需逐任务确认供应商、模型、Prompt、DAG、尺寸和预算，后续继续经过 Artifact、硬 QA、人工审核、Release 与 Delivery。

叙事地图没有独立发布按钮或独立资产 ID。删除 SQLite 后，章节树、场景内容、关系和已物化资产仍可从 Project 重建。

## 查看与编辑项目规范

在资产制作台侧栏的“项目规范”分组中打开“风格圣经”或“Prompt 配方”。风格圣经同时显示 Markdown 源码和安全实时预览（支持标题、列表、表格、引用和代码，不执行原始 HTML）；Prompt 配方提供常用字段表单，并可切换到原始 JSON。右侧检查器会显示来源路径、同步/漂移/缺失状态和版本历史。

保存只会创建不可变候选修订，不会立即覆盖 `production/style-bible.md` 或 `production/prompt-recipes/emperor-primary.json`。审核通过后才原子写回源文件；驳回保留原批准版本。重复扫描保持幂等，外部文件按哈希进入候选，文件暂时缺失时仍可阅读最后批准版本。`production/style-bible.json` 是批准时生成的机器 profile，不会出现在普通资产列表。

资产库中的“媒体”是分组标题，不是重复的总类入口。实体（角色、地点、物品）与其表现文件（立绘、背景、图标）仍是独立资产，通过关系连接。默认入口为“场景”；场景行可按章节归属和覆盖状态筛选，检查器可以跳转叙事地图，叙事地图中的场景 Key、标题和关系节点也能返回同一资产。

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

### 工作区快捷入口和版本控制

左侧“工作区”是资产库上的任务视图：待审查按候选审核状态筛选，已关注是当前浏览器按 Project 保存的星标，我创建的只有在 Project 提供 `created_by`/`author` 元数据时才启用，已发布只包含真正进入 Release 的资产。点击资产库叶子入口会清除这些临时工作区条件，避免组合筛选把结果隐藏。

“本地提交”和“拉取 / 推送”只展示 Git 边界说明，不会在后台执行 Git 命令。GAMS 不持有远端凭据，也不会替用户解决冲突或改写历史；请在可信 Git 客户端中审阅差异后自行提交和同步。Release 交付是独立的文件事务和清单流程，不等同于 Git 提交。
