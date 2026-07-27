# 设计 QA——方案一：生产制作台

> 基线声明：以下结果是 2026-07-20 对 `main@2456639` 加当时统一 Project 架构迁移工作区的验收快照，不是当前分支的实时扫描或持续测试状态。代码、Project 数据或依赖变化后必须重新运行对应构建、测试和浏览器验收；最新计划状态以 [路线图](docs/roadmap.md) 为准。

## 参考资料

- 视觉基准：`docs/design/reference-workbench.png`（1487 × 1058）
- 未来 v1.1 的设计参考，特意不纳入 v1：`docs/design/reference-narrative-atlas.png`
- 实现：`apps/web/src/Workbench.tsx`、`apps/web/src/components/*`、`apps/web/src/styles.css`
- 最终浏览器截图：`docs/design/implementation-final-1280x720.jpg`
- 最终对比图：`docs/design/comparison-workbench-final.jpg`

## 视口与状态

- 浏览器 QA 视口：1280 × 720，设备像素比为 2（应用内浏览器固定的桌面端视口）。
- 状态：自动发现的 Emperor-Simulator 标准 Project、2D 媒体分类、选中已批准背景版本、Prompt 标签页。
- 布局保持计划中的 1024 px 最小宽度，在低于 1180 px 时自适应收缩，并支持高对比度焦点环和减少动态效果设置。生产构建验证了与 1440 × 1024 验收目标相同的响应式 CSS。

## 交互与集成证据

- 搜索 `宫廷夜宴` 后，结果集准确缩减为一行；清除搜索后恢复完整台账。
- 按资产类型筛选后返回了预期的 CG 子集。
- 选择 `portrait.han-lie.resolute` 后，检查器随之更新。
- 并排对比和叠加对比均能正常渲染；叠加模式显示了 50% 不透明度滑块。
- Prompt、硬性 QA 和修订历史标签页均可正常使用；标签页支持方向键左/右、Home 和 End 键。
- 只有在真实候选 ID、渲染结果和 QA 证据加载完成后，才会启用批准与驳回操作。
- 打开供应商设置后，焦点会被限制在弹窗内，并显示 IndexedDB、Web Crypto 和 XSS 安全边界警告。
- 新建 Project 窗口只在 `Game-Projects` 直属目录创建 `format_version: 1` Project；已有 Project 由系统自动发现。
- 新建窗口在桌面和 390 × 844 窄屏下均保持完整输入宽度、正确间距和可见焦点环，并支持 Enter、Escape 和焦点锁定。
- 设置窗口明确列出 Projects、`local-state`、Project `workspace` 和浏览器 IndexedDB 四类写入位置。
- 新浏览器会话真实加载了媒体列表缩略图和检查器大图；可视区域内没有破图，也没有应用运行时异常。

## 对比历史

1. 第一轮：`docs/design/comparison-workbench-round-1.jpg`。三栏结构、炭灰色视觉层级、表格密度、检查器和任务栏均与选定设计稿一致；仍有审核约束和无障碍审计问题需要解决。
2. 最终版本：`docs/design/comparison-workbench-final.jpg`。在保留视觉层级的同时，为尚未接通的操作增加了如实的禁用状态，并加入支持键盘操作的资产按钮、弹窗焦点管理、更强的对比度、候选修订与已批准修订的严格分离，以及安全的审核启用条件。

## 自动化验证证据

- 前端：25/25 项 Vitest 测试通过。
- 前端生产构建通过；仍有 575.29 kB 入口代码块警告，这是不阻塞发布的代码拆分优化项。
- API：23/23 项 pytest 测试通过。
- Emperor 游戏仓库：15/15 项测试通过，生产/PWA 构建通过。
- Emperor Project 扫描得到 1144 个资产、1144 个修订、189 个 rendition、190 条关系和 0 个错误。
- 两次 Project 发现和两次全量扫描并发执行均返回 HTTP 200，没有 SQLite 唯一键冲突。
- 背景、透明立绘、图标和生产锚点内容接口均返回 HTTP 200，并分别提供正确的 WebP/PNG MIME。
- 全文契约检查确认旧项目路径、外部媒体 URI、独立渲染目录、LFS 属性和重复候选路径字段均已清除。

该基线最终结果：通过
