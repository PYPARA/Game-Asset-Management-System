# Release 与游戏项目交付

> 文档状态：目标契约，尚未实现
> 基线日期：2026-07-27
> 当前代码只支持 Project 内的 Release v1；本文中的候选提升、Manifest v2、外部导出、Delivery、`gams-lock.json` 和回滚均为后续里程碑。

## 目标

Release 与 Delivery 是两个不同动作：

- Release 把一组精确、已批准修订封装成 Project 内不可变快照。
- Delivery 把某个 Release 显式同步到绑定的游戏 checkout，并完成验证和收据记录。

创建 Release 不写外部仓库；Delivery 不改变历史 Release。GAMS 永不执行 Git add、commit、push 或历史改写。

## 当前状态与目标差异

| 环节 | 当前实现 | 目标契约 |
|---|---|---|
| 候选存储 | 生成结果和归一化图位于 `workspace/candidates` | workspace 仅保存临时候选；审批前提升为内容寻址耐久对象 |
| 批准 | 审核绑定修订并更新 `current_revision_id` | 审核同时绑定耐久 Artifact 哈希，正式修订不引用 workspace |
| Release | v1 Manifest；跳过不合格资产后仍可创建 | Manifest v2；全量预检，任一项失败则整次失败 |
| 恢复 | Manifest 在 Project，Release DB 索引不能完整重建 | Release 与 Delivery 均能从 Project 文件重建索引 |
| 外部导出 | 未实现 | preview、stage、verify、apply、rollback 和幂等检测 |
| 游戏侧状态 | 无受管文件边界 | `gams-lock.json` 记录版本与所有受管文件哈希 |
| Git | 不自动操作 | 保持不自动操作，只展示 Project 与游戏仓库 diff |

## 不变量

1. workspace、SQLite 和 Codex 会话都不是已批准媒体的依赖。
2. 审核绑定精确修订、依赖哈希与媒体 Blob 哈希，不能只绑定稳定 Key。
3. Release 是不可变、完整、可独立验证的 Project 文件。
4. Release 创建和 Delivery 应用都必须 fail closed，不产生静默部分成功。
5. 游戏仓库中的未知文件永不删除。
6. 已由 GAMS 管理但被人工修改的文件，未经显式处理不得覆盖。
7. `gams-lock.json` 最后写入；它存在即表示受管文件已经全部应用并验证成功。
8. 回滚是重新交付一个旧 Release，不修改或删除任何历史 Release。

## 候选提升与批准媒体

### 存储分层

```text
workspace/candidates/...                 临时供应商输出与归一化候选，可删除
production/sources/<prefix>/<hash>.<ext> Git 跟踪的原始制作来源，内容寻址且不可变
approved/objects/<prefix>/<hash>.webp    Git 跟踪的批准运行媒体，内容寻址且不可变
history/objects/...                      正式修订与 Artifact 元数据
history/reviews/...                      不可变审核决定
```

`<prefix>` 使用完整 SHA-256 的前两个字符，文件名使用完整哈希。若目标文件已存在，字节哈希必须相同，否则视为存储损坏并停止。

### 原子提升流程

批准媒体候选时，Controller 按以下顺序执行：

1. 锁定 Project，并重新读取候选修订、最新 QA、依赖哈希和 Artifact 哈希。
2. 确认候选仍为待审状态，所有媒体均无阻塞 Finding。
3. 把原始输出复制到临时文件，计算哈希并原子落入 `production/sources`。
4. 把归一化运行媒体复制到临时文件，复验解码、尺寸和哈希，并原子落入 `approved/objects`。
5. 构造只引用耐久对象的正式媒体修订。若候选修订不可变且引用 workspace，则创建新的 promotion 修订，不原地改写候选。
6. 写入绑定正式修订、依赖哈希和 Artifact 哈希的审核决定。
7. 最后更新 Catalog 的 `current_revision_id`；失败时不改变原指针。

源文件和 WebP 的提升都成功后才写审核决定。孤立但哈希正确的内容寻址对象可以由后续垃圾回收识别；不能为了清理而回滚已经存在的共享 Blob。

驳回不会提升候选。需要保留的驳回证据可继续位于 `workspace/rejected`，也可由用户显式封存为历史 Artifact。

## Release Manifest v2

### 创建前预检

`gams release preflight` 必须一次性检查拟发布集合：

- 每个要求的稳定 Key 存在且没有重复。
- `current_revision_id` 指向已批准修订。
- 批准决定有效，记录的依赖哈希仍等于当前依赖哈希。
- 所有媒体 Artifact 存在于耐久目录，实际 SHA-256 与元数据一致。
- 最新硬 QA 和要求的语义 QA 都没有阻塞 Finding。
- 每个导出目标逻辑路径合法、唯一且符合游戏消费契约。
- 内容 Schema、关系目标和引用的媒体 Key 完整。
- 生成的 Manifest snapshot hash 可重现。

预检返回完整问题列表，但只要存在一个阻塞问题，`release create` 就拒绝执行。不写空 Manifest、不跳过坏资产，也不更新任何 `publication_status`。

### Manifest 结构

目标 v2 结构示例：

```json
{
  "format_version": 2,
  "release_id": "release_...",
  "project_id": "project_emperor_simulator",
  "name": "emperor-pilot-001",
  "created_at": "...",
  "snapshot_hash": "sha256:...",
  "assets": [
    {
      "key": "portrait.han-lie.resolute",
      "kind": "media",
      "subtype": "portrait",
      "revision_id": "revision_...",
      "content_hash": "sha256:...",
      "dependency_hash": "sha256:...",
      "media": [
        {
          "artifact_id": "artifact_...",
          "blob_hash": "sha256:...",
          "project_path": "approved/objects/ab/ab...webp",
          "target_path": "public/assets/portraits/core/han-lie/resolute.webp",
          "media_type": "image/webp",
          "width": 1024,
          "height": 1536,
          "byte_size": 275154
        }
      ]
    }
  ]
}
```

规范化规则：

- 数组按稳定 Key、Artifact ID 和目标路径稳定排序。
- snapshot hash 对去除自身 hash 字段后的规范 JSON 计算。
- 时间戳、显示名称或机器绝对路径不得进入内容寻址计算。
- Manifest 不含 `game_root`、供应商密钥、Codex 凭据或 workspace 路径。
- `releases/<release_id>/manifest.json` 不可变；Project 根部的当前 Release 指针只保存 release ID 与 hash，可原子替换。

## Project 与本机导出配置

提交到 Project 的 `project.yaml` 保存相对输出契约和 argv 形式的验证命令；被忽略的 `project.local.yaml` 只保存本机 checkout 绑定。

目标配置示例：

```yaml
# project.yaml
export:
  format_version: 1
  content_path: src/generated/content
  manifest_path: src/generated/game-assets/assetManifest.ts
  assets_path: public/assets
  lock_path: gams-lock.json
  validation_commands:
    - argv: [pnpm, check]
    - argv: [pnpm, build]
    - argv: [pnpm, test:e2e]
```

```yaml
# project.local.yaml（不提交）
game_root: /Users/username/Documents/Github/Emperor-Simulator
```

所有配置路径都相对于对应根目录解析，并经过逃逸检查。验证命令直接以 argv 和固定 `cwd=game_root` 启动，不经过 shell，不允许重定向、命令替换或拼接环境变量。额外环境值需要使用独立白名单字段。

## Delivery 流程

### 1. Preview

`gams export preview <release>` 只读检查 Project 与游戏 checkout，输出：

- Release ID、Manifest hash、目标 checkout 和当前 lock 状态。
- 将新增、更新、保持、删除的受管文件。
- 未知文件与受管文件人工篡改冲突。
- 路径碰撞、大小变化和预期目标哈希。
- 将执行的验证命令及预计影响。

Preview 不创建 checkout 文件，不修改 Project，不运行验证命令。

### 2. Stage

Controller 在目标 checkout 所在文件系统创建隔离 staging 目录，确定性生成：

- `src/generated/content` 下的版本化内容模块。
- `src/generated/game-assets/assetManifest.ts` 或项目声明的等价 Manifest。
- `public/assets` 下的 Release 媒体。
- 候选 `gams-lock.json`。

所有 staged 文件均重新计算 SHA-256。生成器版本、Release snapshot hash 和目标相对路径共同决定 Delivery 计划；相同输入必须产生相同字节。

### 3. Validate staging

在应用前完成静态验证：

- staged 文件集合与 Release 完全对应。
- 导入路径、生成代码格式和 Manifest Schema 合法。
- 目标路径无重复、大小写碰撞或目录逃逸。
- lock 中每个受管文件 hash 与 staged 字节一致。

如游戏验证只能读取正式路径，则在受控临时 checkout/worktree 中运行，而不是先破坏当前工作版本。实现必须记录采用的验证环境。

### 4. Apply

应用时获取目标锁，并再次核对 Preview 基线：

1. 读取现有 `gams-lock.json`。
2. 验证所有旧受管文件仍与旧 lock hash 一致；有人工改动即停止。
3. 把将替换或删除的旧受管文件备份到 Delivery 临时区。
4. 从 staging 原子替换受管文件；不触碰未知文件。
5. 运行配置的验证命令。
6. 全部通过后最后原子写入 `gams-lock.json`。
7. 在 Project 写入不可变 Delivery 收据。

任何文件操作或验证失败都恢复备份、移除本次新增的受管文件，并保持旧 lock。恢复本身失败时进入 `recovery_required`，保留 staging、备份和人工恢复说明，禁止继续新的 Delivery。

### 5. Verify

`gams export verify` 可在任意时间重新检查：

- lock 的 project、release、snapshot 和 exporter 版本。
- 所有受管文件是否存在且哈希一致。
- 游戏 Manifest 引用的内容和媒体是否完整。
- 可选地重新运行验证命令。

### 6. Idempotency

如果目标 lock 已指向相同 Release/snapshot、受管文件列表一致且所有哈希通过，重复 apply 产生 `no_op` Delivery 收据，不重写文件也不重复运行高成本验证，除非用户显式要求 `verify`。

## `gams-lock.json`

游戏仓库根部的 lock 记录当前已验证交付：

```json
{
  "format_version": 1,
  "project_id": "project_emperor_simulator",
  "release_id": "release_...",
  "release_manifest_hash": "sha256:...",
  "snapshot_hash": "sha256:...",
  "exporter_version": "...",
  "delivered_at": "...",
  "managed_files": [
    {"path": "src/generated/content/registry.ts", "sha256": "sha256:...", "byte_size": 1234},
    {"path": "src/generated/game-assets/assetManifest.ts", "sha256": "sha256:...", "byte_size": 5678},
    {"path": "public/assets/portraits/core/han-lie/resolute.webp", "sha256": "sha256:...", "byte_size": 275154}
  ]
}
```

`managed_files` 按路径稳定排序。未知文件没有出现在列表中，因此 GAMS 不删除它们。旧 lock 中存在但新 Release 不再需要的文件可以删除，但仅限文件当前 hash 仍等于旧 lock；否则视为人工篡改并停止。

## Delivery 收据

Project 中的 `Delivery` 是不可变审计记录，至少包含：

```json
{
  "id": "delivery_...",
  "project_id": "project_emperor_simulator",
  "release_id": "release_...",
  "release_manifest_hash": "sha256:...",
  "target": {
    "checkout_fingerprint": "sha256:...",
    "display_path": "/Users/.../Emperor-Simulator"
  },
  "status": "succeeded | no_op | rolled_back | failed | recovery_required",
  "previous_release_id": "release_...",
  "files": [],
  "validation_results": [],
  "rollback": null,
  "created_at": "..."
}
```

绝对路径只作为本机显示信息，不进入 Release，也不作为跨机器身份。`checkout_fingerprint` 可由仓库根标识、文件系统信息或用户确认的本机绑定生成，但不得包含密钥。

验证结果记录 argv、cwd 的脱敏显示、开始/结束时间、退出码和截断日志 Artifact。成功收据可以从 Project 重建 SQLite Delivery 索引。

## 回滚

回滚不修改历史 Manifest。用户选择旧 Release 后，系统执行一次新的 Delivery：

1. 对旧 Release 执行当前 checkout 的 Preview。
2. 以旧 Release 生成新的 staging 和候选 lock。
3. 按正常 Apply/Verify 流程替换当前受管文件。
4. 新收据记录从当前 Release 回到旧 Release 的关系。

如果只是当前 apply 中途失败，则使用本次备份做事务恢复，不创建伪造的旧 Release。只有验证成功并写入 lock 的版本才称为 delivered。

## Emperor-Simulator 具体契约

当前本机标准 Project 已声明：

```text
Project ID:       project_emperor_simulator
游戏 checkout:   /Users/0x10/Documents/Github/Emperor-Simulator
内容目标:         src/generated/content
资产 Manifest:   src/generated/game-assets/assetManifest.ts
媒体目标:         public/assets
```

目标游戏仓库消费以下受管区域：

- `src/generated/content/**`：确定性 TypeScript 内容模块；现有 `src/content/contentPack.ts` 继续作为消费/组合层，不由导出器随意改写。
- `src/generated/game-assets/assetManifest.ts`：运行时稳定 Key、类型、路径、尺寸与预加载分组。
- `public/assets/**`：WebP 等运行媒体，保持游戏现有 `/assets/...` 消费语义。
- `gams-lock.json`：GAMS 交付边界；游戏运行时不必读取，但 CI/开发者可验证。

每次试点 Delivery 必须在 checkout 根目录依次通过：

```bash
pnpm check
pnpm build
pnpm test:e2e
```

`pnpm check` 当前包含 TypeScript 检查和 Vitest；`pnpm build` 另行验证生产/PWA 构建；`pnpm test:e2e` 运行 Playwright。任一命令失败即回滚 Delivery，并先分类为资源、导出器或消费端代码问题，不能默认重新生成图片。

试点首批应包含绿衣透明立绘、同角色表情编辑、图标、背景和 CG，以同时覆盖 Alpha、身份保持、尺寸/压缩、宽幅构图与 Manifest 分类。

## Git 边界

Delivery 成功后，GAMS 只展示：

- Game-Projects 父仓库中新增的批准对象、修订、Release 和 Delivery 收据。
- Emperor-Simulator 中生成内容、Manifest、媒体和 lock 的 diff。
- 验证命令和结果。

系统不会执行：

- `git add`
- `git commit`
- `git push`
- 分支创建、切换、rebase、reset 或历史改写

用户可以在两个仓库分别审查和提交。项目不得把“Git 已提交”作为 Delivery 成功条件，也不得把“Delivery 成功”误报为 Git 已保存。

## 目标 CLI 与 API

计划中的命令：

```text
gams release preflight <project> [--json]
gams release create <project> --name <name> [--json]
gams export preview <release> [--json]
gams export apply <release> [--json]
gams export verify [<release>] [--run-commands] [--json]
gams export rollback <release> [--json]
```

Web API 使用相同领域服务，提供预检结果、执行状态、事件流和 Delivery 历史。CLI、API 和 UI 不得各自实现不同的文件事务逻辑。

## 测试与验收

至少覆盖：

- 删除 workspace 后，批准媒体仍能扫描、预览、复验、Release 和 Delivery。
- 删除 SQLite 后，从 Project 恢复 Release 与 Delivery 历史。
- Release 缺一项即整体失败，不写部分 Manifest 或状态。
- Manifest v2 规范化与 snapshot hash 可重现。
- 同一 Release 重复交付 no-op，换 checkout 后正确生成新收据。
- 新旧目标路径碰撞、大小写碰撞、目录逃逸和 Blob 哈希错误。
- 未知文件保持不变；受管文件人工篡改时拒绝覆盖。
- apply 每个文件边界、验证命令边界和 lock 写入边界的故障注入与恢复。
- 旧 Release 回滚通过重新交付完成，历史 Manifest 不变。
- Emperor 内容清单、TypeScript、Vitest、生产构建和浏览器 smoke/E2E 全部通过。
- 自动化测试断言 GAMS 与 Codex 不会执行 commit 或 push。
