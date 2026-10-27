# Codex SSH 原会话动态额度续跑 Guardian v1（实验版）

适配用户现有的 Codex CLI `0.161.0` / `~/.codex/app-server-control/app-server-control.sock`。不新建 Codex App Server，不固定 Thread ID。

- **首次启动：全量遍历 `thread/list` 的所有分页**，纳入之前已发生的额度中断；以后每次扫描也做相同判断。
- 只检测 **最新 Turn 为 `failed` 且 `error.codexErrorInfo == 'usageLimitExceeded'`** 的会话。
- `ordinaryUsageAllowed is True` 才发送；其他值全部等待。额度可用后默认恢复所有满足条件的会话，不做单会话上限。
- 对同一 (Thread ID, failed Turn ID) 使用 SQLite 去重，发送之前记录 claim，结果不确定时标记 `unknown`，不会盲目重发。之后如果又出现新额度失败 Turn，视为新恢复事件。
- 在发送前重新确认目标状态和最新 Turn，避免人工已经续跑后重复发送。
- 原会话 `notLoaded` 时使用 `thread/resume` 加载该 Thread；不 fork，不更换 Thread ID。
- 默认**仅 dry-run**，加 `--send` 才会真的发送。提供 systemd user unit 模板和一个只读监控 + 项目白名单的 Web 控制台（`web.py`）。

## 安装

代码仓库可以放在任意位置；`install.sh` 会把脚本复制到固定目录 `~/.local/share/codex-auto-resume/`，systemd unit 只引用该目录，因此移动/重命名仓库不需要改 unit。

先手动验证（可选）：

```bash
python3 -m py_compile guardian.py probe_transport.py web.py
python3 guardian.py         # 首次只读扫描，包括历史失败线程
python3 guardian.py --status
python3 guardian.py --send  # 警告：会真实向所有符合条件的原会话发送“继续”
```

确认无误后安装为常驻服务：

```bash
./install.sh
journalctl --user -u codex-auto-resume-all.service -f
```

`install.sh` 会复制脚本、`webui/` 和两个 unit，`daemon-reload`、`enable` 并重启服务。之后更新代码只需重新执行 `./install.sh`。

- 安装目录：`~/.local/share/codex-auto-resume/`（可用 `CODEX_GUARDIAN_INSTALL_DIR` 覆盖）
- 状态库：`~/.local/state/codex-auto-resume-all/attempts.sqlite3`（不随仓库或安装目录变化）
- 配置文件：`~/.local/state/codex-auto-resume-all/config.json`（Web 控制台写入）

## Web 控制台

`install.sh` 会同时安装 `codex-auto-resume-web.service`，默认只监听 `127.0.0.1:8787`：

```bash
systemctl --user status codex-auto-resume-web.service
journalctl --user -u codex-auto-resume-web.service -f
# 在本地机器建立隧道，然后浏览器打开 http://127.0.0.1:8787/
ssh -L 8787:127.0.0.1:8787 huangrenhe@<server>
```

页面提供：

- 守护进程状态、SEND / DRY-RUN 模式、最近扫描时间与扫描间隔
- 额度：primary / secondary 使用率、重置时间、`ordinaryUsageAllowed`；支持实时探测（默认缓存 10s）
- 扫描指标：thread 总数、候选、未监控、去重、错误、本轮耗时
- 候选会话与未监控会话明细（thread、cwd、失败 turn）
- 提交记录（`attempts` 表，含 `unknown`）
- **监控项目配置**：添加/删除项目、启用/停用。守护器每轮扫描重新读取，保存后无需重启

项目匹配规则：**项目列表为空（或全部停用）= 监控全部 thread**；存在启用项目时，只续跑 `cwd` 等于项目路径或位于其子目录下的会话。

本地调试可直接运行：`python3 web.py --state-dir ~/.local/state/codex-auto-resume-all --no-live`。

只读 API：`/api/overview`、`/api/config`、`/api/attempts`、`/api/quota`；修改配置用 `PUT /api/config`（要求 `Content-Type: application/json`）。

若需要退出 SSH 后仍然运行，管理员可执行 `sudo loginctl enable-linger huangrenhe`。

## 安全约束

1. `--send` 是真实执行请求，可能引发代码修改、消耗额度和测试命令，建议先用测试仓库和 dry-run 验证。
2. 原始 Thread 可能包含需人工审批或生产操作；守护器不自动批准审批请求。尽管如此，恢复后的 Codex 仍会在原沙箱和批准策略下运行，因此需保持既有权限最小化。
3. 默认全部续跑意味着多个会话可能同时编辑**同一仓库目录**，造成代码冲突；建议你采用不同 Git worktree 保护并行任务。此版本不自动创建 worktree，也不限制同目录并发。
4. HTTP/WebSocket/JSON-RPC 无法保证 exactly-once；本守护器提供**最多一次自动提交尝试**，不确定请求不重发。`unknown` 需要人工审查后处理。
5. Mac 离线后能否持续运行依赖既有 App Server 的生命周期。如果服务器上的 Socket 消失，守护器会记录错误并在下一周期重连，**不会擅自重启/更换**原 App Server。
6. 如果 Codex 版本升级导致 `thread/turns/list` 分页出现历史不一致，本守护器可能漏检或误判；务必在实际环境做小规模验收后再用于重要项目。
7. Web 控制台默认只监听 `127.0.0.1` 且没有鉴权。能访问它就能修改监控项目，因此不要改绑 `0.0.0.0` 或直接暴露到公网，推荐 SSH 端口转发。
