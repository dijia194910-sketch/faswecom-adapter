#!/usr/bin/env python
"""快马商城订单与物流技能（直连数据库版）。

三种结果：
1. 门店唯一 → 返回订单与物流明细
2. 查不到门店 → 提示核对签约人姓名
3. 多门店 → 列出候选，等客户确认后再查
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

STATUS = {
    "TRADE_CLOSED": "已作废",
    "WAIT_SELLER_SEND_GOODS": "待发货",
    "SELLER_PART_SEND_GOODS": "部分已发货",
    "WAIT_BUYER_CONFIRM_GOODS": "待收货",
    "CANCELING": "取消中",
    "WAIT_CHECK": "",
    "TRADE_FINISHED": "交易成功",
}

NOT_FOUND = "系统上暂时未查询到相关物流信息，请您检查签约人姓名是否有误。"
MAX_ORDERS = 3


def load_db(config: dict[str, Any]) -> dict[str, Any]:
    block = config.get("skill_db") or {}
    if block.get("host"):
        return block
    mcp = Path(str(config.get("workbuddy_mcp", ""))) if config.get("workbuddy_mcp") else None
    if mcp is None:
        mcp = Path.home() / ".workbuddy" / "mcp.json"
    if not mcp.is_file():
        return {}
    try:
        servers = json.loads(mcp.read_text("utf-8")).get("mcpServers") or {}
    except (ValueError, OSError):
        return {}
    env = (servers.get("xlx_diros") or {}).get("env") or {}
    return {
        "host": env.get("MYSQL_HOST", ""),
        "port": int(env.get("MYSQL_PORT", 9030) or 9030),
        "user": env.get("MYSQL_USER", ""),
        "password": env.get("MYSQL_PASSWORD", ""),
        "database": env.get("MYSQL_DATABASE", ""),
    }


class KmOrderSkill:
    def __init__(self, config: dict[str, Any], workspace: Path):
        self.config = config
        self.workspace = workspace
        self.db = load_db(config)
        self.days = int((config.get("skills") or {}).get("order_days", 90))
        vendor = workspace / "vendor"
        if vendor.is_dir() and str(vendor) not in sys.path:
            sys.path.insert(0, str(vendor))

    # ---------------------------------------------------------------- 连接

    def _connect(self):
        import pymysql  # noqa: PLC0415

        return pymysql.connect(
            host=self.db["host"],
            port=self.db["port"],
            user=self.db["user"],
            password=self.db["password"],
            database=self.db["database"],
            charset="utf8mb4",
            connect_timeout=8,
            read_timeout=25,
            write_timeout=15,
        )

    def _query(self, sql: str, params: tuple | list = ()) -> list[tuple]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())
        finally:
            conn.close()

    # ---------------------------------------------------------------- 主流程

    def handle(
        self,
        signed_name: str,
        franchisee_id: str = "",
        question: str = "",
    ) -> dict[str, Any]:
        name = str(signed_name or "").strip()
        if not name:
            return {"kind": "error", "reason": "缺少签约人姓名"}

        if not self.db.get("host"):
            return {"kind": "error", "reason": "数据库连接信息缺失"}

        if franchisee_id:
            row = self._by_id(franchisee_id)
            if row:
                return self._orders(row, question)
            return {"kind": "not_found"}

        hits = self._search(name)
        if not hits:
            return {"kind": "not_found"}

        exact = [row for row in hits if str(row[2] or "").strip() == name]
        if len(exact) == 1:
            return self._orders(exact[0])

        return {
            "kind": "choose",
            "approximate": not exact,
            "keyword": name,
            "candidates": [
                {"id": str(r[0]), "name": str(r[2] or ""), "address": str(r[3] or "")}
                for r in (exact or hits)[:10]
            ],
            "total": len(exact or hits),
        }

    def _search(self, name: str) -> list[tuple]:
        pattern = name.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        sql = (
            "SELECT f.id, f.franchisee_code, f.franchisee_name, f.address, f.state "
            "FROM mds_franchisee f WHERE f.franchisee_name LIKE %s ESCAPE '!' "
            "ORDER BY f.id ASC LIMIT 101"
        )
        return self._query(sql, (f"%{pattern}%",))

    def _by_id(self, franchisee_id: str) -> tuple | None:
        try:
            fid = int(str(franchisee_id))
        except ValueError:
            return None
        rows = self._query(
            "SELECT f.id, f.franchisee_code, f.franchisee_name, f.address, f.state "
            "FROM mds_franchisee f WHERE f.id = %s",
            (fid,),
        )
        return rows[0] if rows else None

    # ---------------------------------------------------------------- 订单

    def _orders(self, store: tuple, question: str = "") -> dict[str, Any]:
        store_name = str(store[2] or "")
        days = self.days
        end = datetime.now().replace(microsecond=0)
        start = end - timedelta(days=days)

        same = self._query(
            "SELECT COUNT(*) FROM mds_franchisee WHERE franchisee_name = %s", (store_name,)
        )
        if same and int(same[0][0]) > 1:
            return {
                "kind": "duplicate_name",
                "name": store_name,
                "reply": f"系统里有 {int(same[0][0])} 家门店都叫「{store_name}」，仅凭姓名无法区分订单归属，已转人工核实。",
            }

        rows = self._query(
            "SELECT o.ecshop, o.order_id, o.order_create_time, o.trade_status, "
            "o.final_fee, o.dly_corp_name, o.waybill, o.receive_time "
            "FROM km_order o WHERE o.user_name = %s "
            "AND o.order_create_time >= %s AND o.order_create_time < %s "
            "ORDER BY o.order_create_time DESC, o.ecshop DESC, o.order_id DESC LIMIT 21",
            (store_name, start, end),
        )
        if not rows:
            rows = self._query(
                "SELECT o.ecshop, o.order_id, o.order_create_time, o.trade_status, "
                "o.final_fee, o.dly_corp_name, o.waybill, o.receive_time "
                "FROM km_order o WHERE o.user_name = %s "
                "ORDER BY o.order_create_time DESC, o.ecshop DESC, o.order_id DESC LIMIT 21",
                (store_name,),
            )
            if not rows:
                return {"kind": "no_order", "name": store_name, "days": days}
            scope = "全部历史"
        else:
            scope = f"近 {days} 天"

        shown = rows[:MAX_ORDERS]
        keys = [(str(r[0]), str(r[1])) for r in shown]
        items = self._items(keys)
        children = self._children(keys)

        recent_cut = datetime.now() - timedelta(days=3)
        orders: list[dict[str, Any]] = []
        for index, row in enumerate(shown, 1):
            ecshop, order_id, created, status, fee, corp, waybill, received = row
            key = (str(ecshop), str(order_id))
            child_waybills = [str(child[3] or "").strip() for child in children.get(key, [])
                              if str(child[3] or "").strip()]
            goods = items.get(key) or []
            goods_text = "；".join(goods[:8])
            if len(goods) > 8:
                goods_text += f" 等 {len(goods)} 项"
            orders.append({
                "index": index,
                "ecshop": str(ecshop),
                "order_id": str(order_id),
                "when": created.strftime("%Y-%m-%d %H:%M") if isinstance(created, datetime) else str(created or ""),
                "status": STATUS.get(str(status or ""), str(status or "")),
                "fee": self._num(fee),
                "goods": goods_text,
                "corp": str(corp or ""),
                "waybill": str(waybill or ""),
                "child_waybills": child_waybills,
                "has_waybill": bool(str(waybill or "").strip()) or bool(child_waybills),
                "received": received.strftime("%Y-%m-%d %H:%M") if isinstance(received, datetime) else "",
                "recent": isinstance(created, datetime) and created >= recent_cut,
            })

        # 近三天有订单还没记录运单号：不要把“未记录”整段甩给客户，
        # 先把候选订单（时间/金额/商品）发过去，让客户确认要查哪一笔。
        if any(order["recent"] and not order["has_waybill"] for order in orders):
            return {
                "kind": "pick_order",
                "name": store_name,
                "days": days,
                "total": len(rows),
                "orders": orders,
            }

        lines = [f"{store_name}（{scope}）共 {len(rows)} 笔订单"]
        for order in orders:
            key = (order["ecshop"], order["order_id"])
            block = [f"订单{order['index']}：{order['when']}　{order['status']}　订单金额（原值）{order['fee']}"]
            if order["corp"] or order["waybill"]:
                block.append(f"物流：{order['corp'] or '物流公司未记录'}　运单号 {order['waybill'] or '未记录'}")
            else:
                block.append("物流：未记录运单号")
            for child in children.get(key, []):
                if child[2] or child[3]:
                    block.append(f"子单物流：{child[2] or '未记录'}　运单号 {child[3] or '未记录'}")
            if order["received"]:
                block.append(f"收货时间：{order['received']}")
            if order["goods"]:
                block.append(f"商品：{order['goods']}")
            lines.append("\n".join(block))

        if len(rows) > len(shown):
            lines.append(f"另有 {len(rows) - len(shown)} 笔较早订单未列出，需要的话我再发。")

        return {
            "kind": "ok",
            "name": store_name,
            "reply": "\n\n".join(lines),
        }

    def _items(self, keys: list[tuple[str, str]]) -> dict[tuple[str, str], list[str]]:
        if not keys:
            return {}
        where = " OR ".join(["(i.ecshop=%s AND i.order_id=%s)"] * len(keys))
        params: list[Any] = []
        for ecshop, order_id in keys:
            params.extend([ecshop, order_id])
        rows = self._query(
            "SELECT i.ecshop, i.order_id, i.product_name, i.sku_name, i.props, "
            "i.qty, i.unit, i.max_qty, i.max_unit "
            f"FROM km_order_item i WHERE {where} "
            "ORDER BY i.id ASC",
            tuple(params),
        )
        grouped: dict[tuple[str, str], list[str]] = {}
        for r in rows:
            key = (str(r[0]), str(r[1]))
            name = str(r[2] or "").strip()
            spec = str(r[3] or "").strip() or str(r[4] or "").strip()
            if spec:
                if spec.startswith(name):
                    spec = spec[len(name):].strip(" ，,/-")
                elif spec == name or spec in name:
                    spec = ""
            if str(r[7] or "").strip() and str(r[8] or "").strip():
                qty, unit = r[7], r[8]
            else:
                qty, unit = r[5], r[6]
            text = f"{name}（{spec}）×{self._num(qty)}{unit or ''}" if spec else f"{name}×{self._num(qty)}{unit or ''}"
            grouped.setdefault(key, []).append(text)
        return grouped

    def _children(self, keys: list[tuple[str, str]]) -> dict[tuple[str, str], list[tuple]]:
        if not keys:
            return {}
        where = " OR ".join(["(c.ecshop=%s AND c.order_id=%s)"] * len(keys))
        params: list[Any] = []
        for ecshop, order_id in keys:
            params.extend([ecshop, order_id])
        rows = self._query(
            "SELECT c.ecshop, c.order_id, c.dly_corp_name, c.waybill "
            f"FROM km_order_child c WHERE {where} "
            "ORDER BY c.id ASC",
            tuple(params),
        )
        grouped: dict[tuple[str, str], list[tuple]] = {}
        for r in rows:
            grouped.setdefault((str(r[0]), str(r[1])), []).append(tuple(r))
        return grouped

    @staticmethod
    def _num(value: Any) -> str:
        if value is None:
            return "0"
        text = str(value)
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"


PICK_HEAD = "查到您近 3 天还有订单没有运单号，请回复序号确认要查询哪一笔："
ORDER_HANDOFF_REPLY = "好的请稍等，现在帮您转发人工查询，请稍后。"

_ORDINAL = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def render_pick(out: dict[str, Any]) -> str:
    """列出候选订单（时间/状态/金额/商品），不给运单号，让客户确认是哪一笔。"""
    lines = [PICK_HEAD]
    for order in out.get("orders") or []:
        lines.append(
            f"订单{order['index']}：{order['when']}　{order['status']}　订单金额（原值）{order['fee']}"
        )
        if order.get("goods"):
            lines.append(f"商品：{order['goods']}")
    lines.append("回复“订单1”这样的序号即可。")
    return chr(10).join(lines)


def pick_order(out: dict[str, Any], text: str) -> dict[str, Any] | None:
    """客户用“订单一/订单1/1”确认要查哪一笔订单。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    orders = out.get("orders") or []
    if not orders:
        return None
    token = ""
    match = re.search(r"订单\s*([0-9一二三四五六七八九十]{1,2})", raw)
    if not match:
        match = re.search(r"第\s*([0-9一二三四五六七八九十]{1,2})\s*[笔单]", raw)
    if match:
        token = match.group(1)
    else:
        match = re.fullmatch(r"([0-9一二三四五六七八九十]{1,2})\s*[.、,，]?", raw.replace(" ", ""))
        if match:
            token = match.group(1)
    if not token:
        return None
    index = int(token) if token.isdigit() else _ORDINAL.get(token, 0)
    if 1 <= index <= len(orders):
        return orders[index - 1]
    return None


def order_reply_text(order: dict[str, Any]) -> str:
    """客户选中的订单有运单号时，直接给物流信息。"""
    lines = [f"订单{order['index']}（{order['when']}，{order['status']}）的物流信息："]
    if order.get("waybill"):
        lines.append(f"{order.get('corp') or '物流公司'}　运单号 {order['waybill']}")
    else:
        lines.append("运单号暂未记录")
    if order.get("received"):
        lines.append(f"收货时间：{order['received']}")
    return chr(10).join(lines)


def choose_candidate(candidates: list[dict[str, str]], text: str) -> dict[str, str] | None:
    """客户用序号或门店全名确认候选门店。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits:
        index = int(digits)
        if 1 <= index <= len(candidates):
            return candidates[index - 1]
    compact = raw.replace(" ", "")
    for item in candidates:
        name = item.get("name", "")
        if name and (name in compact or compact in name):
            return item
    return None
