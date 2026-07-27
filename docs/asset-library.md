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
