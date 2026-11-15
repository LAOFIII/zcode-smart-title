---
description: 刷新当前会话标题，或回填卡在首句截断的历史会话（now / status / backfill / lock / unlock）
argument-hint: [now|status|backfill [N]|lock|unlock|config]
allowed-tools: Bash, Read, RenameSession
---

# /smart-title

管理 ZCode 会话标题。用户参数：`$ARGUMENTS`（为空时等同于 `now`）。

脚本位置：先取 `${ZCODE_PLUGIN_ROOT}`，若为空或不存在则用 `~/.zcode/plugins/smart-title`。
Python 解释器优先 `py -3`，否则 `python3`。所有脚本都强制 UTF-8。

## 通用约定

- **当前这个会话**在 ZCode 内存里持有标题，只能通过内置 `RenameSession` 工具改名；直接改数据库会在下一轮被覆盖。
- **历史非活跃会话**只能通过 `backfill_titles.py` 写库，`RenameSession` 对它们会报错。
- 标题风格：能概括整段对话最终目标的具体短语；中文 ≤ 16 字、英文 ≤ 6 个词；不加引号、书名号、句号，不加「标题：」前缀，不用 Markdown。

## now（默认）

1. 取当前会话 id：`$ZCODE_SESSION_ID`（不存在时用 `echo "$CLAUDE_SESSION_ID"`）。
2. 读取对话主旨（你自己的上下文即可，不需要脚本回放）。
3. 调用 `RenameSession`，`sessionId` 用第 1 步的值，`title` 用上面风格生成的标题。
4. 一句话告知用户新标题。如果新标题与现有标题实质相同，就不要调用工具，直接说明无需改动。

## status

运行：

```bash
ROOT="${ZCODE_PLUGIN_ROOT}"; [ -d "$ROOT" ] || ROOT="$HOME/.zcode/plugins/smart-title"
py -3 "$ROOT/scripts/smart_title_core.py" status
```

把输出里的「标题 / 来源 / 轮次 / 状态 / 判定」简明汇报给用户，不要贴原始 JSON。

## backfill [N]

回填历史会话（默认 20 个，`N` 为数量上限）。

1. 列出候选：

```bash
py -3 "$ROOT/scripts/backfill_titles.py" list-candidates --limit N --json
```

2. 依据每个候选的 `transcript`（首句 + 最近几轮）为每个会话生成一个标题，遵守上面的风格。
3. **先给用户一张预览表**（会话 id 简写 | 现有标题 | 建议标题），并说明将要写入两个数据库、写入前会自动备份。只对用户确认过的条目执行。
4. 把确认后的条目写成 JSON 文件（`[{"sessionId": "...", "title": "..."}]`），执行：

```bash
py -3 "$ROOT/scripts/backfill_titles.py" apply-batch --file <json路径>
```

5. 汇报写入条数、跳过原因、备份目录。需要撤销时用 `revert`：

```bash
py -3 "$ROOT/scripts/backfill_titles.py" list-backups
py -3 "$ROOT/scripts/backfill_titles.py" revert --name <备份名>
```

注意：`apply-batch` 会跳过当前活跃会话（提示改用 RenameSession）与 `title_overridden = 1` 的会话，这是预期行为，不要绕过。

## lock / unlock

用户希望某个会话不再被自动改名时：`lock`；恢复自动刷新：`unlock`。二者都作用于当前会话：

```bash
py -3 "$ROOT/scripts/smart_title_core.py" lock
py -3 "$ROOT/scripts/smart_title_core.py" unlock
```

## config

查看或修改配置（`refreshMinIntervalMs`、`refreshMinTurns`、`refreshMaxTurns`、`targetCjkCharacters`、`targetWords`、`enabled` 等）：

```bash
py -3 "$ROOT/scripts/smart_title_core.py" config
py -3 "$ROOT/scripts/smart_title_core.py" config --set refreshMinTurns=3
```
