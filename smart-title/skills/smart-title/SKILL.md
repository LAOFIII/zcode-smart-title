---
name: smart-title
description: Use when a conversation mentions renaming or fixing ZCode session titles ("标题", "改名", "会话标题", "smart title"), when a `[smart-title]` hint appears in context asking you to call RenameSession, or when the user runs /smart-title to refresh or backfill titles. Covers title style rules, why the active session must use RenameSession while historical sessions need the backfill script, and how to preview and apply batch backfills safely.
---

# Smart Title

ZCode 只在第 0 轮生成一次标题，首句过短、超时或首句形似 URL 时会永久停在首句截断。本插件补上后续刷新与历史回填。

## 三种改名途径

| 对象 | 正确做法 | 原因 |
| --- | --- | --- |
| 当前会话 | 内置 `RenameSession` 工具 | 标题同时存在内存里，直接写库会在下一轮被覆盖，侧边栏也不会刷新 |
| 历史非活跃会话 | `scripts/backfill_titles.py apply-batch` | 不在内存中；`RenameSession` 对它们会拒绝 |
| 用户手动改过的会话 | 不要动 | 插件用 `state.json` 里的 `lastAutoTitle` 识别，一旦判定为用户改名就永久锁定 |

## 收到 `[smart-title]` 提示时

上下文里出现以 `[smart-title]` 开头的一句说明，是钩子在邀请你顺手整理标题：

1. 先完成用户这一轮真正的任务。
2. 任务完成后调用一次 `RenameSession`，`sessionId` 用提示里给出的 id。
3. 标题按下面的风格写。如果新标题与提示里的「当前标题」实质相同，就不要调用。
4. 不要在回复里专门解释这次改名，一句话都不用提。

## 标题风格

- 一个能概括整段对话**最终目标**的具体短语，不要照抄第一句。
- 中文 ≤ 16 字，英文 ≤ 6 个词；中英混排按中文长度控制。
- 不加引号、书名号、结尾句号；不加「标题：」「会话：」之类前缀；不用 Markdown、不用 emoji。
- 用名词短语，例如「夸克升级弹窗根因排查」而不是「排查了夸克弹窗」。

## 安全边界

- 不改动用户的 `title_overridden = 1` 状态；`backfill_titles.py` 也会跳过这类会话。
- 批量回填是写数据库的操作：**先给预览表并取得确认**，再执行；脚本会自动备份两个数据库并在 `~/.zcode/smart-title/backups/` 保留回滚清单。
- 配置与状态都在 `~/.zcode/smart-title/`（`config.json`、`state.json`），不要删除 `state.json`：删掉会丢失「哪些标题是自动改的」的记录，导致之后误判用户标题。
