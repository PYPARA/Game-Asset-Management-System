# 资产库分类

## 核心类型

| 类型 | 含义 | 示例 |
|---|---|---|
| `content` | 玩家经历或阅读的结构化记录 | 故事、事件、奏折、结局 |
| `design` | 规则与玩法设计 | 行动、数值、经济、UI 流程 |
| `entity` | 游戏世界中可引用的对象 | 角色、地点、物品、成就 |
| `media` | 表现和运行媒体 | 肖像、背景、CG、图标、结局图、音频 |
| `production` | 生产约束与参考 | 风格锚点、Prompt 配方、QA 规则 |

角色与肖像、物品与图标、内容与 CG 是不同资产，通过 `depicts`、`represents`、`illustrates` 等关系连接。稳定 Key 不随界面分组变化。

## 制作台任务视图

侧栏的叶子入口是互斥的任务视图，不改变核心 `AssetKind`，也不把实体与表现媒体合并。“媒体”是不可点击的分组标题；未知 subtype 会进入相应分组的“其他类型”，并始终保留在“全部资产”。当前 Emperor Project 的视图计数如下：

| 分组 | 入口 | 数量 |
|---|---|---:|
| 叙事 | 场景 | 675 |
| 叙事 | 故事弧 | 88 |
| 叙事 | 奏折 | 24 |
| 叙事 | 结局内容 | 14 |
| 世界 | 角色 | 41 |
| 世界 | 地点 | 12 |
| 世界 | 物品 | 50 |
| 世界 | 成就 | 40 |
| 媒体 | 角色立绘 | 92 |
| 媒体 | 场景背景 | 20 |
| 媒体 | 剧情 CG | 16 |
| 媒体 | 物品图标 | 50 |
| 媒体 | 结局插画 | 8 |
| 媒体 | 音频 | 0 |
| 设计 | 游戏设计 | 11 |
| 项目规范 | 风格圣经 | 1 |
| 项目规范 | Prompt 配方 | 1 |
| 项目规范 | 视觉锚点 | 3 |

没有历史选择时默认打开“场景”。场景是现有 `content` 资产的派生视图，识别 `scene`、`event`、`story_event`、`dialogue_scene`；不会复制资产或新增 API。场景行显示章节归属、`ready/required` 覆盖率和缺失数，并支持“全部 / 已入章 / 未分章”与“全部 / 已就绪 / 有缺失”筛选。当前真实项目为 675 个场景，其中 510 个已入章、165 个未分章。

## 项目规范来源

扫描以下两个外部文件后，首次导入为已批准基线（总资产从 1,144 增至 1,146）：

- `design.style_bible.primary` → `production/style-bible.md`（Markdown）
- `production.prompt_recipe.emperor_primary` → `production/prompt-recipes/emperor-primary.json`（JSON）

页面入口为“资产制作台 → 项目规范 → 风格圣经 / Prompt 配方”。编辑器只保存不可变候选；人工批准后才原子写回源文件。重复扫描按源哈希幂等，外部漂移生成待审候选，驳回不改源文件；来源缺失时保留最后批准版本并显示提示。`production/style-bible.json` 只是批准时生成的机器 profile，不作为独立资产展示。

## 集合文件

资产不按 Key 创建目录。Catalog 按领域和类别保存集合，例如：

- `catalog/content/stories/<domain>.json`
- `catalog/content/events/<domain>.json`
- `catalog/entities/characters.json`
- `catalog/media/portraits.json`
- `catalog/relations.json`

集合只保存当前描述和修订指针；不可变内容位于 History。这样可以保持清晰目录、稳定 Git diff 和完整审计历史。

## 状态

内容状态、生成状态和发布状态彼此独立。只有明确批准的修订可以成为 `current_revision_id`；候选只使用 `latest_candidate_revision_id`。未知来源时间使用 `null` 并标记 `time_accuracy: unknown`，不伪造日期。
