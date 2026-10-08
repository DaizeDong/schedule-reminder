# schedule-reminder

在防崩溃的 SQLite 存储里追踪待办、事件与进度；通过一个稳定的 CLI/JSON API 经 Discord 发到期提醒。

[![Claude Code Skill](https://img.shields.io/badge/Claude%20Code-Skill-orange?style=flat)](https://docs.anthropic.com/en/docs/claude-code)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Languages](https://img.shields.io/badge/Languages-EN%20%2F%20CN-blue?style=flat)](README.md)
[![Roadmap](https://img.shields.io/badge/Roadmap-v0.6.0-purple?style=flat)](ROADMAP.md)

[English](README.md) | [中文版](README_CN.md)

---

## ⭐ 设计哲学

schedule-reminder 是一个 **T0 基础设施基座**：其他 skill 往它写提醒、从它读进度。下游通过带
`api_version` 的 CLI/JSON 契约，以及单独声明的 Task Console 只读关联接口集成，不直接读取数据库内部结构。
v0.1 着重保证并发安全、防崩溃持久化、受保护的状态机、幂等写、至少一次投递和未知字段保留。

稳定契约需要更严格的输入与状态检查，代价是拒绝含糊写入。到期提醒保留至少一次投递的重试行为。
带业务事件身份的通知将回执绑定到具体内容；发送结果不确定时保留待核对状态，不自动重发。
注册任务、健康报告或源码检查不能单独证明提醒已送达。

📜 **[完整设计理念 -> PHILOSOPHY.md](PHILOSOPHY.md)**

---

## 它是什么(不是什么)

**是**：一个持久、可查询的日程 + 备忘存储，带 `pending/doing/done/blocked/cancelled` 状态机、经本地 Discord
relay 的到期提醒派发，以及供人和其他 skill 调用的稳定 `reminder.py [--actor NAME] <verb>` API。

**不是**：一次性通知器（那是 relay）、日历界面、云服务。如果没有东西需要「持久化 / 被查询 / 被提醒」，你不需要它。

## 安装

```
/plugin install github:DaizeDong/schedule-reminder
```

或手动克隆：

```bash
git clone --recurse-submodules https://github.com/DaizeDong/schedule-reminder.git ~/.claude/plugins/schedule-reminder
```

先将 `SCHEDULE_REMINDER_CONFIG` 指向已初始化、纳入版本管理的 PRIVATE 伴生仓，再写入运行数据。预览所选能力后运行幂等安装器：

```powershell
pwsh -File skills/schedule-reminder/scripts/install.ps1 -Capabilities store,remind -Plan
pwsh -File skills/schedule-reminder/scripts/install.ps1 -Capabilities store,remind
```

默认选择 store 和 remind；空选择不执行安装，ingest 和 work 可单独选用。安装器只有在所选能力的就绪证据齐全时才返回成功，任务注册成功不等于已就绪。`health` 能正常输出 JSON 时仍返回成功，具体状态请查看 `health.readiness.ready`。PRIVATE 存储、任务回读及尚未测量的 ingest/work 就绪边界见[部署说明](skills/schedule-reminder/reference/deployment.md)。

## 配置

将 `SCHEDULE_REMINDER_CONFIG` 指向选定的 PRIVATE 伴生仓。切换配置时，同步调整根目录及已设置的 `AGENT_CENTER_CONFIG` 文件覆盖值。[CONFIG.md](CONFIG.md) 说明 registry 格式、配置与 DATA 的独立覆盖规则、初始化和恢复。`tools/init_config.py` 保留已有配置；`tools/verify_config.py --json` 只检查本地配置。数据库仍由 `reminder.py init` 初始化，运行能力是否就绪要看 `health.readiness.ready`。

## 快速开始

```bash
cd skills/schedule-reminder/scripts
python reminder.py init
python reminder.py add --title "回复招聘" --due-at 2026-06-28T17:00:00Z --priority 1 \
       --source me --idempotency-key me:1
python reminder.py list --active
python reminder.py creation-preflight --title "Prepare Acme report" --source my-skill --idempotency-key my-skill:report-1
python reminder.py transition --id <ID> --to doing --progress 30
python reminder.py done --id <ID>
python reminder.py tick --now 2026-06-28T17:00:00Z   # 调度器每 5 分钟跑这个
```

## 如何触发

当用户想追踪 待办 / 事件 / 截止 / 进度 / 提醒 / 备忘，或另一个 skill 需要写提醒、读任务进度时触发。

## 示例输出

合成输出示例见 [examples.json](examples.json)，由 tools/make_fixtures.py 复现。

## 架构（三层）

```
SQLite (WAL) 单文件             <- 私有存储，下游绝不直接碰
  store.py (带类型函数)         <- 同进程；可信 skill 可 import
    reminder.py [--actor NAME] <verb>   <- 版本化 CLI 契约 (api_version 1.0.0)
    reminder_linked_items.py           <- 已审查的 Task Console 只读关联接口
[Windows 任务: PT5M 心跳]  -> reminder.py tick -> 对账到期项 -> Discord relay (出)
[Windows 任务: PT10M 入站] -> ingest_tick -> 轮询每个可读频道 -> 命中已注册命令则交给它的
                              处理器，否则交给 dispatch(LLM 判断)                  (入)
```

OS 任务只是心跳。`tick` 对账持久表，所以休眠/关机的机器下次运行时会一次性补发**所有**错过的提醒（幂等、至少
一次 + 去重），不为每个事件建 OS 触发器，不静默跳过。

- **契约**：[`skills/schedule-reminder/reference/contract.md`](skills/schedule-reminder/reference/contract.md)
- **部署**：[`skills/schedule-reminder/reference/deployment.md`](skills/schedule-reminder/reference/deployment.md)
- **集成（写给下游 skill）**：[`skills/schedule-reminder/reference/integration.md`](skills/schedule-reminder/reference/integration.md)
- 入站命令、工作单和提醒的恢复规则见 [operations.md](skills/schedule-reminder/reference/operations.md)。
- 新建前比较现有事项、关联后续要求及重试身份见[集成说明](skills/schedule-reminder/reference/integration.md)和[派发身份](docs/dispatch-identity.md)。
- 工作台操作见[手动完成](docs/manual-completion.md)和[任务关联审查](skills/schedule-reminder/reference/linkage-review.md)。
- 带业务事件身份的通知见[通知回执](skills/schedule-reminder/reference/notification-receipts.md)。

## 测试范围

15 个验收信号（E1-E15）全程经 subprocess 调冻结 CLI、断言 JSON：CRUD、完整状态转移表（合法 + 非法）、写入
不变量、到期触发 / 幂等 tick / 错过补发 / 重试退避、并发写 + `PRAGMA integrity_check`、并发读写、幂等去重、
API golden、未知字段保留、health、RRULE 滚动重复、per-alarm 提前量。外加 Agent Center 总线、relay 出口、
每日 digest、心跳存活、notify 路由及双向 ingest/dispatch 的隔离模块测试。E8/E9/E11/E12 为阻断合并的红线。

```bash
python -B -m pytest skills/schedule-reminder/tests/ -q -p no:cacheprovider
```

通知路由测试需要通过 `SCHEDULE_TEST_LANGUAGE_RULE` 指定现有的 `notification_language.py`。
测试只把该代码复制到合成配置中，不读取实际配置或凭据。通过 `SCHEDULE_TEST_TASK_CONSOLE_ROOT`
指定 Task Console 源码后，还会验证其编译器和工作接口对 reminder CLI 的调用；未指定时明确跳过这些检查。
这些测试不运行计划任务，也不证明安装后的就绪状态。

## 局限

- **建议 SQLite ≥ 3.51.3**。更早版本有 WAL-reset 多写者损坏 bug；`health` 会告警（不硬性失败）。无需换 Python
  的升级路径：`pip install pysqlite3-binary`（自动检测）。测试套件会验证本机 SQLite 在并发下 `integrity_check`
  保持 `ok`。
- **RRULE 按次滚动。** 提醒成功后，主记录移到下一个未来时间，不会一次建出无限多条记录。支持的字段见契约。
- **优先 Windows 部署**（`install.ps1` 计划任务）；目前未提供维护中的 Unix cron 安装方式。
- **数据库必须放本地 NTFS**，绝不放 OneDrive/GDrive/网络盘（WAL 锁 + 同步会损坏库）。
- **工作完成依赖 llmcall 的执行证据**。缺少进程约束、清理确认、模型家族身份或审查证据时，工作保留未解决状态。
  离线测试不能证明本机安装的 llmcall 已提供这些能力。

## 语言

中文 (`README_CN.md`) · English (`README.md`, 权威版)

## Roadmap · 贡献 · 许可

见 [ROADMAP.md](ROADMAP.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [LICENSE](LICENSE)(MIT)。

JSON 会始终输出，无需 --json。--db 和 --actor 是全局参数，必须放在子命令之前。

历史测试结果不代表当前版本已通过回归或完成安装。请以对应源码版本的执行报告为准。
