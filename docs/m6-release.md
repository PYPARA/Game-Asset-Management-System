# M6 v1 发布验收记录

状态：GAMS v1 发布候选验收工具和运行手册已完成（2026-07-31）。

## 可重复验收

默认离线运行使用真实 `OpenAICompatibleProvider` 的 HTTP 路径和 loopback 故障注入服务，不调用供应商、不写入真实 Project、不执行 Git：

```bash
npm run acceptance:m6 -- --output /tmp/gams-m6.json
```

报告结构为 `format_version: 1`，每个 check 都有开始/结束时间、耗时、状态、证据和错误；退出码 `0` 表示没有失败，退出码 `2` 表示至少一个 check 失败。未提供真实供应商时，只有 `provider.live_contract` 为 `skipped`，不把它伪装成通过。

离线矩阵包含：

- `/models`、`/chat/completions`、`/images/generations`、`/images/edits` 成功协议；
- 限流、5xx、鉴权、额度、内容策略和坏响应分类及 retryable 边界；
- 8 个并发图片请求及观察到的最大 in-flight 数；
- 服务/Worker 重启后的持久 Job 恢复且不新增供应商 Attempt；
- `ENOSPC` 注入后的 checkout 文件/lock 恢复；
- 验证命令失败后的 Delivery 中断恢复；
- 删除 SQLite 后从 Project History 重建 Delivery 索引；
- Project v1 与 Manifest v1/v2 兼容边界。

如果要增加真实供应商成功验收，API Key 只通过环境变量传入：

```bash
GAME_ASSETS_M6_API_KEY='…' npm run acceptance:m6 -- \
  --provider-url https://provider.example/v1 \
  --require-live-provider \
  --output /tmp/gams-m6-live.json
```

报告只记录模型 ID、请求数量、状态码、分类和是否有 request id，不记录 Key。

## 发布门禁

发布前必须同时满足：

1. `npm test`、`npm run build`、Python 编译和 `git diff --check` 通过。
2. 离线 M6 报告 `failed: 0`；真实供应商 run 若纳入发布，`provider.live_contract` 也必须通过。
3. 目标 Project 全量迁移前预检无错误，批准媒体全部指向耐久 Artifact；Release preflight 和 Delivery verify 通过。
4. P0/P1 缺陷已关闭，或在发布收据中写出影响、恢复路径和明确人工负责人。
5. 外部游戏 checkout 的 `pnpm check`、`pnpm build`、`pnpm test:e2e` 和浏览器 smoke 结果随 Delivery 收据保存；GAMS 不替游戏仓库提交修复。

## 当前已知外部阻塞

M5 隔离 Emperor checkout 的类型检查和构建已通过。游戏仓库既有 E2E 测试仍查找 `开始新朝`，而当前 UI 文案为 `开创新朝`；这是消费端测试断言不一致，不是 GAMS 生产、Release 或 Delivery 错误。M6 复跑时当前 macOS 沙箱还出现 Playwright Chromium `browserType.launch`/`EPERM`，因此不能把这次浏览器结果标成通过。交付工具保持 fail-closed：验证失败会回滚 checkout 并写入 `failed` 收据。游戏仓库维护者修正文案断言并在可启动浏览器的环境中重跑：

```bash
pnpm check
pnpm build
pnpm test:e2e
```

在该外部断言修正前，M6 的 GAMS 门禁可以完成，但“Emperor 全量浏览器 E2E”仍按用户接受的降级方案留在发布清单中，不得标成已通过。

## 相关文档

- [用户操作手册](user-guide.md)
- [故障手册](troubleshooting.md)
- [格式升级策略](format-upgrades.md)
- [Release 与游戏项目交付](release-delivery.md)
