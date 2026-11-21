---
description: 刷新当前会话标题，或查看状态/回填历史/锁定配置（now / status / backfill / lock / unlock / config）
argument-hint: [now|status|backfill [N]|lock|unlock|config]
allowed-tools: Bash, Read, RenameSession, LocalToolSearch, DeferredToolCall, ToolSearch
---

# /smart-title

用户参数：`$ARGUMENTS`

## 严格路由（先匹配第一词，再只执行该分支）

- **参数为空** 或 **`now`** → 只执行 **now**
- **`status`** → 只执行 **status**。**严禁调用 `RenameSession`！**
- **`backfill` [N]** → 只执行 **backfill**。**严禁顺便修改当前会话标题！**
- **`lock`** / **`unlock`** → 只执行 **lock / unlock**。**严禁调用 `RenameSession`！**
- **`config` ...** → 只执行 **config**。**严禁调用 `RenameSession`！**

## 定位 Python 与插件目录（脚本类分支共用）

执行任何脚本前，用下面片段定位可用的 Python 与插件目录（不要硬编码单一路径，优先使用已安装缓存路径）：

```bash
PY=""
for c in "py -3" py python3 python; do
  $c -c "import sqlite3,sys;raise SystemExit(0 if sys.version_info>=(3,8) else 1)" </dev/null >/dev/null 2>&1 && { PY="$c"; break; }
done
ROOT=$(PYTHONUTF8=1 $PY -c '
import glob, json, os
env_root = os.environ.get("ZCODE_PLUGIN_ROOT", "")
if env_root and os.path.isfile(os.path.join(env_root, "scripts", "smart_title_core.py")):
    print(env_root.replace("\\", "/")); raise SystemExit(0)
ip = os.path.expanduser("~/.zcode/cli/plugins/installed_plugins.json")
if os.path.isfile(ip):
    items = [x for x in json.load(open(ip, encoding="utf-8")).get("plugins", []) if x.get("name") == "smart-title"]
    for item in sorted(items, key=lambda x: x.get("updatedAt", ""), reverse=True):
        p = item.get("installPath", "")
        if os.path.isfile(os.path.join(p, "scripts", "smart_title_core.py")):
            print(p.replace("\\", "/")); raise SystemExit(0)
hits = sorted(glob.glob(os.path.expanduser("~/.zcode/cli/plugins/cache/*/smart-title/*")), reverse=True)
for h in hits + [os.path.expanduser("~/.zcode/plugins/smart-title")]:
    if os.path.isfile(os.path.join(h, "scripts", "smart_title_core.py")):
        print(h.replace("\\", "/")); raise SystemExit(0)
')
```

## 通用约定

- **当前会话**在 ZCode 内存里持有标题，只能通过内置 `RenameSession` 工具改名；直接写库会在下一轮被覆盖。若 `RenameSession` 是延迟工具，先通过 `LocalToolSearch`/`ToolSearch`（`query: "select:RenameSession"`）加载后再用 `DeferredToolCall`（或直接调用）执行。
- **历史非活跃会话**不在内存中，必须通过 `backfill_titles.py` 写库，`RenameSession` 对它们会拒绝。
- 标题风格：概括整段对话最终目标的名词短语；中文 ≤ 16 字、英文 ≤ 6 个词；不加引号、书名号、句号，不加「标题：」前缀，不用 Markdown。

## now（仅在参数为空或 `now` 时执行）

1. 取当前会话 id：`$ZCODE_SESSION_ID`（不存在时取 `$CLAUDE_SESSION_ID`）。
2. 根据你上下文里的对话主旨生成标题（遵守上面的标题风格）。
3. 若 `RenameSession` 未加载，先加载它；调用 `RenameSession` 传入 `sessionId` 与 `title`。若新标题与当前已知标题实质相同，则不调用并说明无需改动。
4. 一句话告知用户新标题。

## status（仅查状态，禁止改名）

```bash
PYTHONUTF8=1 $PY "$ROOT/scripts/smart_title_core.py" status
```

只把输出里的「标题 / 来源 / 用户轮次 / 状态 / 判定」简明汇报给用户，不要贴原始 JSON，**也不要调用 `RenameSession`**。

## backfill [N]

回填历史非活跃会话（默认 20 个，`N` 为数量上限）。**不要修改当前会话。**

1. 列出候选：

```bash
PYTHONUTF8=1 $PY "$ROOT/scripts/backfill_titles.py" list-candidates --limit 20 --json
```

2. 依据每个候选的 `transcript`（首句 + 最近几轮）生成标题，遵守上面的风格。
3. **先给用户展示预览表**（会话 id 简写 | 现有标题 | 建议标题），说明写前会自动快照双库并保留回滚清单。只对用户确认的条目写入。
4. 把确认后的条目写入临时 JSON（`[{"sessionId": "...", "title": "..."}]`），执行：

```bash
PYTHONUTF8=1 $PY "$ROOT/scripts/backfill_titles.py" apply-batch --file "<json路径>"
```

5. 汇报写入条数、跳过原因与备份目录。需要撤销时：

```bash
PYTHONUTF8=1 $PY "$ROOT/scripts/backfill_titles.py" list-backups
PYTHONUTF8=1 $PY "$ROOT/scripts/backfill_titles.py" revert --name "<备份名>"
```

## lock / unlock（仅改锁定状态，禁止改名）

```bash
PYTHONUTF8=1 $PY "$ROOT/scripts/smart_title_core.py" lock
PYTHONUTF8=1 $PY "$ROOT/scripts/smart_title_core.py" unlock
```

## config（仅查改配置，禁止改名）

```bash
PYTHONUTF8=1 $PY "$ROOT/scripts/smart_title_core.py" config
PYTHONUTF8=1 $PY "$ROOT/scripts/smart_title_core.py" config --set refreshMinTurns=3
```
