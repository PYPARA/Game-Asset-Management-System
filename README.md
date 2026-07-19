# 游戏资产管理系统

一个本地优先的游戏资产生产工作台，用于管理结构化游戏内容和 2D 媒体资产。项目目录中的文件是权威数据源，并保持 Git 友好；SQLite 仅保存可重建索引、持久任务队列和本地操作记录。

## 已实现功能

- 制作台界面：资产筛选、版本比较、QA 审核、供应商设置和批量操作。
- FastAPI 服务：项目、资产、类型化关系、不可变修订、供应商配置、生成计划与任务、QA、审核和发布。
- OpenAI 兼容的结构化文本、图片生成及图片编辑适配器，并提供离线假供应商用于测试。
- 前端加密凭据缓存：API Key 解锁后只进入后端进程内存，不会写入 SQLite、项目文件或日志。
- Git 友好的 `.game-assets` 项目文件契约，以及只包含已批准版本的运行时 Manifest。
- Emperor-Simulator 只读迁移预览、幂等导入和兼容 Manifest 导出。

## 里程碑状态

首个实施里程碑已经完成，包括项目骨架、项目文件契约、Option 1“制作台”、仅监听回环地址的 API、持久化本地任务模型，以及经过真实项目验证的 Emperor dry-run、导入和导出。

审核操作严格依赖真实的候选修订、媒体版本和 QA 证据。尚未接通持久化流程的界面操作会明确禁用，不会伪装成成功。

下一个 v1 里程碑包括生成计划编辑器、结构化文本 Diff 与写回、项目文件持续监听，以及真实供应商在线验收。

独立的“叙事地图”工作台安排在 v1.1；其所需的类型化关系模型已经在 v1 中建立，后续无需迁移核心数据。

## 环境要求

- Node.js 20 或更高版本。
- Python 3.13 或更高版本。
- [uv](https://docs.astral.sh/uv/)。

## 安装与启动

在项目根目录执行：

```bash
npm --prefix apps/web install
uv sync --all-packages
npm run dev
```

启动后可访问：

- 前端开发服务：`http://127.0.0.1:4173`
- 后端 API：`http://127.0.0.1:8787`
- OpenAPI：`http://127.0.0.1:8787/openapi.json`

后端检测到 `apps/web/dist` 时，也会在 `http://127.0.0.1:8787/` 提供构建后的完整应用。

## 构建与测试

```bash
npm run build
npm test
```

也可以分别运行：

```bash
npm run test:web
npm run test:api
npm run test:emperor
```

## Emperor-Simulator 迁移

迁移器始终先以只读方式扫描旧项目。确认导入后，它会在单独的管理项目目录中写入 `.game-assets` 元数据，并将已有媒体保留为外部引用：

- 不复制现有大文件。
- 不修改旧项目。
- 保留稳定 Key、旧路径、尺寸、状态和 QA 证据。
- 无法确认的历史请求 ID、模型和成本使用 `unknown`，不会伪造数据。
- 重复导入保持幂等，未变化的文件不会被重写。

命令行示例：

```bash
# 只读预览
uv run --project packages/emperor_adapter emperor-adapter dry-run /path/to/Emperor-Simulator

# 导入到独立的管理项目目录
uv run --project packages/emperor_adapter emperor-adapter import /path/to/Emperor-Simulator /path/to/managed-project

# 导出兼容的运行时 Manifest
uv run --project packages/emperor_adapter emperor-adapter export /path/to/managed-project --format typescript --output assetManifest.ts
```

## 安全边界

这是一个本机单用户应用，不包含账号、多租户或云同步功能。

供应商 API Key 使用不可导出的 Web Crypto 密钥加密后存入 IndexedDB。解锁时，Key 只会发送给 `127.0.0.1` 后端并保存在进程内存中；后端重启后需要重新解锁。

前端加密缓存无法抵御同源脚本注入。因此应用采用严格的内容安全策略（CSP），不加载第三方运行时脚本或 CDN，并在设置界面明确提示这一风险。

Base URL 默认只允许 HTTPS、本机回环地址的 HTTP，以及用户在高级设置中明确允许的私有网络地址。包含用户名、密码、查询参数或 URL 片段的地址会被拒绝，避免秘密以明文形式进入本地配置。

## 权威数据源策略

- `.game-assets/` 保存项目定义、Schema、资产描述、不可变修订、关系、风格资料、Prompt 配方和发布清单。
- `output/game-assets/` 保存候选结果、驳回结果和 QA 证据，默认被 Git 忽略。
- SQLite 只保存可重建索引、任务队列和本地状态，不是项目内容的权威来源。
- 只有人工批准的具体修订才能写入正式路径并进入发布 Manifest。
- 正式媒体写回前会保留旧版本及 SHA-256，并使用临时文件和原子替换。
- 系统不会自动创建 Git 分支、提交或推送，也不会自动启用 Git LFS。

## 项目结构

```text
apps/web/                 React 19 + Vite + TypeScript 制作台
apps/api/                 FastAPI + SQLAlchemy + SQLite 后端
packages/emperor_adapter/ Emperor-Simulator 迁移与导出适配器
docs/                     架构和设计文档
design-qa.md              视觉、交互与真实项目验收记录
```

## 相关文档

- [架构说明](docs/architecture.md)
- [设计参考](docs/design/README.md)
- [设计与交互验收](design-qa.md)
- [后端说明](apps/api/README.md)
- [Emperor 适配器说明](packages/emperor_adapter/README.md)
