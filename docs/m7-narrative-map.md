# M7 v1.1 叙事地图验收记录

日期：2026-08-06

状态：已完成。

## 已交付能力

- `GET /api/projects/{project_id}/narrative-map` 从普通 Asset、不可变 Revision、类型化 Relation 与 Rendition 聚合章节树、场景内容、关系图和资产覆盖，不建立第二套事实源。
- 章节识别支持 `chapter`、`story_arc`、`act`；场景识别支持 `scene`、`event`、`story_event`、`dialogue_scene`。现有 `story_arc.node_keys` 与 `contains` 关系都能形成章节树。
- 参与角色和引用可从 Revision 内容派生为可审计的关系投影；持久 Relation 仍是正式关系索引，投影不会写入或替代它。
- 场景编辑调用普通 `POST /api/revisions`，保存新的不可变候选并保留父修订；不会覆盖当前批准版本。
- `POST /api/projects/{project_id}/narrative-map/scenes/{scene_asset_id}/requirements` 把选中的缺失需求物化为普通 Catalog Asset。返回的 Asset ID 随后进入既有生成计划编辑器，继续使用供应商/模型冻结、DAG、预算、Artifact、QA、Review、Release 与 Delivery 契约。
- Web 增加独立“叙事地图”视图：章节树、场景案卷、关系画布、覆盖率、缺失项清单、生成方案预览和场景编辑器均可操作；叙事参考图只约束布局与信息结构，所有表面和控件继承制作台炭灰色主题。

## 兼容性边界

- `project.yaml` 的 `format_version` 保持 1；没有数据库迁移或新的 Project 目录。
- 叙事地图只保存 Asset 与 Revision；SQLite 仍是可删除重建索引。
- 缺失项在进入生产前必须获得普通 Catalog 稳定 Key；生成计划仍要求用户显式确认供应商、模型、调用预算与运行约束。
- Release Manifest 与 Delivery 收据格式未改变。

## 验收

- Web：58 项测试通过；生产构建通过。
- API：79 项测试通过；仅有既有 Starlette TestClient 弃用警告。
- Python `compileall` 与 `git diff --check` 通过。
- 真实 Emperor Simulator 扫描：88 个章节、675 个场景；510 个场景由 `story_arc.node_keys` 归入章节，165 个保留为“未分章”而不是被猜测归属。
- 1488 × 1057 应用内浏览器验证通过：暗色主视图、章节展开/折叠、搜索与场景编辑器可用，无 body overflow，控制台 warning/error 为 0。完整视觉对照见仓库根部 `design-qa.md`。
