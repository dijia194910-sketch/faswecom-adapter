#!/usr/bin/env python
"""转人工通知：把客户信息与最近对话推送给指定的人工客服。

触发点：responder 判定 ESCALATE 时（客户主动要人工、红线、知识库与技能都答不了）。
同一位客户再次催办时会发【催办】，但受间隔与次数限制，避免刷屏。
推送失败不影响对客户的回复，通知全文落到 handoff-inbox 供人工查看。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

REASON_LABEL = {
    "客户明确要求人工": "客户主动要人工",
    "命中高风险词，转人工": "命中红线词（投诉/退款/合同等）",
}

# 适配器不同阶段的中 | 成功终态
SENT_STATES = {"SENT", "VERIFIED", "ACCEPTED", "DELIVERED", "SUCCESS", "OK", "SERVER_RECORDED",
              "VISIBLE_OUTGOING"}
PENDING_STATES = {"PENDING", "QUEUED", "SENDING", "DISPATCHED", "SERVER_ACCEPTED"}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


RATE_LIMIT_HINTS = ("每小时上限", "每分钟最多", "过于频繁", "请求过于频繁")


def _rate_limited(value: Any) -> bool:
    """识别适配器的限流提示，避免继续重试把额度烧光。"""
    text = str(value or "")
    return any(hint in text for hint in RATE_LIMIT_HINTS)


def _trim(text: str, limit: int = 60) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def contact_of(db_path: Path, conversation_id: str) -> dict[str, str]:
    """从本地库取客户显示名与企微外部联系人 ID。读不到就返回空，不阻塞。"""
    info = {"name": "", "sender_id": ""}
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as conn:
            row = conn.execute(
                "SELECT conversation, sender_id FROM messages "
                "WHERE platform='wecom' AND conversation_id=? AND is_self=0 "
                "ORDER BY id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        if row:
            info["name"] = str(row[0] or "")
            info["sender_id"] = str(row[1] or "")
    except (sqlite3.Error, OSError, ValueError):
        pass
    return info


def transcript(db_path: Path, conversation_id: str, limit: int) -> list[str]:
    """最近 N 条对话。直接读本地库并按 id 倒序取，避免 MCP 返回的是最早几条。"""
    rows: list[tuple] = []
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as conn:
            rows = conn.execute(
                "SELECT timestamp, is_self, content FROM messages "
                "WHERE platform='wecom' AND conversation_id=? "
                "ORDER BY id DESC LIMIT ?",
                (conversation_id, int(limit)),
            ).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        rows = []

    lines: list[str] = []
    for stamp, is_self, content in reversed(rows):
        text = str(content or "").strip()
        if not text:
            continue
        if text.startswith("[企业微信非文本消息") or text.startswith("[企业微信消息格式尚未支持"):
            text = "[图片/文件等非文本消息]"
        when = ""
        if isinstance(stamp, (int, float)) and stamp:
            when = datetime.fromtimestamp(stamp / 1000).strftime("%m-%d %H:%M")
        who = "客服" if is_self else "客户"
        lines.append(f"{when} {who}：{_trim(text)}".strip())
    return lines


def compose(
    name: str,
    sender_id: str,
    conversation: str,
    reason: str,
    lines: list[str],
    urge: bool = False,
    summary: str = "",
) -> str:
    label = REASON_LABEL.get(reason, reason or "智能客服无法处理，需要人工介入")
    who = conversation or name or "未知"
    head: list[str] = []
    if urge:
        head.append(f"【催办】{who} 再次催促，尚未接入")
        head.append("")
        head.append("客户已在转人工队列中，请尽快跟进")
        head.append("")
    else:
        head.append(f"【转人工】{who} 需要人工跟进")
    head.append(f"客户：{who}")
    head.append(f"触发原因：{label}")
    head.append(f"时间：{_now()}")
    if summary:
        head.append(f"客户问题：{summary}")
    elif lines:
        # 摘要生成失败时只带最近 1~2 条客户消息，绝不整段转发聊天记录
        recent = [line for line in lines if "客户：" in line][-2:]
        brief = "；".join(
            line.split("客户：", 1)[-1].strip().rstrip("…")[:40] for line in recent
        ) if recent else ""
        head.append(f"客户问题：{brief or '见该客户最近会话'}")
        head.append("（摘要生成失败，请在企业微信中查看完整会话）")
    else:
        head.append("客户问题：读取失败，请在企业微信中直接查看该客户会话")

    tail = (
        "客户正在等着你哦，收到请回复任意内容即可，"
        "我帮您转发给客户，说明您已经接手"
    )
    # 提示语必须留在最后一条，正文按 1900 字上限裁剪
    body = chr(10).join(head)
    limit = 1900 - len(tail) - 2
    if len(body) > limit:
        body = body[:limit] + "…"
    return f"{body}{chr(10)}{chr(10)}{tail}"


def notify(
    config: dict[str, Any],
    workspace: Path,
    conversation: str,
    conversation_id: str,
    messages: list[dict[str, Any]],
    reason: str,
) -> dict[str, Any]:
    """推送转人工信息。返回 {state, recipient, dispatch_id, saved?, urge?}。

    state 落在 SENT_STATES 内表示已送达；其他均视为未送达，全文会落盘。
    """
    block = config.get("handoff") or {}
    if not block.get("enabled", False):
        return {}

    agent_pid = str(block.get("agent_recipient_id") or "").strip()
    agent_name = str(block.get("agent_name") or "").strip()
    if not agent_pid and not agent_name:
        _log(workspace, {"at": _now(), "state": "SKIPPED", "reason": "未配置人工客服"})
        return {}

    trigger_id = int(messages[-1].get("id") or 0) if messages else 0
    plan = _plan(workspace, conversation_id, block, trigger_id)
    if plan == "skip":
        _log(
            workspace,
            {
                "at": _now(),
                "conversation": conversation,
                "state": "SKIPPED",
                "reason": "重复或过于频繁的转人工通知，已跳过",
            },
        )
        return {}

    import responder  # noqa: PLC0415  延迟导入避免循环

    client = responder.faswecom_client(config)
    db_path = Path(str(config.get("db_path", "")))
    info = contact_of(db_path, conversation_id)
    limit = int(block.get("history_limit", 20))
    lines = transcript(db_path, conversation_id, limit)
    summary = ""
    try:
        summary = responder.summarize_conversation(config, lines)
    except Exception:  # noqa: BLE001
        summary = ""
    text = compose(
        info.get("name", ""),
        info.get("sender_id", ""),
        conversation,
        reason,
        lines,
        urge=(plan == "urge"),
        summary=summary,
    )

    if not agent_pid:
        data = client.tool("search_wecom_recipients", {"query": agent_name})
        candidates = [
            c for c in (data or {}).get("candidates") or [] if str(c.get("kind")) == "private"
        ]
        exact = [c for c in candidates if str(c.get("name")) == agent_name]
        pick = exact[0] if len(exact) == 1 else (candidates[0] if len(candidates) == 1 else None)
        if not pick:
            saved = _save_local(workspace, conversation, text)
            _log(
                workspace,
                {
                    "at": _now(),
                    "conversation": conversation,
                    "state": "FAILED",
                    "reason": f"人工客服「{agent_name}」无法唯一识别（候选 {len(candidates)} 个）",
                    "saved": saved,
                },
            )
            return {"state": "FAILED", "saved": saved}
        agent_pid = str(pick.get("id"))

    if str(config.get("mode", "dry_run")) != "live":
        _log(
            workspace,
            {
                "at": _now(),
                "conversation": conversation,
                "state": "DRY_RUN",
                "urge": plan == "urge",
            },
        )
        return {"state": "DRY_RUN", "recipient": agent_pid, "urge": plan == "urge"}

    base_key = re.sub(r"[^0-9A-Za-z_-]+", "-", f"handoff-{conversation_id}-{trigger_id}")[:110]
    retries = max(0, int(block.get("retries", 2)))
    wait = float(block.get("wait_seconds", 8))

    dispatch_id = ""
    state = "NOT_SENT"
    limited = False
    for attempt in range(retries + 1):
        key = base_key if attempt == 0 else f"{base_key}-r{attempt}"
        try:
            receipt = client.tool(
                "send_wecom_message",
                {"recipient_id": agent_pid, "text": text, "idempotency_key": key},
            )
        except Exception as exc:  # noqa: BLE001
            receipt = {}
            if _rate_limited(exc):
                limited = True
                break
        dispatch_id = str((receipt or {}).get("dispatch_id") or "")
        state = str((receipt or {}).get("state") or "UNKNOWN")
        if state in SENT_STATES:
            break
        if _rate_limited(receipt):
            # 额度已满时继续重试只会白烧额度，立即停手并把通知落盘。
            limited = True
            break
        if dispatch_id:
            state = _await_state(client, dispatch_id, wait)
            if state in SENT_STATES:
                break
    if limited:
        state = "RATE_LIMITED"

    record = {
        "at": _now(),
        "conversation": conversation,
        "conversation_id": conversation_id,
        "message_id": trigger_id,
        "state": state,
        "dispatch_id": dispatch_id,
        "recipient": agent_pid,
        "chars": len(text),
        "lines": len(lines),
        "urge": plan == "urge",
    }
    if state not in SENT_STATES:
        record["saved"] = _save_local(workspace, conversation, text)
        if limited:
            record["reason"] = "发送额度已满，通知已存入 handoff-inbox；额度恢复后不会自动补发"
    else:
        _ticket_open(
            workspace, conversation_id, conversation, agent_pid, reason,
            summary=str(text.split("客户问题：", 1)[-1].split(chr(10), 1)[0])[:160] if "客户问题：" in text else "",
        )
    _log(workspace, record)
    if not limited:
        _mark(workspace, conversation_id, ok=state in SENT_STATES, urge=plan == "urge", trigger_id=trigger_id)
    return record


def _await_state(client: Any, dispatch_id: str, timeout: float) -> str:
    """PENDING 不等于失败：等它落到终态再判断，最多等 timeout 秒。"""
    deadline = time.time() + max(0.0, timeout)
    state = ""
    while time.time() < deadline:
        time.sleep(1.5)
        try:
            info = client.tool("get_wecom_operation", {"dispatch_id": dispatch_id})
        except Exception:  # noqa: BLE001
            break
        state = str((info or {}).get("state") or state)
        if state in SENT_STATES:
            return state
        if state and state not in PENDING_STATES:
            return state
    return state or "PENDING"


def _save_local(workspace: Path, conversation: str, text: str) -> str:
    """发送通道不可用时把全文落到本地收件箱，保证转人工请求不丢。"""
    inbox = workspace / "handoff-inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\u4e00-\u9fa5-]+", "_", conversation or "unknown")[:20]
    path = inbox / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{safe}.txt"
    try:
        path.write_text(text, encoding="utf-8")
        return path.name
    except OSError:
        return ""


def _state_path(workspace: Path) -> Path:
    return workspace / "handoff-state.json"


def _load(workspace: Path) -> dict[str, Any]:
    path = _state_path(workspace)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text("utf-8"))
        return value if isinstance(value, dict) else {}
    except (ValueError, OSError):
        return {}


def _save(workspace: Path, value: dict[str, Any]) -> None:
    path = _state_path(workspace)
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _plan(
    workspace: Path, conversation_id: str, block: dict[str, Any], trigger_id: int = 0
) -> str:
    """决定这条转人工请求要不要发给人工客服。

    - 没有未结工单：按新请求发出去（客户关单后再要人工，也必须送到）。
    - 已有未结工单：不再重复发完整工单，改由 watcher 定时催办
      （`remind_pending`，默认每 10 分钟一次，直到人工回复）。
    """
    if open_ticket(workspace, conversation_id) is None:
        return "send"
    return "skip"


def _mark(
    workspace: Path, conversation_id: str, ok: bool, urge: bool, trigger_id: int = 0
) -> None:
    now = time.time()
    data = {
        key: value
        for key, value in _load(workspace).items()
        if isinstance(value, dict) and now - float(value.get("last") or 0) < 86400
    }
    entry = data.get(conversation_id) or {"reminders": 0, "last_reminder": 0}
    if urge:
        entry["reminders"] = int(entry.get("reminders") or 0) + 1
    if trigger_id:
        try:
            entry["last_trigger"] = max(int(entry.get("last_trigger") or 0), int(trigger_id))
        except (TypeError, ValueError):
            entry["last_trigger"] = int(trigger_id)
    # 首推与催办都要刷新基准时间，否则紧接着的第二次会被误判为可催办
    entry["last_reminder"] = now
    if ok or not urge:
        entry["last"] = now
    data[conversation_id] = entry
    _save(workspace, data)


def _log(workspace: Path, record: dict[str, Any]) -> None:
    try:
        path = workspace / "handoff-log.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + chr(10))
    except OSError:
        pass


# ---------------------------------------------------------------- 工单与人工回执

AGENT_ACK_REPLY = "人工已经受理，稍后人工会与您联系，请注意查收~"


def _ticket_path(workspace: Path) -> Path:
    return workspace / "handoff-tickets.json"


def _tickets(workspace: Path) -> dict[str, Any]:
    path = _ticket_path(workspace)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text("utf-8"))
        return value if isinstance(value, dict) else {}
    except (ValueError, OSError):
        return {}


def _save_tickets(workspace: Path, value: dict[str, Any]) -> None:
    path = _ticket_path(workspace)
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _send_text(config: dict[str, Any], recipient_id: str, text: str) -> dict[str, Any]:
    """轻量发送，用于定时催办。"""
    import responder  # noqa: PLC0415

    client = responder.faswecom_client(config)
    key = f"remind-{int(time.time() * 1000)}"
    try:
        receipt = client.tool(
            "send_wecom_message",
            {"recipient_id": recipient_id, "text": text, "idempotency_key": key},
        )
    except Exception as exc:  # noqa: BLE001
        return {"state": "ERROR", "error": str(exc)[:120]}
    state = str((receipt or {}).get("state") or "UNKNOWN")
    dispatch_id = str((receipt or {}).get("dispatch_id") or "")
    if state not in SENT_STATES and dispatch_id:
        state = _await_state(client, dispatch_id, 6.0)
    return {"state": state, "dispatch_id": dispatch_id}


def _remind_text(item: dict[str, Any], now: float) -> str:
    """第 1 次是提醒，第 2 次是催办。"""
    waited = max(0, int((now - float(item.get("at") or now)) / 60))
    count = int(item.get("reminders") or 0) + 1
    who = item.get("conversation") or "客户"
    if count <= 1:
        head = f"【提醒】{who} 在等人工回复"
    else:
        head = f"【催办】{who} 已等 {waited} 分钟，仍没人接手"
    lines = [head, "", f"已等待约 {waited} 分钟"]
    summary = str(item.get("summary") or "").strip()
    if summary:
        lines.append(f"客户问题：{summary}")
    lines.append("")
    lines.append(
        "客户正在等着你哦，收到请回复任意内容即可，"
        "我帮您转发给客户，说明您已经接手"
    )
    return chr(10).join(lines)


def remind_pending(config: dict[str, Any], workspace: Path) -> list[dict[str, Any]]:
    """人工一直没接手时按固定间隔提醒。

    同一张工单最多提醒 `handoff.max_reminders` 次（默认 2：一次提醒、一次催办），
    发完最后一次就把工单结掉，不再打扰。0 表示不限次数。
    """
    block = config.get("handoff") or {}
    if not block.get("enabled"):
        return []
    if str(config.get("mode", "dry_run")) != "live":
        return []
    agent_pid = str(block.get("agent_recipient_id") or "").strip()
    if not agent_pid:
        return []

    interval = float(block.get("reminder_interval_seconds", 600))
    limit = int(block.get("max_reminders", 2) or 0)
    now = time.time()
    data = _tickets(workspace)
    done: list[dict[str, Any]] = []

    for cid, item in list(data.items()):
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").upper() != "OPEN":
            continue
        if now - float(item.get("at") or 0) > 172800:
            continue
        if limit and int(item.get("reminders") or 0) >= limit:
            continue
        last = float(item.get("last_reminder") or item.get("at") or 0)
        if now - last < interval:
            continue

        receipt = _send_text(config, agent_pid, _remind_text(item, now))
        item["last_reminder"] = now
        item["reminders"] = int(item.get("reminders") or 0) + 1

        # 最后一次提醒发完，工单到此结束，不再反复打扰人工
        if limit and item["reminders"] >= limit:
            item["status"] = "CLOSED"
            item["closed_at"] = now
            item["close_reason"] = f"已提醒 {limit} 次，人工仍未接手，自动结束"

        data[cid] = item
        record = {
            "at": _now(),
            "kind": "remind",
            "conversation": item.get("conversation", ""),
            "conversation_id": cid,
            "state": receipt.get("state"),
            "dispatch_id": receipt.get("dispatch_id", ""),
            "round": item["reminders"],
            "closed": str(item.get("status") or "").upper() == "CLOSED",
        }
        _log(workspace, record)
        done.append(record)
        time.sleep(2.0)

    if done:
        _save_tickets(workspace, data)
    return done


def _ticket_open(
    workspace: Path,
    customer_cid: str,
    conversation: str,
    agent_pid: str,
    reason: str,
    summary: str = "",
) -> None:
    now = time.time()
    data = _tickets(workspace)
    data[customer_cid] = {
        "status": "OPEN",
        "conversation": conversation,
        "agent": agent_pid,
        "reason": reason,
        "summary": summary,
        "at": now,
        "last_reminder": now,
        "reminders": 0,
    }
    data = {k: v for k, v in data.items() if now - float(v.get("at") or 0) < 172800}
    _save_tickets(workspace, data)


def close_ticket(workspace: Path, customer_cid: str, reason: str = "") -> bool:
    """客户道谢/结束语时把工单结掉。"""
    data = _tickets(workspace)
    item = data.get(customer_cid)
    if not isinstance(item, dict) or item.get("status") == "CLOSED":
        return False
    item["status"] = "CLOSED"
    item["closed_at"] = time.time()
    item["close_reason"] = reason
    data[customer_cid] = item
    _save_tickets(workspace, data)
    _log(
        workspace,
        {
            "at": _now(),
            "kind": "ticket_close",
            "conversation_id": customer_cid,
            "conversation": item.get("conversation", ""),
            "reason": reason,
        },
    )
    return True


def open_ticket(workspace: Path, customer_cid: str) -> dict[str, Any] | None:
    """取该客户尚未结单的工单（OPEN / ACKED），没有则返回 None。"""
    item = _tickets(workspace).get(customer_cid)
    if not isinstance(item, dict):
        return None

    status = str(item.get("status") or "").upper()
    if status not in ("OPEN", "ACKED"):
        return None
    if time.time() - float(item.get("at") or 0) > 172800:
        return None
    return item


def is_agent_conversation(config: dict[str, Any], conversation_id: str) -> bool:
    """判断这条会话是不是人工客服本人，而不是客户。"""
    block = config.get("handoff") or {}
    target = str(block.get("agent_conversation_id") or "").strip()
    return bool(target) and conversation_id == target


def agent_ack(
    config: dict[str, Any],
    workspace: Path,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """人工客服回话后，把「已转达」回给对应客户并关单。"""
    data = _tickets(workspace)
    open_items = [
        (cid, item) for cid, item in data.items()
        if isinstance(item, dict)
        and str(item.get("status") or "").upper() in ("OPEN", "ACKED")
    ]
    if not open_items:
        return {"state": "NO_TICKET"}
    customer_cid, ticket = max(open_items, key=lambda pair: float(pair[1].get("at") or 0))

    if str(config.get("mode", "dry_run")) != "live":
        return {"state": "DRY_RUN", "customer": ticket.get("conversation", "")}

    import responder  # noqa: PLC0415

    client = responder.faswecom_client(config)
    recipient_id, note = responder.resolve_recipient(client, str(ticket.get("conversation") or ""))
    if not recipient_id:
        _log(
            workspace,
            {"at": _now(), "state": "FAILED", "reason": note or "客户接收人无法识别", "kind": "agent_ack"},
        )
        return {"state": "FAILED", "reason": note}

    trigger_id = int((messages[-1] or {}).get("id") or 0) if messages else 0
    key = re.sub(r"[^0-9A-Za-z_-]+", "-", f"ack-{customer_cid}-{trigger_id}")[:110]
    try:
        receipt = client.tool(
            "send_wecom_message",
            {"recipient_id": recipient_id, "text": AGENT_ACK_REPLY, "idempotency_key": key},
        )
    except Exception as exc:  # noqa: BLE001
        receipt = {}
        note = str(exc)[:120]
    state = str((receipt or {}).get("state") or "UNKNOWN")
    dispatch_id = str((receipt or {}).get("dispatch_id") or "")
    if state not in SENT_STATES and dispatch_id:
        state = _await_state(client, dispatch_id, 8.0)

    record = {
        "at": _now(),
        "kind": "agent_ack",
        "customer": ticket.get("conversation", ""),
        "conversation_id": customer_cid,
        "message_id": trigger_id,
        "state": state,
        "dispatch_id": dispatch_id,
        "reply": AGENT_ACK_REPLY,
    }
    if state in SENT_STATES:
        # 人工已接手即视为智能客服工单结束，后续是人工与客户的单线联系
        now_ack = time.time()
        ticket["status"] = "CLOSED"
        ticket["acked_at"] = now_ack
        ticket["closed_at"] = now_ack
        ticket["close_reason"] = "人工已接手"
        data[customer_cid] = ticket
        _save_tickets(workspace, data)
    _log(workspace, record)
    return record
