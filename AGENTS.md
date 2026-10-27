# AGENTS.md

Guardian：通过已有的 Codex App Server Unix socket 扫描历史 thread，发现"额度耗尽中断"的会话，额度恢复后向**原 thread** 发送一条"继续"。不新建 App Server，不 fork，不换 thread ID。另有一个 stdlib 实现的 Web 控制台（`web.py` + `webui/`），用于看指标和配置监控项目。纯 Python 标准库，无第三方依赖。

## 常用命令

```bash
python3 -m unittest test_guardian test_web -v   # 全部测试（不需要真实 socket / Codex）
python3 -m unittest test_guardian.GuardianTests.test_not_loaded_resumed_original_id
python3 -m py_compile guardian.py web.py probe_transport.py
python3 guardian.py                      # dry-run 单次全量扫描（默认，不发送）
python3 guardian.py --status             # 查看本地 attempts 记录，不抢锁
python3 web.py --no-live                 # 本地起控制台（127.0.0.1:8787）
./install.sh                             # 安装/更新到固定目录并重启两个 user 服务
```

- 改完代码必须重跑 `./install.sh`：服务跑的是 `~/.local/share/codex-auto-resume/` 里的副本（含 `webui/`），不是仓库里的文件。
- 服务：`codex-auto-resume-all.service`（守护，`--daemon --interval 120 --send`）和 `codex-auto-resume-web.service`（控制台，127.0.0.1:8787）。日志用 `journalctl --user -u <unit> -f`。
- 不要在 unit 里改成仓库路径——历史上仓库移动导致 `CHDIR` 反复失败；unit 只引用固定安装目录。

## 改动前必读的约束

- **不要加依赖**。`probe_transport.py` 是手写的 WebSocket 客户端（Unix socket + RFC6455 帧编解码 + JSON-RPC）。不要换成 `websockets`/`requests`。
- **at-most-once 是核心语义**：`claim()` 必须在 `turn/start` 之前落库；响应不明确记 `unknown` 且**绝不重发**。不要"顺手"加重试或补偿逻辑。
- **默认 dry-run**，只有 `--send` 才真实续跑。手工验证、调试都不要随手加 `--send`（会真消费额度并可能让 Codex 自动改代码/执行命令）。
- **扫描必须遍历全部分页**：`thread/list` 的 `nextCursor` 有值就继续；分页异常、cursor 重复直接抛错放弃本轮，不允许截断或部分处理后再继续。
- 候选判定严格：最新 turn 为 `failed` 且 `error.codexErrorInfo == 'usageLimitExceeded'`，并且 `account/rateLimits/read` 的 `ordinaryUsageAllowed is True`（其他值一律等待）。
- `notLoaded` 的 thread 用 `thread/resume` 加载后**沿用原 thread ID**，不 fork。
- 状态与仓库、安装目录解耦：`~/.local/state/codex-auto-resume-all/`（`CODEX_GUARDIAN_HOME` 可覆盖），内含 `attempts.sqlite3`、`config.json`、`status.json` 和 `lock`。daemon 在跑时再启动一个非 `--status` 实例会因 flock 直接退出（`Another guardian instance is running`），这不是 bug。
- README 的"安全约束"章节仍是有效约束（并行会话可能改同一目录、无法保证 exactly-once 等），改行为时同步更新。

## Web 控制台

- `web.py` 用 stdlib `http.server` + 原生 JS，**没有构建步骤**；`webui/` 原样静态托管，改 HTML/CSS/JS 后刷新即可。
- 数据来源：`status.json`（guardian 每轮扫描结束时原子写入）和 `attempts.sqlite3`（以只读 URI 打开）。实时额度 `/api/quota` 通过 `guardian.Server` 探测，默认缓存 10s。
- 唯一写操作是 `PUT /api/config`（改 `config.json`，tmp+rename、0600）：要求 `Content-Type: application/json`，拒绝相对路径和不存在的目录，路径 realpath 去重。
- 项目匹配：`config.projects` 为空或全部禁用 = 监控全部（兼容原行为）；否则只有 `cwd` 等于项目路径或位于其子目录才监控。`guardian.py` 每轮重新 `load_config()`，保存配置无需重启守护器。
- 控制台无鉴权且只监听 `127.0.0.1`。不要在未加鉴权时提供 `turn/start` 等会真实续跑的操作接口。
- `test_web.py` 用临时 state dir + `127.0.0.1:0` 跑真实 HTTP 请求，`live=False` 避免连 socket；改 handler 时保留这两个约束。

## 测试注意

- `test_guardian.py` 用 `FakeServer`，完全离线，不需要 App Server。
- `FakeServer.call()` 按方法名硬编码。新增/修改 RPC 调用时要在 `FakeServer` 里同步实现，否则测试抛 `Unexpected method`。
- 测试会断言 `starts` 次数和 attempts 表行数，改判定/去重逻辑时优先扩展这些用例。

## 仓库与提交

- 远程：`https://gitee.com/renhehuang/codex-auto-resume.git`（新 clone 若无 origin 先 `git remote add origin ...`）。
- 提交信息用精简中文 + `feat/fix/refactor` 前缀，不写冗长 body。
- **不要自动提交**，等用户明确说"提交"。
