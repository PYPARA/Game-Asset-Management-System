# M7 叙事地图暗色主题设计 QA

**视觉基准与证据**

- 主题基准：`docs/design/reference-workbench.png`，约束炭灰背景、面板层级、边框、控件与交互状态。
- 布局基准：`docs/design/reference-narrative-atlas.png`，仅约束三栏结构、内容组织与信息密度，不约束浅色皮肤。
- 暗色最终截图：`output/playwright/m7-narrative-atlas-dark-final.png`。
- 场景编辑器截图：`output/playwright/m7-narrative-atlas-dark-editor.png`。
- 三图组合对比：`output/playwright/m7-narrative-atlas-dark-comparison-final.png`。
- M7 后续资产库截图：`output/playwright/m7-followup-library-dark.jpg`。
- 风格圣经编辑截图：`output/playwright/m7-followup-style-bible-editor.jpg`。
- Prompt 配方字段/JSON 编辑截图：`output/playwright/m7-followup-prompt-recipe-editor.jpg`。
- 叙事地图回归截图：`output/playwright/m7-followup-narrative-atlas-dark.jpg`。
- 窄视口截图：`output/playwright/m7-followup-responsive-390.jpg`（390 × 844）。
- 浏览器环境：1488 × 1057 CSS px，deviceScaleFactor 1。两张基准图均为 1487 × 1058 px，最终实现为 1488 × 1057 px；1 px 的源尺寸差异不作为视觉 Finding。
- 验收状态：Emperor Simulator 真实 Project；叙事地图；第 1 章展开；场景“丹砂赤雨 · 起因”选中；搜索条件已清空。

**Findings**

- 无剩余 P0/P1/P2。
- 主题统一：顶栏、项目选择器、视图切换、章节树、场景正文、关系画布、资产覆盖栏、浮层和底部任务栏全部继承制作台暗色主题，没有白色或暖白色“孤岛”。
- token 与状态：叙事内部背景、面板、边框和文字 token 映射到全局 `--bg`、`--surface-*`、`--line*` 与 `--text*`；角色关系保留蓝色 `#84b5d5`，内容关系保留绿色 `#9bc696`；状态继续使用全局 green/amber/red，主操作使用 `--accent-strong`。
- 层级与交互：悬停、选中、禁用和输入焦点均保持暗色环境下的可辨识度；当前场景使用红色强调标记，正文保留宋体层级，工具界面继续使用系统无衬线与 Phosphor 图标。
- 布局与滚动：三栏尺寸、场景头部、覆盖栏固定操作区和 50 px 底部任务栏保持原业务布局；1488 × 1057 下无横向或纵向 body overflow，各栏独立滚动正常。
- 图片与数据准确性：真实 Project rendition 继续通过现有内容接口加载；当前场景没有直接关联 CG/背景时，中心节点明确显示场景实体占位，不借用参与角色立绘伪装场景媒体。
- 运行质量：资产制作台与叙事地图连续切换正常，最终浏览器控制台 warning/error 均为 0。

## M7 后续：项目规范与资产库分类验收

- 项目规范入口固定为“资产制作台 → 项目规范”；风格圣经与 Prompt 配方分别以 1 个普通资产登记，视觉锚点仍保留 3 个媒体资产，生成的 `production/style-bible.json` 不出现在资产行。
- 首次扫描真实 Emperor Simulator Project 后总资产为 1,146（基线 1,144 + 风格圣经 + Prompt 配方）；再次扫描保持 1,146，规范来源状态显示“已同步”。
- 资产库移除同级媒体总入口；“媒体”仅为分组标题，角色立绘 92、场景背景 20、剧情 CG 16、物品图标 50、结局插画 8、音频 0 互斥计数，未知 subtype 走对应分组“其他类型”。
- 默认入口为“场景”，真实结果 675；章节筛选为已入章 510 / 未分章 165，覆盖筛选为已就绪 675 / 有缺失 0。行内显示章节、`ready/required` 与缺失数。
- 风格圣经检查器安全渲染批准/候选内容，编辑器同时显示 Markdown 源码与实时预览（标题、列表、表格、引用、代码）；Prompt 配方提供字段表单、原始 JSON 切换、版本差异和来源路径。原始 HTML 作为文本处理，不进入 DOM。
- 编辑保存只创建候选；真实浏览器验证了编辑器取消不写入、非法 JSON 时保存禁用，以及视觉锚点继续进入媒体比较检查器。场景检查器与叙事地图支持双向定位同一 Asset ID。
- 新增页面、检查器和抽屉在 1488 × 1057 与 390 × 844 下均为炭灰主题；两档视口 `scrollWidth === clientWidth`，无 body overflow、白色残留或控制台 warning/error。

**自动化门禁（2026-08-06）**

- Web：58/58 Vitest 测试通过；生产构建通过（仅保留入口 chunk > 500 kB 的非阻塞拆分提示）。
- API：79/79 pytest 通过；仅有既有 Starlette TestClient 弃用警告。
- Python `compileall` 与 `git diff --check` 通过。

**Primary interactions tested**

- 资产制作台与叙事地图双向切换，标题、三栏结构和选中场景保持正确。
- 章节展开/折叠、场景选择以及章节/场景搜索与清除。
- 场景编辑器打开、现有字段载入、暗色抽屉样式与无写入取消。
- 当前真实 Project 的资产覆盖率为 100%，因此“生成缺失资产”和“预览生成方案”在 live QA 中按业务规则禁用，未修改真实数据强行打开；单元测试继续覆盖生成方案预览、缺失项物化以及交回现有计划编辑器。

**Comparison history**

1. 旧浅色实现：`output/playwright/m7-narrative-atlas-final.png` 与 `output/playwright/m7-narrative-atlas-comparison-final.png`。
   - 状态：已废弃，仅作为历史记录；不再代表 M7 验收结果。
2. 暗色第一轮：`output/playwright/m7-narrative-atlas-dark-pass1.png`。
   - 结果：完成核心 token 和三栏暗色映射，并据此继续检查编辑器与真实交互状态。
3. 暗色编辑器：`output/playwright/m7-narrative-atlas-dark-editor.png`。
   - 结果：场景编辑抽屉与输入控件没有浅色残留，取消后无数据写入。
4. 暗色最终轮：`output/playwright/m7-narrative-atlas-dark-final.png`。
   - 结果：搜索已清空，第 1 章与当前场景可见；无 body overflow；控制台 warning/error 为 0。
5. M7 后续资产库与规范编辑：`output/playwright/m7-followup-library-dark.jpg`、`output/playwright/m7-followup-style-bible-editor.jpg`、`output/playwright/m7-followup-prompt-recipe-editor.jpg`。
   - 结果：分类计数与编辑/预览流程通过；旧浅色截图仍保留为废弃历史，不作为当前颜色验收依据。
6. M7 后续叙事地图与响应式：`output/playwright/m7-followup-narrative-atlas-dark.jpg`、`output/playwright/m7-followup-responsive-390.jpg`。
   - 结果：资产库 ↔ 叙事地图连续切换、窄视口边界和滚动检查通过。

final result: passed
