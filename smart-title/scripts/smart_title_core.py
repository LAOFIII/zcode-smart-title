#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""smart-title core: hook helpers, status and config for the ZCode smart-title plugin.

Subcommands
-----------
  check-hook                    UserPromptSubmit hook: decide whether to hint a rename
  record-rename                 PostToolUse hook (RenameSession): remember auto titles
  status   [--session-id ID]    print the current title state and refresh gates
  config   [--set k=v ...]      show or update settings
  lock     [--session-id ID]    pin a session: never auto-rename it again
  unlock   [--session-id ID]    drop the pin
  forget   [--session-id ID]    drop all bookkeeping for one session

Design notes
------------
* Hooks must never block a turn: every hook path swallows errors, prints a JSON
  object and exits 0.
* The plugin never writes the session tables itself. The active session's title
  lives in memory inside ZCode, so it has to be renamed through the built-in
  RenameSession tool. Only historical (non-active) sessions are written by
  backfill_titles.py.
* RenameSession writes title_source='custom', the same value a manual UI rename
  produces. Auto titles are therefore tracked in state.json and compared
  against the live DB row instead of relying on title_source alone.
"""

import json
import os
import re
import sqlite3
import sys
import time
import traceback
from urllib.parse import quote

STATE_VERSION = 1


def _bootstrap_encoding():
    """Windows defaults to the ANSI code page, which would mangle Chinese hook
    payloads and additionalContext. Force UTF-8 on all three standard streams."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_bootstrap_encoding()

DEFAULT_CONFIG = {
    "enabled": True,
    "targetCjkCharacters": 16,
    "targetWords": 6,
    "refreshMinIntervalMs": 120000,
    "refreshMinTurns": 2,
    "refreshMaxTurns": 100,
    "rescueAfterTurns": 2,
    "hintCooldownMs": 180000,
    "attributionWindowMs": 900000,
    "excludeForks": True,
}


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
def home():
    return os.path.expanduser("~")


def data_dir():
    override = os.environ.get("SMART_TITLE_DATA_DIR")
    if override:
        return os.path.abspath(override)
    return os.path.join(home(), ".zcode", "smart-title")


def ensure_data_dir():
    path = data_dir()
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def cli_db_path():
    return os.environ.get("SMART_TITLE_CLI_DB") or os.path.join(
        home(), ".zcode", "cli", "db", "db.sqlite"
    )


def tasks_db_path():
    return os.environ.get("SMART_TITLE_TASKS_DB") or os.path.join(
        home(), ".zcode", "v2", "tasks-index.sqlite"
    )


def state_path():
    return os.path.join(data_dir(), "state.json")


def config_path():
    return os.path.join(data_dir(), "config.json")


# --------------------------------------------------------------------------- #
# json helpers
# --------------------------------------------------------------------------- #
def read_stdin_json():
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def load_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else fallback
    except Exception:
        return fallback


def save_json(path, value):
    """Atomic-ish write: temp file then replace, so a crashed hook cannot
    leave a half-written state.json behind."""
    ensure_data_dir()
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    try:
        os.replace(temp, path)
    except OSError:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        try:
            os.remove(temp)
        except OSError:
            pass


def load_config():
    merged = dict(DEFAULT_CONFIG)
    stored = load_json(config_path(), {})
    if isinstance(stored.get("config"), dict):
        stored = stored["config"]
    for key, value in stored.items():
        if key in DEFAULT_CONFIG:
            merged[key] = value
    return merged


def save_config(config):
    save_json(config_path(), {"config": config})


def load_state():
    state = load_json(state_path(), {})
    if not isinstance(state.get("sessions"), dict):
        state = {"version": STATE_VERSION, "sessions": {}}
    state["version"] = STATE_VERSION
    return state


def session_entry(state, session_id):
    sessions = state.setdefault("sessions", {})
    entry = sessions.get(session_id)
    if not isinstance(entry, dict):
        entry = {}
        sessions[session_id] = entry
    return entry


# --------------------------------------------------------------------------- #
# sqlite helpers (read-only, short timeout: a hook must stay cheap)
# --------------------------------------------------------------------------- #
def ro_connect(path, timeout=1.5):
    if not os.path.exists(path):
        return None
    absolute = os.path.abspath(path).replace("\\", "/")
    if not absolute.startswith("/"):
        absolute = "/" + absolute
    uri = "file:%s?mode=ro" % quote(absolute, safe="/:@-._~")
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=timeout)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def rw_connect(path, timeout=10.0):
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(path, timeout=timeout)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def fetch_session_row(session_id):
    conn = ro_connect(cli_db_path())
    if conn is None:
        return None
    try:
        return conn.execute(
            "SELECT id, parent_id, task_type, title, title_source,"
            " time_title_updated, time_created, time_updated"
            " FROM session WHERE id = ?",
            (session_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def fetch_tasks_row(session_id):
    conn = ro_connect(tasks_db_path())
    if conn is None:
        return None
    try:
        return conn.execute(
            "SELECT task_id, title, title_overridden, pinned,"
            " forked_from_task_id, cli_title, cli_title_source"
            " FROM tasks WHERE task_id = ?",
            (session_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def count_promoted_inputs(session_id):
    conn = ro_connect(cli_db_path())
    if conn is None:
        return 0
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM session_input"
                " WHERE session_id = ? AND kind = 'sendText' AND status = 'promoted'",
                (session_id,),
            ).fetchone()[0]
        )
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def count_real_user_messages(session_id):
    conn = ro_connect(cli_db_path())
    if conn is None:
        return 0
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM message"
                " WHERE session_id = ?"
                " AND json_extract(data, '$.role') = 'user'"
                " AND json_extract(data, '$.synthetic') IS NULL",
                (session_id,),
            ).fetchone()[0]
        )
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def turn_count(session_id):
    """Real user turns. session_input is authoritative; older sessions may
    predate it, hence the message-table fallback."""
    promoted = count_promoted_inputs(session_id)
    if promoted > 0:
        return promoted
    return count_real_user_messages(session_id)


# --------------------------------------------------------------------------- #
# small utilities
# --------------------------------------------------------------------------- #
CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
TRAILING_PUNCT_RE = re.compile(r"[。．.！!？?；;：:，,、~～…\s]+$")
QUOTE_PAIRS = (
    ('"', '"'),
    ("'", "'"),
    ("“", "”"),
    ("‘", "’"),
    ("「", "」"),
    ("『", "』"),
    ("《", "》"),
    ("〈", "〉"),
    ("（", "）"),
    ("【", "】"),
    ("(", ")"),
    ("[", "]"),
)


def normalize_title(value):
    """Case/whitespace-insensitive comparison key (dsh-smart-title parity)."""
    text = "" if value is None else str(value)
    return re.sub(r"\s+", " ", text).strip().lower()


def clean_title(value, config=None):
    """Strip quotes/markdown emphasis and trailing punctuation from an
    LLM-produced title, then clamp it to the configured length."""
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"^(?:title|标题)\s*[:：]\s*", "", text, flags=re.IGNORECASE)
    text = text.strip("`*_#> ")
    # Only unwrap balanced pairs: a title may legitimately end with 「）」.
    for _ in range(2):
        text = text.strip()
        for left, right in QUOTE_PAIRS:
            if len(text) >= 2 and text.startswith(left) and text.endswith(right):
                text = text[1:-1].strip()
                break
    text = text.strip("\"'")
    text = TRAILING_PUNCT_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    config = config or load_config()
    limit = int(config.get("targetCjkCharacters") or 16)
    words = int(config.get("targetWords") or 6)
    if CJK_RE.search(text):
        limit = max(4, limit)
        if len(text) > limit:
            text = text[:limit].rstrip(" 　·、,，。.：:")
    else:
        parts = text.split(" ")
        if len(parts) > max(1, words):
            text = " ".join(parts[: max(1, words)])
    return text.strip()


def is_urlish(text):
    stripped = normalize_title(text)
    if not stripped:
        return True
    if stripped.startswith("/"):
        return True
    if re.match(r"^https?://", stripped):
        return True
    if re.match(r"^[a-z]:[\\/]", stripped):
        return True
    return False


def now_ms():
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# gates
# --------------------------------------------------------------------------- #
def evaluate(session_id, config, state, entry, row, tasks_row):
    """Return (should_hint, reason, context) for one session."""
    if not config.get("enabled", True):
        return False, "disabled", {}

    if row is None:
        return False, "no_session_row", {}

    if (row["task_type"] or "") != "interactive":
        return False, "not_interactive", {}

    if row["parent_id"]:
        return False, "subagent_session", {}

    if tasks_row is not None:
        if config.get("excludeForks", True) and tasks_row["forked_from_task_id"]:
            return False, "forked_session", {}

    now = now_ms()
    turns = turn_count(session_id)
    title = row["title"] or ""
    source = (row["title_source"] or "").lower()
    # The desktop index flips title_overridden to 1 for *any* custom title,
    # including an agent-driven RenameSession, so it only tells us "custom",
    # never "the user did it". Ownership is decided by lastAutoTitle below.
    if tasks_row is not None and tasks_row["title_overridden"] in (1, "1", True):
        source = "custom"
    updated = int(row["time_title_updated"] or 0)
    last_auto = entry.get("lastAutoTitle") or ""
    last_hint_at = int(entry.get("lastHintAt") or 0)
    last_refresh_at = int(entry.get("lastRefreshAt") or 0)
    last_turns = int(entry.get("lastTurnCount") or 0)

    if entry.get("locked") or entry.get("autoDisabled"):
        return False, "locked", {}

    # --- manual rename protection ------------------------------------------
    # RenameSession sets title_source='custom' exactly like a UI rename does,
    # so an auto title is only recognised through lastAutoTitle. When that
    # record is missing (PostToolUse hook never fired) the row's own timestamp
    # is compared against the last hint we injected.
    if source == "custom":
        window = int(config.get("attributionWindowMs") or 900000)
        attributed = bool(last_hint_at) and (
            last_hint_at - 5000 <= updated <= last_hint_at + window
        )
        if not last_auto:
            if attributed:
                entry["lastAutoTitle"] = title
                entry["lastRefreshAt"] = max(last_refresh_at, updated or now)
                entry["lastTurnCount"] = turns
                last_auto = title
            else:
                entry["locked"] = True
                entry["lockReason"] = "custom_title_before_plugin"
                entry["lockedAt"] = now
                return False, "custom_title_before_plugin", {}
        elif normalize_title(title) != normalize_title(last_auto):
            if attributed:
                # Our own rename whose PostToolUse record never arrived.
                entry["lastAutoTitle"] = title
                entry["lastRefreshAt"] = max(last_refresh_at, updated or now)
                entry["lastTurnCount"] = turns
            else:
                entry["locked"] = True
                entry["lockReason"] = "user_renamed"
                entry["lockedAt"] = now
                return False, "user_renamed", {}

    # --- long-conversation freeze -------------------------------------------
    max_turns = int(config.get("refreshMaxTurns") or 100)
    if max_turns > 0 and turns > max_turns and source != "first_input":
        return False, "frozen_after_max_turns", {}

    # --- hint cooldown ------------------------------------------------------
    cooldown = int(config.get("hintCooldownMs") or 180000)
    if last_hint_at and now - last_hint_at < cooldown:
        return False, "hint_cooldown", {}

    rescue_after = int(config.get("rescueAfterTurns") or 2)

    # --- rescue: still stuck on the truncated first line --------------------
    if source == "first_input" or not title:
        if turns < rescue_after:
            return False, "waiting_native_title", {}
        return True, "rescue_first_input", {
            "turns": turns,
            "title": title,
            "source": source,
        }

    # --- periodic refresh ---------------------------------------------------
    min_turns = max(1, int(config.get("refreshMinTurns") or 2))
    if turns - last_turns < min_turns:
        return False, "turn_gate", {"turns": turns, "lastTurns": last_turns}
    interval = int(config.get("refreshMinIntervalMs") or 120000)
    if last_refresh_at and now - last_refresh_at < interval:
        return False, "interval_gate", {}
    if is_urlish(title) and turns >= rescue_after:
        return True, "url_like_title", {"turns": turns, "title": title, "source": source}
    return True, "periodic", {"turns": turns, "title": title, "source": source}


def build_hint(session_id, config, context):
    cjk = int(config.get("targetCjkCharacters") or 16)
    words = int(config.get("targetWords") or 6)
    current = (context.get("title") or "").strip()
    if len(current) > 28:
        current = current[:27].rstrip() + "…"
    reason = context.get("reason") or ""
    parts = [
        "[smart-title] 本轮正常完成任务后，请额外调用一次 RenameSession 工具"
        '（若为延迟工具请先通过 LocalToolSearch/ToolSearch 加载，sessionId="%s"），'
        "把本会话标题改写为能概括整段对话最终目标的具体短语："
        "中文 ≤ %d 字、英文 ≤ %d 个词，不加引号/书名号/句号，不加「标题：」之类前缀，"
        "不使用 Markdown。" % (session_id, cjk, words)
    ]
    if current:
        parts.append("当前标题为「%s」；若新标题与它实质相同就不要调用。" % current)
    if reason == "rescue_first_input":
        parts.append("现有标题只是首句截断，请改写成真正的会话主题。")
    parts.append("这是轻量整理动作，无需向用户解释。")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# hook: check
# --------------------------------------------------------------------------- #
def cmd_check_hook():
    payload = read_stdin_json()
    session_id = (
        payload.get("session_id")
        or os.environ.get("ZCODE_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
        or ""
    )
    if not session_id:
        return {}

    config = load_config()
    state = load_state()
    entry = session_entry(state, session_id)

    row = fetch_session_row(session_id)
    tasks_row = fetch_tasks_row(session_id)

    before = json.dumps(entry, sort_keys=True)
    should, reason, context = evaluate(session_id, config, state, entry, row, tasks_row)
    # evaluate() may lock or adopt a title; persist only when it did.
    mutated = json.dumps(entry, sort_keys=True) != before

    if not should:
        if mutated:
            save_json(state_path(), state)
        return {}

    context["reason"] = reason
    now = now_ms()
    entry["lastHintAt"] = now
    entry["lastTurnCount"] = context.get("turns") or entry.get("lastTurnCount") or 0
    entry["hintCount"] = int(entry.get("hintCount") or 0) + 1
    entry["lastHintReason"] = reason
    save_json(state_path(), state)

    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": build_hint(session_id, config, context),
        }
    }


# --------------------------------------------------------------------------- #
# hook: record an auto rename
# --------------------------------------------------------------------------- #
SUCCESS_STATES = ("applied", "accepted", "queued", "ok", "success")


def _rename_succeeded(tool_response):
    if tool_response is None:
        return True
    if isinstance(tool_response, str):
        lowered = tool_response.lower()
        if '"status":"rejected"' in lowered.replace(" ", ""):
            return False
        return "error" not in lowered and "rejected" not in lowered
    if isinstance(tool_response, dict):
        status = str(tool_response.get("status") or "").lower()
        if status:
            return status in SUCCESS_STATES
        if tool_response.get("error"):
            return False
    return True


def cmd_record_rename():
    payload = read_stdin_json()
    tool_name = payload.get("tool_name") or ""
    if tool_name and tool_name != "RenameSession":
        return {}

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return {}

    target = str(tool_input.get("sessionId") or payload.get("session_id") or "")
    title = tool_input.get("title")
    if not target or not isinstance(title, str):
        return {}
    if not _rename_succeeded(payload.get("tool_response")):
        return {}

    config = load_config()
    state = load_state()
    entry = session_entry(state, target)
    now = now_ms()
    last_hint_at = int(entry.get("lastHintAt") or 0)
    window = int(config.get("attributionWindowMs") or 900000)

    if last_hint_at and now <= last_hint_at + window:
        entry["lastAutoTitle"] = title
        entry["lastAutoTitleNormalized"] = normalize_title(title)
        entry["lastRefreshAt"] = now
        entry["lastTurnCount"] = turn_count(target)
        entry["pendingAttribution"] = False
    else:
        # A rename we never asked for: treat it as a deliberate user title.
        entry["locked"] = True
        entry["lockReason"] = "user_requested_rename"
        entry["lockedAt"] = now
        entry["userTitle"] = title

    save_json(state_path(), state)
    return {}


# --------------------------------------------------------------------------- #
# CLI commands
# --------------------------------------------------------------------------- #
def resolve_session_id(args):
    for index, arg in enumerate(args):
        if arg == "--session-id" and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--session-id="):
            return arg.split("=", 1)[1]
    return (
        os.environ.get("SMART_TITLE_SESSION_ID")
        or os.environ.get("ZCODE_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
        or ""
    )


def cmd_status(args):
    session_id = resolve_session_id(args)
    as_json = "--json" in args
    if not session_id:
        print("缺少会话 id：请传入 --session-id 或设置 ZCODE_SESSION_ID")
        return 1

    config = load_config()
    state = load_state()
    entry = state.get("sessions", {}).get(session_id, {}) or {}
    row = fetch_session_row(session_id)
    tasks_row = fetch_tasks_row(session_id)

    if row is None:
        print("未在 %s 中找到会话 %s" % (cli_db_path(), session_id))
        return 1

    turns = turn_count(session_id)
    now = now_ms()
    should, reason, _context = evaluate(
        session_id, config, state, dict(entry), row, tasks_row
    )

    # Re-evaluate on a copy so status does not mutate persisted state.
    last_hint_at = int(entry.get("lastHintAt") or 0)
    last_refresh_at = int(entry.get("lastRefreshAt") or 0)
    cooldown = int(config.get("hintCooldownMs") or 180000)
    interval = int(config.get("refreshMinIntervalMs") or 120000)

    payload = {
        "sessionId": session_id,
        "title": row["title"],
        "titleSource": row["title_source"],
        "taskType": row["task_type"],
        "parentId": row["parent_id"],
        "forkedFrom": tasks_row["forked_from_task_id"] if tasks_row else None,
        "titleOverridden": tasks_row["title_overridden"] if tasks_row else None,
        "turns": turns,
        "locked": bool(entry.get("locked")),
        "lockReason": entry.get("lockReason"),
        "lastAutoTitle": entry.get("lastAutoTitle"),
        "lastAutoTitleIsCurrent": bool(
            entry.get("lastAutoTitle")
            and normalize_title(entry["lastAutoTitle"]) == normalize_title(row["title"])
        ),
        "hintCount": int(entry.get("hintCount") or 0),
        "lastHintAgoMs": (now - last_hint_at) if last_hint_at else None,
        "lastRefreshAgoMs": (now - last_refresh_at) if last_refresh_at else None,
        "nextHintEligibleInMs": max(0, cooldown - (now - last_hint_at))
        if last_hint_at
        else 0,
        "nextRefreshEligibleInMs": max(0, interval - (now - last_refresh_at))
        if last_refresh_at
        else 0,
        "shouldHintNow": should,
        "gate": reason,
        "config": config,
        "dataDir": data_dir(),
    }

    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    source_label = {
        "first_input": "首句截断（原生未生成）",
        "generated": "原生 LLM 生成",
        "custom": "custom（手动改名或插件改名）",
    }.get(str(row["title_source"] or ""), str(row["title_source"]))

    gate_label = {
        "disabled": "插件已关闭",
        "not_interactive": "非交互会话，跳过",
        "subagent_session": "子智能体会话，跳过",
        "forked_session": "分叉会话，跳过",
        "title_overridden": "桌面端已标记标题被覆盖，跳过",
        "locked": "已锁定：%s" % (entry.get("lockReason") or ""),
        "user_renamed": "已锁定：用户手动改过标题",
        "custom_title_before_plugin": "已锁定：插件安装前已有自定义标题",
        "frozen_after_max_turns": "已冻结：超过 %s 轮" % config.get("refreshMaxTurns"),
        "hint_cooldown": "冷却中：距上次提示不足 %ss" % (cooldown // 1000),
        "turn_gate": "轮次不足：需新增 ≥ %s 轮" % config.get("refreshMinTurns"),
        "interval_gate": "间隔不足：需距上次刷新 ≥ %ss" % (interval // 1000),
        "waiting_native_title": "等待原生标题生成（第 %s 轮才救援）"
        % config.get("rescueAfterTurns"),
        "rescue_first_input": "将救援首句截断的标题",
        "url_like_title": "标题形似 URL/命令，将改写",
        "periodic": "将按周期刷新",
        "no_session_row": "数据库中还没有该会话",
    }.get(reason, reason)

    print("会话    %s" % session_id)
    print("标题    %s" % (row["title"] or "(空)"))
    print("来源    %s" % source_label)
    print("用户轮次 %d" % turns)
    print("自动标题 %s" % (entry.get("lastAutoTitle") or "(无记录)"))
    print("状态    %s" % ("锁定" if entry.get("locked") else "可自动刷新"))
    print("判定    %s" % gate_label)
    print("提示次数 %d" % int(entry.get("hintCount") or 0))
    if payload["nextHintEligibleInMs"]:
        print("下次可提示 %.0fs 后" % (payload["nextHintEligibleInMs"] / 1000.0))
    if payload["nextRefreshEligibleInMs"]:
        print("下次可刷新 %.0fs 后" % (payload["nextRefreshEligibleInMs"] / 1000.0))
    print("数据目录 %s" % data_dir())
    return 0


def cmd_config(args):
    config = load_config()
    changed = False
    sets = []
    for index, arg in enumerate(args):
        if arg == "--set" and index + 1 < len(args):
            sets.append(args[index + 1])
        elif arg.startswith("--set="):
            sets.append(arg.split("=", 1)[1])
    for pair in sets:
        if "=" not in pair:
            continue
        key, raw = pair.split("=", 1)
        key = key.strip()
        if key not in DEFAULT_CONFIG:
            print("未知配置项：%s" % key)
            continue
        default = DEFAULT_CONFIG[key]
        try:
            if isinstance(default, bool):
                value = raw.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(default, int):
                value = int(raw)
            else:
                value = raw
        except ValueError:
            print("配置值无效：%s=%s" % (key, raw))
            continue
        config[key] = value
        changed = True
    if changed:
        save_config(config)
    print(json.dumps(config, ensure_ascii=False, indent=2))
    return 0


def cmd_lock(args, locked):
    session_id = resolve_session_id(args)
    if not session_id:
        print("缺少会话 id：请传入 --session-id 或设置 ZCODE_SESSION_ID")
        return 1
    state = load_state()
    entry = session_entry(state, session_id)
    entry["locked"] = bool(locked)
    if locked:
        entry["lockReason"] = "manual_lock"
        entry["lockedAt"] = now_ms()
    else:
        entry.pop("lockReason", None)
        entry.pop("lockedAt", None)
    save_json(state_path(), state)
    print("%s %s" % ("已锁定" if locked else "已解锁", session_id))
    return 0


def cmd_forget(args):
    session_id = resolve_session_id(args)
    if not session_id:
        print("缺少会话 id：请传入 --session-id 或设置 ZCODE_SESSION_ID")
        return 1
    state = load_state()
    state.get("sessions", {}).pop(session_id, None)
    save_json(state_path(), state)
    print("已清除记录 %s" % session_id)
    return 0


HOOK_COMMANDS = ("check-hook", "record-rename")


def main(argv):
    command = argv[1] if len(argv) > 1 else "status"
    args = argv[2:]
    try:
        if command == "check-hook":
            print(json.dumps(cmd_check_hook(), ensure_ascii=False))
            return 0
        if command == "record-rename":
            print(json.dumps(cmd_record_rename(), ensure_ascii=False))
            return 0
        if command == "status":
            return cmd_status(args)
        if command == "config":
            return cmd_config(args)
        if command == "lock":
            return cmd_lock(args, True)
        if command == "unlock":
            return cmd_lock(args, False)
        if command == "forget":
            return cmd_forget(args)
        print(__doc__)
        return 2
    except SystemExit:
        raise
    except BaseException:
        if command in HOOK_COMMANDS:
            print("{}")
            return 0
        print(
            json.dumps(
                {"ok": False, "error": traceback.format_exc(limit=4)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
