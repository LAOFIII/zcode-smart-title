# zcode-smart-title

ZCode 插件市场，内置插件 **Smart Title（智能标题）**：让会话标题跟着对话主题走，而不是永远停在你发的第一句话。

## 为什么需要它

ZCode 只在第 0 轮生成一次标题。首句太短（「你好」）、首句是 URL 或命令、或首轮生成超时，标题就会永久停在首句截断（`title_source = first_input`），之后聊到别的主题也不会更新。

## 安装

在 ZCode 里打开 **插件市场 → 添加 → 添加插件市场**，粘贴本仓库地址：

```
https://github.com/LAOFIII/zcode-smart-title
```

然后在 **个人 → laofeng-zcode-plugins → 智能标题** 点安装。装完新建一个任务即可生效（旧任务的技能/工具清单不会刷新）。

也可以直接用本地目录：把本仓库 clone 下来，在添加市场时粘贴该目录路径。

## 能力

- **多轮自动刷新**：对话推进 ≥ 2 轮、距上次刷新 ≥ 2 分钟后，按整段对话重新生成标题
- **首句救援**：卡在 `first_input` 的会话从第 2 轮起自动改写
- **手动改名保护**：你在侧边栏改过的标题永不被覆盖（插件记录自己改过哪些标题来区分）
- **范围隔离**：子智能体会话、分叉会话一律跳过；超过 100 轮冻结
- **`/smart-title` 命令**：立即刷新当前会话、查看状态、批量回填历史会话、锁定/解锁、改配置
- **零额外密钥**：复用当前会话的模型，不调用外部 API

## 命令

```
/smart-title                  立即按当前对话刷新标题
/smart-title status           查看标题来源、轮次、节流判定
/smart-title backfill [N]     预览并回填卡在首句截断的历史会话
/smart-title lock / unlock    锁定或恢复当前会话的自动刷新
/smart-title config           查看配置
```

## 目录

```
marketplace.json                     市场清单
smart-title/
  .zcode-plugin/plugin.json          插件清单
  hooks/hooks.json                   UserPromptSubmit + PostToolUse(RenameSession)
  hooks/run-hook.cmd                 cmd/sh 双语包装器（Windows 无 bash 时静默退出）
  hooks/smart-title-hook             解释器探测 + JSON 兜底
  scripts/smart_title_core.py        触发判定、改名归因、状态与配置
  scripts/backfill_titles.py         历史会话扫描、批量写库、备份与回滚
  commands/smart-title.md            /smart-title
  skills/smart-title/SKILL.md        标题风格与安全边界
  README.md                          详细说明
```

详见 [smart-title/README.md](smart-title/README.md)。

## 实现要点

ZCode 的活跃会话在内存里持有标题，下一轮会写回 SQLite，所以插件从不自己写活跃会话的标题，而是由 `UserPromptSubmit` 钩子注入一句提示，让当前模型调用内置 `RenameSession`；历史非活跃会话才由脚本直接写库，写前自动快照两个数据库并留回滚清单。

一个反直觉的细节：`RenameSession` 和用户手动改名写入的 `title_source` 都是 `custom`，桌面端索引也会把 `title_overridden` 置 1，二者无法区分。插件用 `state.json` 里的 `lastAutoTitle` 加上 `time_title_updated` 与上次注入提示的时间做归因，才能既尊重用户改名、又允许自己持续刷新。

## License

MIT
