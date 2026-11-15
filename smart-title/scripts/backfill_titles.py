#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Historical session backfill for the ZCode smart-title plugin.

Only *inactive* sessions are written here. The session that is currently open
inside ZCode keeps its title in memory and must be renamed through the built-in
RenameSession tool (see the /smart-title command), otherwise the runtime's next
save would overwrite the row.

Subcommands
-----------
  list-candidates [--limit N] [--all] [--json]
      Show interactive sessions still stuck on the truncated first line,
      together with a compact transcript for title generation.

  apply-batch --file <json> | --stdin
      Write generated titles. Payload: [{"sessionId": "...", "title": "..."}].
      Both databases are backed up first and a revert manifest is written.

  list-backups
  revert --name <backup-name>
      Undo one apply-batch using its recorded previous titles.
"""

import json
import os
import shutil
import sqlite3
import sys
import time
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smart_title_core import (  # noqa: E402
    CJK_RE,
    clean_title,
    cli_db_path,
    data_dir,
    ensure_data_dir,
    load_config,
    normalize_title,
    ro_connect,
    rw_connect,
    tasks_db_path,
)

FIRST_TEXT = 400
MID_TEXT = 220
LAST_USER_TURNS = 4
LAST_ASSISTANT_TEXT = 300


# --------------------------------------------------------------------------- #
# transcript reading
# --------------------------------------------------------------------------- #
def message_ids(conn, session_id):
    try:
        rows = conn.execute(
            "SELECT id, json_extract(data, '$.role') AS role"
            " FROM message WHERE session_id = ?"
            " AND json_extract(data, '$.synthetic') IS NULL"
            " ORDER BY sequence",
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [(row["id"], row["role"] or "") for row in rows]


def message_text(conn, message_id):
    try:
        rows = conn.execute(
            "SELECT data FROM part WHERE message_id = ? ORDER BY sequence",
            (message_id,),
        ).fetchall()
    except sqlite3.Error:
        return ""
    chunks = []
    for row in rows:
        try:
            part = json.loads(row["data"])
        except Exception:
            continue
        if part.get("type") != "text":
            continue
        text = (part.get("text") or "").strip()
        if text:
            chunks.append(text)
    return "\n".join(chunks).strip()


def clip(text, limit):
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def build_transcript(conn, session_id):
    timeline = message_ids(conn, session_id)
    users = [mid for mid, role in timeline if role == "user"]
    assistants = [mid for mid, role in timeline if role == "assistant"]

    transcript = []
    if users:
        transcript.append(
            {"role": "user", "seq": 0, "text": clip(message_text(conn, users[0]), FIRST_TEXT)}
        )
    tail_users = users[1:][-LAST_USER_TURNS:] if len(users) > 1 else []
    for mid in tail_users:
        transcript.append({"role": "user", "text": clip(message_text(conn, mid), MID_TEXT)})
    for mid in assistants[-2:]:
        transcript.append(
            {
                "role": "assistant",
                "text": clip(message_text(conn, mid), LAST_ASSISTANT_TEXT),
            }
        )
    return transcript, len(users)


# --------------------------------------------------------------------------- #
# candidates
# --------------------------------------------------------------------------- #
def active_session_id():
    return (
        os.environ.get("SMART_TITLE_SESSION_ID")
        or os.environ.get("ZCODE_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
        or ""
    )


def collect_candidates(limit=50, include_all=False):
    conn = ro_connect(cli_db_path(), timeout=3.0)
    if conn is None:
        return [], "无法打开 %s" % cli_db_path()
    try:
        where = [
            "task_type = 'interactive'",
            "parent_id IS NULL",
            "time_deleted IS NULL",
        ]
        if not include_all:
            where.append("title_source = 'first_input'")
        sql = (
            "SELECT id, title, title_source, time_title_updated, time_created, time_updated"
            " FROM session WHERE " + " AND ".join(where) + " ORDER BY time_updated DESC LIMIT ?"
        )
        rows = conn.execute(sql, (limit,)).fetchall()
        current = active_session_id()
        candidates = []
        for row in rows:
            if row["id"] == current:
                continue
            transcript, turns = build_transcript(conn, row["id"])
            candidates.append(
                {
                    "sessionId": row["id"],
                    "currentTitle": row["title"] or "",
                    "titleSource": row["title_source"],
                    "turns": turns,
                    "updatedAt": row["time_updated"],
                    "updatedAtText": time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime((row["time_updated"] or 0) / 1000.0)
                    ),
                    "transcript": transcript,
                }
            )
        return candidates, None
    except sqlite3.Error as error:
        return [], "查询失败：%s" % error
    finally:
        conn.close()


def cmd_list_candidates(args):
    limit = 50
    include_all = "--all" in args
    as_json = "--json" in args
    for index, arg in enumerate(args):
        if arg == "--limit" and index + 1 < len(args):
            try:
                limit = int(args[index + 1])
            except ValueError:
                pass
        elif arg.startswith("--limit="):
            try:
                limit = int(arg.split("=", 1)[1])
            except ValueError:
                pass

    candidates, error = collect_candidates(limit=limit, include_all=include_all)
    if error:
        print(error, file=sys.stderr)
        return 1

    if as_json:
        print(json.dumps(candidates, ensure_ascii=False, indent=2))
        return 0

    if not candidates:
        print("没有需要回填的会话。")
        return 0

    print("共 %d 个候选会话（title_source = %s）：\n"
          % (len(candidates), "任意" if include_all else "first_input"))
    for index, item in enumerate(candidates, 1):
        print("[%d] %s" % (index, item["sessionId"]))
        print("    现有标题 %s" % clip(item["currentTitle"], 60))
        print("    轮次 %d · 最后活动 %s" % (item["turns"], item["updatedAtText"]))
        first = next(
            (part["text"] for part in item["transcript"] if part["role"] == "user"), ""
        )
        print("    首句 %s" % clip(first, 100))
        print("")
    return 0


# --------------------------------------------------------------------------- #
# backups
# --------------------------------------------------------------------------- #
def backup_dir():
    return os.path.join(ensure_data_dir(), "backups")


def snapshot(source_path, target_path):
    """Use SQLite's online backup API so WAL content is included."""
    source = sqlite3.connect(source_path, timeout=10.0)
    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def create_backup():
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = os.path.join(backup_dir(), stamp)
    os.makedirs(folder, exist_ok=True)
    copied = []
    for label, path in (("db", cli_db_path()), ("tasks", tasks_db_path())):
        if os.path.exists(path):
            target = os.path.join(folder, "%s.sqlite" % label)
            try:
                snapshot(path, target)
                copied.append(target)
            except sqlite3.Error:
                shutil.copy2(path, target)
                copied.append(target)
    return folder, copied


def prune_backups(keep=5):
    root = backup_dir()
    if not os.path.isdir(root):
        return
    entries = sorted(
        (name for name in os.listdir(root) if os.path.isdir(os.path.join(root, name))),
        reverse=True,
    )
    for name in entries[keep:]:
        shutil.rmtree(os.path.join(root, name), ignore_errors=True)


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #
def read_payload(args):
    path = None
    for index, arg in enumerate(args):
        if arg == "--file" and index + 1 < len(args):
            path = args[index + 1]
        elif arg.startswith("--file="):
            path = arg.split("=", 1)[1]
    if path:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    raw = sys.stdin.read()
    return json.loads(raw)


def cmd_apply_batch(args):
    try:
        payload = read_payload(args)
    except Exception as error:
        print("无法读取标题数据：%s" % error, file=sys.stderr)
        return 1
    if isinstance(payload, dict):
        payload = payload.get("titles") or payload.get("items") or []
    if not isinstance(payload, list) or not payload:
        print("标题数据为空。", file=sys.stderr)
        return 1

    config = load_config()
    current = active_session_id()
    cli_conn = rw_connect(cli_db_path())
    tasks_conn = rw_connect(tasks_db_path())

    plan = []
    skipped = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        session_id = str(item.get("sessionId") or item.get("session_id") or "")
        title = clean_title(item.get("title"), config)
        if not session_id or not title:
            skipped.append({"sessionId": session_id, "reason": "empty_title"})
            continue
        if session_id == current:
            skipped.append({"sessionId": session_id, "reason": "active_session_use_rename_tool"})
            continue
        plan.append({"sessionId": session_id, "title": title})

    if not plan:
        print(json.dumps({"applied": 0, "skipped": skipped}, ensure_ascii=False, indent=2))
        return 1 if skipped else 0

    folder, _files = create_backup()
    manifest_path = os.path.join(folder, "manifest.json")
    applied = []
    now = int(time.time() * 1000)

    try:
        for item in plan:
            session_id = item["sessionId"]
            title = item["title"]
            previous = {"sessionId": session_id, "oldTitle": None, "newTitle": title}

            if cli_conn is not None:
                row = cli_conn.execute(
                    "SELECT title, title_source FROM session WHERE id = ?", (session_id,)
                ).fetchone()
                if row is None:
                    skipped.append({"sessionId": session_id, "reason": "not_found"})
                    continue
                previous["oldTitle"] = row["title"]
                previous["oldTitleSource"] = row["title_source"]
                if normalize_title(row["title"]) == normalize_title(title):
                    skipped.append({"sessionId": session_id, "reason": "unchanged"})
                    continue
                cli_conn.execute(
                    "UPDATE session SET title = ?, title_source = 'generated',"
                    " time_title_updated = ? WHERE id = ?",
                    (title, now, session_id),
                )
                cli_conn.commit()

            if tasks_conn is not None:
                tasks_conn.execute(
                    "UPDATE tasks SET title = ?, cli_title = ?,"
                    " cli_title_source = 'generated'"
                    " WHERE task_id = ? AND COALESCE(title_overridden, 0) = 0",
                    (title, title, session_id),
                )
                tasks_conn.commit()

            applied.append(previous)
    finally:
        for conn in (cli_conn, tasks_conn):
            if conn is not None:
                conn.close()

    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(applied, handle, ensure_ascii=False, indent=2)

    prune_backups()
    print(
        json.dumps(
            {
                "applied": len(applied),
                "skipped": skipped,
                "backup": folder,
                "titles": applied,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_list_backups():
    root = backup_dir()
    if not os.path.isdir(root):
        print("尚无备份（%s）" % root)
        return 0
    names = sorted(
        (name for name in os.listdir(root) if os.path.isdir(os.path.join(root, name))),
        reverse=True,
    )
    if not names:
        print("尚无备份（%s）" % root)
        return 0
    for name in names:
        folder = os.path.join(root, name)
        manifest = os.path.join(folder, "manifest.json")
        count = 0
        if os.path.exists(manifest):
            try:
                with open(manifest, "r", encoding="utf-8") as handle:
                    count = len(json.load(handle))
            except Exception:
                count = 0
        print("%s  (%d 条改动)" % (name, count))
    return 0


def cmd_revert(args):
    name = None
    for index, arg in enumerate(args):
        if arg == "--name" and index + 1 < len(args):
            name = args[index + 1]
        elif arg.startswith("--name="):
            name = arg.split("=", 1)[1]
    root = backup_dir()
    if not name:
        entries = sorted(
            (entry for entry in os.listdir(root) if os.path.isdir(os.path.join(root, entry)))
        ) if os.path.isdir(root) else []
        if not entries:
            print("没有可回滚的备份。", file=sys.stderr)
            return 1
        name = entries[-1]
    folder = os.path.join(root, name)
    manifest_path = os.path.join(folder, "manifest.json")
    if not os.path.exists(manifest_path):
        print("备份 %s 缺少 manifest.json" % folder, file=sys.stderr)
        return 1
    with open(manifest_path, "r", encoding="utf-8") as handle:
        applied = json.load(handle)

    cli_conn = rw_connect(cli_db_path())
    tasks_conn = rw_connect(tasks_db_path())
    restored = 0
    try:
        for item in applied:
            old = item.get("oldTitle")
            if old is None:
                continue
            session_id = item["sessionId"]
            source = item.get("oldTitleSource") or "generated"
            if cli_conn is not None:
                cli_conn.execute(
                    "UPDATE session SET title = ?, title_source = ? WHERE id = ?",
                    (old, source, session_id),
                )
                cli_conn.commit()
            if tasks_conn is not None:
                tasks_conn.execute(
                    "UPDATE tasks SET title = ?, cli_title = ?, cli_title_source = ?"
                    " WHERE task_id = ?",
                    (old, old, source, session_id),
                )
                tasks_conn.commit()
            restored += 1
    finally:
        for conn in (cli_conn, tasks_conn):
            if conn is not None:
                conn.close()
    print("已回滚 %d 条标题（来源备份 %s）" % (restored, name))
    return 0


# --------------------------------------------------------------------------- #
def main(argv):
    command = argv[1] if len(argv) > 1 else "list-candidates"
    args = argv[2:]
    if command == "list-candidates":
        return cmd_list_candidates(args)
    if command == "apply-batch":
        return cmd_apply_batch(args)
    if command == "list-backups":
        return cmd_list_backups()
    if command == "revert":
        return cmd_revert(args)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
