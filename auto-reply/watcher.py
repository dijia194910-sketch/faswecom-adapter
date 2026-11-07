#!/usr/bin/env python
"""企业微信新消息监听器（直连版）。

链路：
    企微新消息 → faswecom 本地库 → 本监听器（秒级轮询）
    → responder：IMA 知识库检索 → 模型决策 → faswecom 发送 → 回执与入库核对

设计约束：
1. 消息级幂等：进入 processed 的消息永不再回复。
2. 发送成功以企微本地库出现带 AI 后缀的本人消息为准。
3. 单实例端口互斥，避免重复回复。
4. 只处理私聊、非本人、非占位消息；超过 max_message_age_seconds 的旧消息直接丢弃。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import socket
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import responder  # noqa: E402

STATE_VERSION = 3

# 适配器侧已确认落库的状态；本地聊天库可能还没同步，不能判成没发出去
SERVER_OK_STATES = {
    "SENT", "VERIFIED", "ACCEPTED", "DELIVERED", "SUCCESS", "OK", "SERVER_RECORDED",
    # 窗口/OCR 发送方式没有服务器回执，只确认"企微里已出现本人消息气泡"；
    # 不把它算成功会导致本地库同步稍慢时触发兜底补发，给客户发重复消息。
    "VISIBLE_OUTGOING",
}


# ---------------------------------------------------------------- 基础工具


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    value = json.loads(path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} 必须是 JSON 对象")
    return value


def recover_state(events: Path, epoch: str, upper: int) -> dict[str, Any]:
    """从事件日志找回最后处理到的消息编号，作为新断点。

    断电或异常关机可能把 live-state.json 写坏（实测会变成整文件全零），
    这时不能直接放弃运行或从 0 重扫全部历史，而应回到“最后处理过的那条消息”。
    """
    cursor = 0
    if events.exists():
        try:
            lines = events.read_text("utf-8").splitlines()[-2000:]
        except OSError:
            lines = []
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            ids = []
            if isinstance(event.get("message_ids"), list):
                ids.extend(event["message_ids"])
            if event.get("sent_message_id"):
                ids.append(event["sent_message_id"])
            for value in ids:
                try:
                    cursor = max(cursor, int(value))
                except (TypeError, ValueError):
                    continue
    if cursor <= 0:
        cursor = upper
    return initial_state(epoch, cursor)


def load_state(path: Path, events: Path, epoch: str, upper: int) -> dict[str, Any]:
    """读取运行状态；文件损坏时用事件日志恢复，保证服务仍能起来。"""
    if path.exists():
        try:
            value = json.loads(path.read_text("utf-8"))
            if isinstance(value, dict):
                return value
        except (OSError, ValueError):
            pass
        append_jsonl(
            events,
            {"at": now_iso(), "result": "state_corrupt",
             "note": "live-state.json 无法解析（可能断电写坏），已按事件日志恢复断点"},
        )
    return recover_state(events, epoch, upper)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with open(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()


def connect_ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def db_mark(path: Path) -> tuple[str, int]:
    with closing(connect_ro(path)) as db:
        epoch = db.execute("SELECT epoch FROM local_change_meta WHERE id=1").fetchone()[0]
        upper = db.execute(
            "SELECT coalesce(max(id),0) FROM messages WHERE platform='wecom'"
        ).fetchone()[0]
    return str(epoch), int(upper)


def initial_state(epoch: str, cursor: int) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "epoch": epoch,
        "cursor": cursor,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "processed": [],
        "pending": {},
        "ledger": [],
    }


# ---------------------------------------------------------------- 消息筛选


def valid_inbound(row: sqlite3.Row, suffix: str) -> bool:
    if int(row["is_self"]) != 0:
        return False
    content = str(row["content"] or "").strip()
    if not content or content.startswith("[企业微信"):
        return False
    return not (suffix and content.rstrip().endswith(suffix))


def in_scope(config: dict[str, Any], row: sqlite3.Row) -> bool:
    cid = str(row["conversation_id"])
    name = str(row["conversation"] or "")
    if cid in set(config.get("blocked_conversations") or []):
        return False
    if name in set(config.get("blocked_names") or []):
        return False
    if re.fullmatch(r"[A-Z]:\d+", name):
        return False
    whitelist = config.get("allowed_conversations") or {}
    if whitelist:
        return cid in whitelist
    if config.get("scope") != "direct_all":
        return False
    return str(row["conversation_type"] or "") == "direct" or ":S:" in cid


def collect(config: dict[str, Any], state: dict[str, Any]) -> int:
    db_path = Path(config["db_path"])
    epoch, upper = db_mark(db_path)
    if epoch != state.get("epoch"):
        state.clear()
        state.update(initial_state(epoch, upper))
        return 0

    cursor = int(state.get("cursor") or 0)
    if upper <= cursor:
        return 0

    suffix = str(config.get("ai_suffix", "（AI回复）"))
    processed = set(int(x) for x in state.get("processed") or [])
    added = 0
    with closing(connect_ro(db_path)) as db:
        rows = db.execute(
            """
            SELECT id,conversation_id,conversation,sender_id,sender,timestamp,
                   content,is_self,conversation_type
            FROM messages
            WHERE platform='wecom' AND id>? AND id<=?
            ORDER BY id LIMIT 500
            """,
            (cursor, upper),
        ).fetchall()

    for row in rows:
        message_id = int(row["id"])
        state["cursor"] = max(int(state.get("cursor") or 0), message_id)
        if message_id in processed:
            continue
        if not in_scope(config, row) or not valid_inbound(row, suffix):
            continue
        conversation_id = str(row["conversation_id"])
        pending = state["pending"].setdefault(
            conversation_id,
            {
                "conversation": str(row["conversation"]),
                "first_seen": time.time(),
                "last_seen": time.time(),
                "attempts": 0,
                "messages": [],
            },
        )
        if any(int(item["id"]) == message_id for item in pending["messages"]):
            continue
        pending["last_seen"] = time.time()
        pending["messages"].append(
            {
                "id": message_id,
                "sender": str(row["sender"]),
                "timestamp": int(row["timestamp"]),
                "content": str(row["content"]),
            }
        )
        pending["messages"] = pending["messages"][-10:]
        added += 1
    state["updated_at"] = now_iso()
    return added


def verify_sent(
    db_path: Path, conversation_id: str, before_upper: int, suffix: str
) -> tuple[bool, int | None]:
    for attempt in range(3):
        with closing(connect_ro(db_path)) as db:
            row = db.execute(
                """
                SELECT id FROM messages
                WHERE platform='wecom' AND conversation_id=? AND is_self=1
                  AND id>? AND content LIKE ?
                ORDER BY id DESC LIMIT 1
                """,
                (conversation_id, before_upper, f"%{suffix}%"),
            ).fetchone()
        if row:
            return True, int(row["id"])
        time.sleep(2)
    return False, None


# ---------------------------------------------------------------- 处理


def process_ready(
    config: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    workspace: Path,
    events: Path,
) -> bool:
    mode = str(config.get("mode", "dry_run"))
    suffix = str(config.get("ai_suffix", "（AI回复）"))
    debounce = float(config.get("debounce_seconds", 5)) + random.uniform(0.0, 2.5)  # 去抖也带随机性
    max_age = float(config.get("max_message_age_seconds", 1800))
    did_work = False

    for conversation_id, pending in list(state["pending"].items()):
        now = time.time()
        if pending.get("blocked"):
            continue
        if config.get("force_process") is not True and now - float(pending["last_seen"]) < debounce:
            continue
        if now < float(pending.get("next_retry", 0)):
            continue

        did_work = True
        ids = [int(m["id"]) for m in pending["messages"]]
        event: dict[str, Any] = {
            "at": now_iso(),
            "mode": mode,
            "conversation_id": conversation_id,
            "conversation": pending["conversation"],
            "message_ids": ids,
        }

        newest_ms = max(int(m["timestamp"]) for m in pending["messages"])
        if max_age > 0 and now - newest_ms / 1000.0 > max_age:
            event["result"] = "stale_skipped"
            event["age_seconds"] = int(now - newest_ms / 1000.0)
            state["processed"].extend(ids)
            state["pending"].pop(conversation_id, None)
            append_jsonl(events, event)
            continue

        # 人工客服本人回话：不走客户逻辑，改为给对应客户回「已转达」
        try:
            import handoff  # noqa: PLC0415

            if handoff.is_agent_conversation(config, conversation_id):
                info = handoff.agent_ack(config, workspace, pending["messages"])
                event.update(result="agent_reply", handoff=info)
                state["processed"].extend(ids)
                state["pending"].pop(conversation_id, None)
                append_jsonl(events, event)
                continue
        except Exception as exc:  # noqa: BLE001
            event.update(result="agent_reply_error", error=f"{type(exc).__name__}: {exc}"[:200])
            state["processed"].extend(ids)
            state["pending"].pop(conversation_id, None)
            append_jsonl(events, event)
            continue

        if mode == "preview":
            event["messages"] = pending["messages"]
            event["result"] = "preview_only"
            state["processed"].extend(ids)
            state["pending"].pop(conversation_id, None)
            append_jsonl(events, event)
            continue

        _, before_upper = db_mark(Path(config["db_path"]))
        try:
            result = responder.handle(
                config, workspace, pending["conversation"], conversation_id, pending["messages"]
            )
        except responder.McpError as exc:
            pending["attempts"] = int(pending.get("attempts", 0)) + 1
            text = str(exc)
            limited = "每小时上限" in text or "rate" in text.lower() and "limit" in text.lower()
            event.update(
                result="rate_limited" if limited else "infra_error",
                error=text[:300],
                attempts=pending["attempts"],
            )
            # 无法安全定位接收人时不要重试，直接把情况转给人工核实并说明原因。
            if any(marker in text for marker in ("同一显示名", "重名", "会话指纹")):
                reason = ("重名：发现多个客户使用同一显示名，桌面发送无法安全区分，"
                          "需人工核实后回复该客户" if ("重名" in text or "同一显示名" in text)
                          else "会话校验未通过：无法确认打开的正是该客户会话，需人工核实后回复")
                try:
                    import handoff  # noqa: PLC0415
                    handoff.notify(config, workspace, pending["conversation"],
                                   conversation_id, pending["messages"], reason)
                    event["result"] = "handoff_manual_verify"
                    event["handoff_reason"] = reason
                except Exception as handoff_exc:  # noqa: BLE001
                    event["result"] = "handoff_error"
                    event["handoff_error"] = f"{type(handoff_exc).__name__}: {handoff_exc}"[:160]
                state["processed"].extend(ids)
                state["pending"].pop(conversation_id, None)
                append_jsonl(events, event)
                continue
            if limited:
                # 适配器有每小时发送上限，重试只会继续撞墙，直接放行并把工单留档
                state["processed"].extend(ids)
                state["pending"].pop(conversation_id, None)
                append_jsonl(events, event)
                continue
            if pending["attempts"] >= int(config.get("max_attempts", 2)):
                state["processed"].extend(ids)
                state["pending"].pop(conversation_id, None)
                event["result"] = "blocked_infra_error"
            else:
                pending["next_retry"] = time.time() + float(
                    config.get("retry_delay_seconds", 30)
                )
            append_jsonl(events, event)
            continue
        except Exception as exc:  # noqa: BLE001
            pending["attempts"] = int(pending.get("attempts", 0)) + 1
            event.update(
                result="exception", error=f"{type(exc).__name__}: {exc}"[:300],
                attempts=pending["attempts"],
            )
            if pending["attempts"] >= int(config.get("max_attempts", 2)):
                state["processed"].extend(ids)
                state["pending"].pop(conversation_id, None)
                event["result"] = "blocked_exception"
            else:
                pending["next_retry"] = time.time() + float(
                    config.get("retry_delay_seconds", 30)
                )
            append_jsonl(events, event)
            continue

        decision = result.get("decision")
        send_state = str(result.get("send_state") or "NOT_SENT")
        sent_message_id = None
        sendable = decision == "AUTO" or bool(result.get("send_notice"))
        if mode == "live" and sendable:
            ok, sent_message_id = verify_sent(
                Path(config["db_path"]), conversation_id, before_upper, suffix
            )
            if ok:
                send_state = "VERIFIED"
            elif str(result.get("send_state") or "") in SERVER_OK_STATES:
                # 适配器已落库，本地聊天库还没同步——不能算失败
                send_state = "VERIFIED"
            else:
                send_state = "NOT_VERIFIED"
                # 本地库同步有延迟，等两秒再核对一次
                time.sleep(2.0)
                ok2, mid2 = verify_sent(
                    Path(config["db_path"]), conversation_id, before_upper, suffix
                )
                if ok2:
                    send_state = "VERIFIED"
                    sent_message_id = mid2
                else:
                    # 兜底补发：适配器偶发把请求丢进 PENDING，客户不该静默
                    try:
                        rid = str(result.get("recipient_id") or "")
                        text_out = str(result.get("reply_text") or "")
                        last = pending["messages"][-1]
                        if rid and text_out:
                            retry = responder.send_reply(
                                responder.faswecom_client(config),
                                rid,
                                text_out,
                                int(last["id"]),
                                int(last["timestamp"]),
                            )
                            if str(retry.get("state") or "") in SERVER_OK_STATES:
                                send_state = "VERIFIED_RESEND"
                                result["dispatch_id"] = retry.get("dispatch_id") or ""
                    except Exception:  # noqa: BLE001
                        pass

        event.update(
            result="processed",
            decision=decision,
            send_state=send_state,
            dispatch_id=result.get("dispatch_id") or "",
            sent_message_id=sent_message_id,
            confidence=result.get("confidence"),
            matched_question=result.get("matched_question"),
            reason=result.get("reason"),
            reply_text=str(result.get("reply_text") or "")[:2000],
        )
        state["processed"].extend(ids)
        state["pending"].pop(conversation_id, None)
        state["ledger"].append(
            {
                "at": now_iso(),
                "ids": ids,
                "conversation": pending["conversation"],
                "decision": decision,
                "send_state": send_state,
                "dispatch_id": result.get("dispatch_id") or "",
                "sent_message_id": sent_message_id,
                "reply_text": str(result.get("reply_text") or "")[:2000],
                "reason": result.get("reason"),
            }
        )
        if decision != "AUTO" or send_state not in ("VERIFIED", "DRY_RUN"):
            append_jsonl(workspace / "review-queue.jsonl", event)
        append_jsonl(events, event)

    state["processed"] = state["processed"][-3000:]
    state["ledger"] = state["ledger"][-500:]
    state["updated_at"] = now_iso()
    atomic_json(state_path, state)
    return did_work


def ai_takeover(config: dict[str, Any]) -> bool:
    """适配器操作台的“智能客服接管”总开关，直接读适配器配置。

    不通过 MCP 读：MCP 连接默认没有 get_status 权限；配置文件就在
    db_path 同级目录，读不到时按接管处理（保持原行为）。
    """
    path = Path(config.get("db_path", "")).resolve().parent / "wecom-adapter.json"
    try:
        cfg = json.loads(path.read_text("utf-8"))
        return cfg.get("ai_takeover") is not False
    except (OSError, ValueError):
        return True


def skip_pending(state: dict[str, Any], events: Path) -> None:
    """智能客服接管关闭时：消息只标记跳过，不回复，留给人工。"""
    for conversation_id, pending in list(state["pending"].items()):
        ids = [int(m["id"]) for m in pending["messages"]]
        state["processed"].extend(ids)
        state["pending"].pop(conversation_id, None)
        append_jsonl(
            events,
            {"at": now_iso(), "result": "ai_disabled",
             "conversation": pending["conversation"], "message_ids": ids,
             "note": "智能客服接管已关闭，未自动回复"},
        )


def acquire_single_instance(port: int) -> socket.socket:
    handle = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        handle.bind(("127.0.0.1", port))
        handle.listen(1)
    except OSError:
        handle.close()
        raise SystemExit(f"端口 {port} 已被占用：已有监听器在运行")
    return handle


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--force-process", action="store_true")
    parser.add_argument("--reset-baseline", action="store_true")
    args = parser.parse_args()

    config = load_json(args.config, {})
    if not config:
        raise SystemExit(f"配置为空：{args.config}")
    if config.get("mode") not in ("preview", "dry_run", "live"):
        raise SystemExit("mode 必须是 preview、dry_run 或 live")
    if args.force_process:
        config["force_process"] = True

    db_path = Path(config["db_path"])
    if not db_path.is_file():
        raise SystemExit(f"未找到企业微信消息库：{db_path}")
    workspace = Path(config.get("workspace", args.config.parent)).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    lock = acquire_single_instance(int(config.get("single_instance_port", 45917)))

    state_path = workspace / "live-state.json"
    events = workspace / "events.jsonl"
    epoch, upper = db_mark(db_path)
    state = load_state(state_path, events, epoch, upper)
    if state.get("version") != STATE_VERSION or state.get("epoch") != epoch or args.reset_baseline:
        state = initial_state(epoch, upper)
        atomic_json(state_path, state)
        append_jsonl(events, {"at": now_iso(), "result": "baseline_reset", "cursor": upper})
        if args.once:
            lock.close()
            return

    remind_scan_at = 0.0
    takeover_check_at = 0.0
    takeover = True
    while True:
        try:
            if time.time() - takeover_check_at >= 5:
                takeover_check_at = time.time()
                fresh = ai_takeover(config)
                if fresh != takeover:
                    append_jsonl(events, {"at": now_iso(), "result": "takeover",
                                          "enabled": fresh,
                                          "note": "智能客服接管" + ("已打开" if fresh else "已关闭，消息留给人工")})
                    takeover = fresh
            collect(config, state)
            atomic_json(state_path, state)
            if takeover:
                process_ready(config, state, state_path, workspace, events)
            else:
                skip_pending(state, events)

            # 人工未接手时定时催办，默认每 10 分钟一次（由 handoff.reminder_interval_seconds 决定）
            if time.time() - remind_scan_at >= 60:
                remind_scan_at = time.time()
                try:
                    import handoff  # noqa: PLC0415

                    for rec in handoff.remind_pending(config, workspace):
                        append_jsonl(events, {"at": now_iso(), "result": "remind", **rec})
                except Exception as exc:  # noqa: BLE001
                    append_jsonl(
                        events,
                        {
                            "at": now_iso(),
                            "result": "remind_error",
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
        except KeyboardInterrupt:
            lock.close()
            return
        except Exception as exc:  # noqa: BLE001
            append_jsonl(
                events,
                {"at": now_iso(), "result": "watcher_error", "error": f"{type(exc).__name__}: {exc}"},
            )
        if args.once:
            lock.close()
            return
        time.sleep(float(config.get("poll_seconds", 1)))


if __name__ == "__main__":
    main()
