# Smart Title（ZCode 插件）

ZCode 只会在第 0 轮生成一次会话标题：首句含空白后不足 10 字符（例如「你好」）、首句是 URL/命令、或首轮生成超时，标题就会永久停在首句截断（`title_source = first_input`）。对话主题后来演进了，侧边栏也不会再更新。

本插件参考 [`weibaohui/dsh-smart-title`](https://github.com/weibaohui/dsh-smart-title) 的思路，为 ZCode 补上：

- **多轮自动刷新**：对话推进到一定轮次后刷新标题
- **手动改名保护**：你在左侧栏改过的标题永不被覆盖
- **范围隔离**：子智能体会话、分叉会话、桌面端标记 `title_overridden` 的会话一律跳过
- **`/smart-title` 命令**：立即刷新当前会话、查看状态、批量回填历史会话
- **零额外密钥**：复用当前会话的模型，不调用外部 API

## 为什么活跃会话必须走 RenameSession

ZCode 的活跃会话在内存里持有 `title`，每轮结束会写回 SQLite。外部脚本直接改库会在下一轮被内存旧值覆盖，侧边栏也不会实时刷新；而 `RenameSession` 会同步内存、`db.sqlite`、`tasks-index.sqlite` 并推送 `session_title_updated` 事件。

所以插件从不自己写活跃会话的标题，而是由钩子注入一句提示，让当前模型顺手调用 `RenameSession`。历史非活跃会话不在内存中，才由 `backfill_titles.py` 直接写库。

## 工作机制

| 环节 | 实现 |
| --- | --- |
| 触发判定 | `UserPromptSubmit` 钩子 → `hooks/smart-title-hook check-hook` |
| 改名落地 | 模型调用内置 `RenameSession` |
| 记录自动标题 | `PostToolUse`（matcher `^RenameSession$`）→ `record-rename` |
| 历史回填 | `/smart-title backfill` → `scripts/backfill_titles.py` |

`RenameSession` 写入的 `title_source` 是 `custom`，与用户手动改名完全相同，无法据此区分。插件把「自己改出来的标题」记在 `state.json` 的 `lastAutoTitle`，再用 `time_title_updated` 与上次注入提示的时间做归因；两者都不匹配时才判定为用户改名并永久锁定。

## 节流与保护规则

| 配置项 | 默认 | 含义 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `refreshMinIntervalMs` | `120000` | 距上次刷新至少 2 分钟 |
| `refreshMinTurns` | `2` | 至少新增 2 轮用户对话 |
| `refreshMaxTurns` | `100` | 超过 100 轮冻结（首句截断的救援除外） |
| `rescueAfterTurns` | `2` | 第 2 轮起仍卡在 `first_input` 就救援 |
| `hintCooldownMs` | `180000` | 两次注入提示的最小间隔 |
| `attributionWindowMs` | `900000` | 提示后多久内的改名算作插件自己的 |
| `targetCjkCharacters` / `targetWords` | `16` / `6` | 标题长度上限 |
| `excludeForks` | `true` | 跳过分叉会话 |

配置与状态文件都在 `~/.zcode/smart-title/`：

- `config.json` — 用户配置（`/smart-title config --set refreshMinTurns=3` 可改）
- `state.json` — 每个会话的自动标题、提示时间、锁定状态
- `backups/<时间戳>/` — 回填前自动备份的双库快照与 `manifest.json` 回滚清单

**不要删除 `state.json`**：它记录着哪些标题是插件改的，删掉会导致插件把现有标题误判成用户改名。

## 命令

```
/smart-title                  立即按当前对话刷新标题
/smart-title status           查看标题来源、轮次、节流判定
/smart-title backfill [N]     预览并回填卡在首句截断的历史会话
/smart-title lock / unlock    锁定或恢复当前会话的自动刷新
/smart-title config           查看配置
```

## 手动执行

```bash
ROOT=~/.zcode/plugins/smart-title
py -3 "$ROOT/scripts/smart_title_core.py" status
py -3 "$ROOT/scripts/backfill_titles.py" list-candidates
py -3 "$ROOT/scripts/backfill_titles.py" list-backups
py -3 "$ROOT/scripts/backfill_titles.py" revert --name 20261115-102932
```

## 钩子的容错约定

钩子在任何异常下都输出 `{}` 并 `exit 0`：没有 Python、数据库被锁、载荷格式不对，都只会让这一轮静默跳过，不会阻塞对话。Python 解释器按 `py -3` → `py` → `python3` → `python` 顺序探测，并要求 ≥ 3.8 且带 `sqlite3`；Windows 上由 `hooks/run-hook.cmd` 直接调用 Python（无需绕经 Git Bash，仅作兜底），macOS/Linux 自动走同一文件底部的 POSIX `sh` 分支。

## 已知边界

- 自动刷新依赖模型配合调用 `RenameSession`：注入提示的那一轮会多一次轻量工具调用。
- 只在 `UserPromptSubmit` 判定，不在 `Stop` 判定，避免打断回复流程。
- 回填只改历史非活跃会话；当前打开的会话请用 `/smart-title`。
