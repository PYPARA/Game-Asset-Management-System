# M5 Emperor-Simulator 试点

## 验收边界

M5 使用真实 Emperor-Simulator 资产，但不调用真实供应商，也不执行任何 Git 操作。试点脚本把已经存在的媒体批准记录作为确定性 replay 输入，写入完整的生成运行证据，然后通过当前 M2–M4 闭环完成旧媒体升级、Release v2，并在外部 checkout 执行可回滚的 Delivery 验证。

脚本只接受显式的 Project 根目录、SQLite 状态目录和 checkout 路径：

```bash
PYTHONPATH=apps/api/src .venv/bin/python scripts/m5_pilot.py \
  --project-root /path/to/Emperor-Simulator \
  --state-dir /tmp/gams-m5-state \
  --checkout /tmp/emperor-m5-checkout \
  --include-media-baseline \
  --release-name m5-emperor-media-baseline
```

脚本会：

1. 扫描 Project 并把旧的 `approved/assets/**` 媒体显式提升到内容寻址的 `production/sources/**` 与 `approved/objects/**`；原始 Revision 不会被覆盖。
2. 为五项试点资产建立零成本 deterministic replay 的 Plan、Job、Attempt、Finding、Action、Evidence 和事件链：绿衣透明立绘、同角色表情编辑、图标、背景和 CG。
3. 对全量媒体基线创建显式资产子集 Release v2，确保现有游戏 Manifest 不因五项试点而被截断。
4. 使用 checkout staging、Manifest/lock 哈希和验证命令执行 Delivery；失败时仍写入 `history/m5/pilot.json` 与失败 Delivery 收据。

## 旧媒体升级 API

```http
POST /api/projects/{project_id}/legacy-media-promotions
Content-Type: application/json

{"asset_keys":["icon.artifact-01","background.alchemy-room"]}
```

该操作只接受已批准的 `media` 资产，检查来源与运行 Blob 哈希，重新执行硬 QA，并通过普通媒体批准事务创建 Promotion Revision、Source/Runtime Artifact、QA 和带依赖哈希的 Review。重复请求会返回 `skipped: [{"reason":"already_promoted"}]`，历史 Revision 与旧审核记录仍保留。

旧的 `approved/assets/...` 目标路径会在 Promotion Revision 中规范化为 `public/assets/...`；这只改变新 Release 的目标逻辑路径，不移动或删除旧文件。

## 证据与预算

每项试点资产应能从 `history/m5/pilot.json` 的 ID 追到：

```text
Plan → Job → Attempt → Artifact/Evidence → Finding → Action → Review
                                                    ↘ Release → Delivery → gams-lock.json
```

Replay 不产生供应商调用，预算报告固定记录五项基础调用、零实际供应商调用和一次额外返工额度；真实供应商调用仍须由用户在正式计划中显式确认。

## 验收结果（2026-07-31）

- GAMS API 测试（含旧媒体升级、重复执行和 Release 子集）通过。
- 真实 Emperor Project replay 已完成：186 项 legacy media 均已升级，五项试点各有一个 Job/Finding/Action，Release `ae33b74b-9865-4945-833d-a3fc1cddc109` 包含 186 项媒体，snapshot hash 为 `sha256:cda89d6c68bc7cd14ff3b4fa0b3c7247bd25966a44a8f94a9946d3af8ca3b43b`。
- 迁移后从 Project 文件重建索引仍为 1,144 assets、1,330 revisions、372 artifacts、2 releases，扫描错误为 0。
- 隔离 Emperor checkout 的 `pnpm check` 通过（TypeScript + Vitest，15 tests）；`pnpm build` 通过。
- 隔离 checkout 的 `pnpm test:e2e` 已在允许浏览器进程的环境中运行，但四项测试在标题页等待 `开始新朝` 超时；当前 UI 实际按钮为 `开创新朝`。该断言差异存在于用户维护的 Emperor checkout，Delivery 已回滚并保留失败收据，GAMS 没有修改原始脏工作区。
- `gams-lock.json`、Manifest 和媒体文件只写入隔离 checkout；Project 只写入可重建的历史、Release 和 Delivery 记录。

在修正游戏 checkout 的既有 E2E 文案断言后，应重新执行上面的命令；脚本会复用已经升级的内容寻址对象和同名 Release，不会重复创建历史 Revision。

## Git 边界

M5 脚本和 Delivery 永远不执行 `git add`、`git commit`、`git push`、分支切换或历史改写。用户应分别审阅 GAMS Project 历史与 Emperor checkout diff，再决定是否提交。
