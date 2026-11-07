#!/usr/bin/env python
"""技能路由层：知识库未命中时，按注册表把问题路由到对应业务技能。

设计要点：
- 注册表驱动（skills/registry.json），新增业务流程不改本文件。
- 槽位收集与记忆（slot-state.json），同一会话不重复索要。
- 技能执行交给 WorkBuddy CLI 加载对应 skill 完成，本文件只负责调度与取回文本。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import km_order  # noqa: E402
from km_order import choose_candidate  # noqa: E402

NAME_PATTERN = re.compile(r"^[\u4e00-\u9fa5·]{2,4}\d{0,2}店?$")

NAME_MARKERS = (
    "签约人姓名是", "签约人姓名叫", "签约人名字是", "签约人叫", "签约人是",
    "签约人姓名", "签约人", "门店叫", "门店是", "姓名是", "名字是", "姓名", "名字", "叫",
)
NAME_TAIL_STOP = re.compile(r"[有的门店对吧呢啊哦呀嘛哈，。,.!！?？]")

NON_NAME = {
    "在吗", "在不在", "你好", "您好", "谢谢", "好的", "收到", "嗯嗯", "是的",
    "没有", "什么", "怎么", "多少", "可以", "不行", "对的", "不是",
    "不知道", "不清楚", "不确定", "不记得", "忘了", "忘记了", "记不清",
    "查不到", "算了", "不用了", "没事", "等等", "稍等", "你说什么",
}


def extract_name(text: str) -> str:
    """从客户这句话里取出姓名。支持整句就是姓名，也支持「签约人姓名是何子君」这类说法。"""
    raw = normalise(text)
    if not raw or raw in NON_NAME:
        return ""
    for marker in NAME_MARKERS:
        if marker not in raw:
            continue
        tail = raw.split(marker, 1)[1]
        tail = NAME_TAIL_STOP.split(tail, 1)[0]
        match = re.match(r"^[\u4e00-\u9fa5·]{2,4}\d{0,2}店?", tail)
        if match:
            return match.group(0)
    if NAME_PATTERN.match(raw):
        return raw
    return ""


class SkillError(RuntimeError):
    pass


@dataclass
class Route:
    kind: str = "none"  # skill / need_slot / ambiguous / none
    skill: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    runner_up: float = 0.0
    slots: dict[str, str] = field(default_factory=dict)
    prompt: str = ""
    reason: str = ""


def normalise(text: str) -> str:
    return re.sub(r"[\s？?！!。，,、~～：:（）()]+", "", str(text or ""))


def similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalise(left), normalise(right)).ratio()


class SkillRouter:
    def __init__(self, config: dict[str, Any], workspace: Path):
        block = config.get("skills") or {}
        self.enabled = bool(block.get("enabled", True))
        self.workspace = workspace
        self.registry_path = workspace / str(block.get("registry", "skills/registry.json"))
        self.state_path = workspace / str(block.get("slot_state", "slot-state.json"))
        self.slot_ttl = int(block.get("slot_ttl_seconds", 1800))
        self.slot_retry_max = int(block.get("slot_retry_max", 2))
        self.notfound_max = int(block.get("notfound_max", 3))
        self.choose_retry_max = int(block.get("choose_retry_max", 2))
        self.cli_timeout = int(block.get("cli_timeout_seconds", 150))
        self.max_turns = int(block.get("max_turns", 14))
        self.last_backend = ""
        self.config = config
        self.block = block
        self._registry: list[dict[str, Any]] | None = None

    # ------------------------------------------------------------ 注册表

    def registry(self) -> list[dict[str, Any]]:
        if self._registry is not None:
            return self._registry
        if not self.registry_path.is_file():
            self._registry = []
            return self._registry
        try:
            data = json.loads(self.registry_path.read_text("utf-8"))
        except (ValueError, OSError):
            self._registry = []
            return self._registry
        skills = data.get("skills") if isinstance(data, dict) else None
        self._registry = [
            item
            for item in (skills or [])
            if isinstance(item, dict) and str(item.get("status", "")).upper() == "ACTIVE"
        ]
        return self._registry

    # ------------------------------------------------------------ 打分

    def score(self, skill: dict[str, Any], question: str) -> float:
        text = normalise(question)
        if not text:
            return 0.0
        for bad in skill.get("negatives") or []:
            if normalise(bad) and normalise(bad) in text:
                return 0.0
        strong = [normalise(t) for t in skill.get("strong_triggers") or [] if normalise(t)]
        weak = [normalise(t) for t in skill.get("weak_triggers") or [] if normalise(t)]
        legacy = [normalise(t) for t in skill.get("triggers") or [] if normalise(t)]

        strong_hits = sum(1 for token in strong if token in text)
        weak_hits = sum(1 for token in weak if token in text)
        legacy_hits = sum(1 for token in legacy if token in text)
        total = min(strong_hits, 2) * 4.0 + min(weak_hits, 2) * 2.0 + min(legacy_hits, 2) * 3.0

        best = 0.0
        for example in skill.get("examples") or []:
            best = max(best, similarity(question, example))
        if best >= 0.8:
            total += 2.0
        elif best >= 0.55:
            total += 1.0
        return total

    def choose(self, question: str) -> tuple[dict[str, Any] | None, float, float]:
        ranked = sorted(
            ((self.score(skill, question), skill) for skill in self.registry()),
            key=lambda pair: (pair[0], -float(pair[1].get("priority", 99))),
            reverse=True,
        )
        if not ranked:
            return None, 0.0, 0.0
        top_score, top_skill = ranked[0]
        second = ranked[1][0] if len(ranked) > 1 else 0.0
        return top_skill, top_score, second

    # ------------------------------------------------------------ 槽位状态

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {}
        try:
            value = json.loads(self.state_path.read_text("utf-8"))
        except (ValueError, OSError):
            return {}
        return value if isinstance(value, dict) else {}

    def _save_state(self, value: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def pending(self, conversation_id: str) -> dict[str, Any] | None:
        entry = self._load_state().get(conversation_id)
        if not isinstance(entry, dict):
            return None
        if time.time() > float(entry.get("expires_at", 0)):
            self.clear(conversation_id)
            return None
        return entry

    def remember(
        self,
        conversation_id: str,
        skill: dict[str, Any],
        slots: dict[str, str],
        asked: int = 1,
    ) -> None:
        state = self._load_state()
        previous = state.get(conversation_id) or {}
        entry = {
            "skill_id": skill.get("id", ""),
            "slots": slots,
            "asked": asked,
            "expires_at": time.time() + self.slot_ttl,
            "updated_at": int(time.time()),
        }
        if int(previous.get("notfound", 0)) > 0:
            entry["notfound"] = int(previous["notfound"])
        state[conversation_id] = entry
        self._save_state(state)

    def clear(self, conversation_id: str) -> None:
        state = self._load_state()
        if state.pop(conversation_id, None) is not None:
            self._save_state(state)

    # ------------------------------------------------------------ 路由

    def route(self, conversation_id: str, question: str) -> Route:
        if not self.enabled:
            return Route(kind="none", reason="技能路由未启用")

        entry = self.pending(conversation_id)
        if entry:
            skill = next(
                (s for s in self.registry() if s.get("id") == entry.get("skill_id")), None
            )
            if skill and entry.get("orders"):
                chosen = km_order.pick_order(entry, question)
                if chosen:
                    self.clear(conversation_id)
                    if chosen.get("has_waybill"):
                        return Route(
                            kind="order_reply",
                            skill=skill,
                            prompt=km_order.order_reply_text(chosen),
                            reason="客户已确认订单（有运单号）",
                        )
                    return Route(
                        kind="handoff",
                        skill=skill,
                        prompt=km_order.ORDER_HANDOFF_REPLY,
                        reason="客户查询近三天未记录运单号的订单，需人工核实运单",
                    )
                tries = int(entry.get("choose_tries", 0)) + 1
                if tries >= self.choose_retry_max:
                    self.clear(conversation_id)
                    return Route(
                        kind="handoff",
                        skill=skill,
                        prompt=km_order.ORDER_HANDOFF_REPLY,
                        reason="订单序号确认连续无效，需人工核实",
                    )
                entry["choose_tries"] = tries
                entry["expires_at"] = time.time() + self.slot_ttl
                state = self._load_state()
                state[conversation_id] = entry
                self._save_state(state)
                return Route(
                    kind="need_slot",
                    skill=skill,
                    slots={},
                    prompt="请回复订单序号（例如：订单1）。",
                    reason="等待客户确认订单",
                )
            if skill and entry.get("candidates"):
                chosen = choose_candidate(entry["candidates"], question)
                if chosen:
                    self.clear(conversation_id)
                    return Route(
                        kind="skill",
                        skill=skill,
                        slots={
                            "signed_name": chosen.get("name", ""),
                            "franchisee_id": chosen.get("id", ""),
                        },
                        reason="客户已确认门店",
                    )
                # 客户没说序号，而是报了一个新姓名：换人重查，不要卡在候选上
                fresh = extract_name(question)
                if fresh and fresh != str(entry.get("keyword") or ""):
                    self.clear(conversation_id)
                    self.remember(conversation_id, skill, {"signed_name": fresh}, asked=1)
                    return Route(
                        kind="skill",
                        skill=skill,
                        slots={"signed_name": fresh},
                        reason=f"客户改用新姓名重查：{fresh}",
                    )
                tries = int(entry.get("choose_tries", 0)) + 1
                if tries >= self.choose_retry_max:
                    self.clear(conversation_id)
                    return Route(
                        kind="none",
                        skill=skill,
                        reason="门店确认连续无效，转人工",
                    )
                entry["choose_tries"] = tries
                entry["expires_at"] = time.time() + self.slot_ttl
                state = self._load_state()
                state[conversation_id] = entry
                self._save_state(state)
                return Route(
                    kind="need_slot",
                    skill=skill,
                    slots={},
                    prompt="请回复门店序号（例如：1）；如果都不是，请直接回复正确的门店合同签约人姓名。",
                    reason="等待门店确认",
                )
            if skill:
                return self._fill_slot(conversation_id, skill, entry, question)

        skill, top, second = self.choose(question)
        if not skill or top < float(skill.get("min_score", 4)):
            return Route(kind="none", score=top, reason="无技能达到阈值")

        if top - second < 2 and second >= float(skill.get("min_score", 4)):
            return Route(
                kind="ambiguous",
                skill=skill,
                score=top,
                runner_up=second,
                prompt="老板您好，为了准确帮您处理，麻烦说明一下是查询订单物流进度，还是售后退换问题？",
                reason="两个技能分差过小",
            )

        slots = {slot["name"]: "" for slot in skill.get("slots") or [] if slot.get("required")}
        missing = [slot for slot in skill.get("slots") or [] if slot.get("required") and not slots.get(slot["name"])]
        if missing:
            slot = missing[0]
            guess = self._autoextract(slot, question)
            if guess:
                slots[slot["name"]] = guess
                rest = [
                    s for s in skill.get("slots") or []
                    if s.get("required") and not slots.get(s.get("name"))
                ]
                if not rest:
                    self.remember(conversation_id, skill, slots, asked=1)
                    return Route(kind="skill", skill=skill, score=top, slots=slots, reason="槽位由问句自动提取")
                slot = rest[0]
            self.remember(conversation_id, skill, slots, asked=1)
            return Route(
                kind="need_slot",
                skill=skill,
                score=top,
                slots=slots,
                prompt=str(slot.get("prompt") or ""),
                reason=f"缺少槽位 {slot['name']}" if not guess else f"缺少槽位 {slot['name']}（已提取部分）",
            )

        return Route(kind="skill", skill=skill, score=top, slots=slots, reason="技能命中")

    def _autoextract(self, slot: dict[str, Any], question: str) -> str:
        """从问句里直接拿到槽位值，避免问了客户已经说过的话。"""
        if slot.get("autoextract") != "store_prefix":
            return ""
        stop = [str(w) for w in slot.get("stopwords") or []]
        for match in re.finditer(r"([\u4e00-\u9fa5]{2,4})(\d{0,2})门?店", question):
            base, digits = match.group(1), match.group(2)
            if len(base) < 2 or base in stop or any(base.endswith(w) for w in stop):
                continue
            return f"{base}{digits}店" if digits else base
        return ""

    def _fill_slot(
        self, conversation_id: str, skill: dict[str, Any], entry: dict[str, Any], question: str
    ) -> Route:
        slots = dict(entry.get("slots") or {})
        spec = next(
            (s for s in skill.get("slots") or [] if s.get("required") and not slots.get(s.get("name"))),
            None,
        )
        asked = int(entry.get("asked", 1))
        if not spec:
            # 槽位已齐但客户又发来一句，视为更正姓名后重新查询
            replaced = self._replace_name(skill, slots, question)
            if replaced is not None:
                self.remember(conversation_id, skill, replaced, asked=1)
                return Route(kind="skill", skill=skill, slots=replaced, reason="客户更正姓名后重查")
            return self.clear_and_route(conversation_id, skill, slots)

        value = str(question or "").strip()
        found = extract_name(value)
        if found:
            slots[spec["name"]] = found
            remaining = [
                s for s in skill.get("slots") or [] if s.get("required") and not slots.get(s.get("name"))
            ]
            if not remaining:
                self.remember(conversation_id, skill, slots, asked=1)
                return Route(kind="skill", skill=skill, slots=slots, reason="槽位已齐")
            self.remember(conversation_id, skill, slots, asked=1)
            return Route(
                kind="need_slot",
                skill=skill,
                slots=slots,
                prompt=str(remaining[0].get("prompt") or ""),
                reason="继续收集槽位",
            )

        if asked >= self.slot_retry_max:
            self.clear(conversation_id)
            return Route(kind="none", skill=skill, reason="两次未获得签约人姓名")

        self.remember(conversation_id, skill, slots, asked=asked + 1)
        hint = spec.get("reask") if asked == 1 else spec.get("invalid")
        return Route(
            kind="need_slot",
            skill=skill,
            slots=slots,
            prompt=str(hint or spec.get("prompt") or ""),
            reason="槽位无效，再次索要",
        )

    def _replace_name(
        self, skill: dict[str, Any], slots: dict[str, str], question: str
    ) -> dict[str, str] | None:
        """客户在“查无此人”之后再发一个名字，视为更正，而不是新问题。"""
        first = next((s for s in skill.get("slots") or [] if s.get("required")), None)
        if not first:
            return None
        text = str(question or "").strip()
        found = extract_name(text)
        if not found:
            return None
        updated = dict(slots)
        updated[first["name"]] = found
        return updated

    def clear_and_route(
        self, conversation_id: str, skill: dict[str, Any], slots: dict[str, str]
    ) -> Route:
        self.clear(conversation_id)
        return Route(kind="skill", skill=skill, slots=slots, reason="槽位已齐")

    # ------------------------------------------------------------ 技能执行

    def execute(
        self,
        route: Route,
        question: str,
        history: str,
        conversation: str,
        conversation_id: str = "",
    ) -> str:
        skill = route.skill
        name = str(skill.get("skill") or "")
        if not name:
            return ""
        self.last_backend = str(self.block.get("executor", "queue")).lower()
        if self.last_backend == "builtin":
            return self._builtin(route, question, conversation_id)
        if self.last_backend == "queue":
            self._enqueue(route, question, history, conversation)
            return ""
        prompt = self._prompt(skill, route.slots, question, history, conversation)
        return self._call_cli(name, prompt)

    def _builtin(self, route: Route, question: str, conversation_id: str) -> str:
        """内置执行器：进程内直连数据库，毫秒级返回。"""
        try:
            import km_order  # noqa: PLC0415
        except ImportError:
            return ""
        slots = route.slots or {}
        skill = km_order.KmOrderSkill(self.config, self.workspace)
        try:
            out = skill.handle(
                str(slots.get("signed_name") or ""),
                str(slots.get("franchisee_id") or ""),
                question,
            )
        except Exception as exc:  # noqa: BLE001
            self._log_error(exc)
            return ""
        kind = out.get("kind")
        if kind == "pick_order":
            # 近三天有未记录运单号的订单：先让客户确认是哪一笔，不直接发“未记录”
            self.remember_orders(conversation_id, route.skill, out)
            return km_order.render_pick(out)
        if kind == "ok":
            self.clear(conversation_id)
            return str(out.get("reply") or "")
        if kind == "not_found":
            tries = self._bump_notfound(conversation_id)
            if tries >= self.notfound_max:
                self.clear(conversation_id)
                return km_order.NOT_FOUND + "我已经帮您转接客服同事，稍后会有人跟您核实。"
            return km_order.NOT_FOUND
        if kind == "no_order":
            self.clear(conversation_id)
            return (
                f"{out.get('name', '')} 在近 {out.get('days', 90)} 天内没有商城订单记录，"
                "暂时查不到物流信息。"
            )
        if kind in ("choose", "duplicate_name"):
            if kind == "choose":
                self.remember_choice(conversation_id, route.skill, out)
            else:
                self.clear(conversation_id)
            return self._render_candidates(out)
        return ""

    def _bump_notfound(self, conversation_id: str) -> int:
        """查无此人时保留槽位状态，允许客户改个名字再试，并计数防死循环。"""
        state = self._load_state()
        entry = state.get(conversation_id)
        if not isinstance(entry, dict):
            return 0
        entry["notfound"] = int(entry.get("notfound", 0)) + 1
        entry["expires_at"] = time.time() + self.slot_ttl
        state[conversation_id] = entry
        self._save_state(state)
        return entry["notfound"]

    def _render_candidates(self, out: dict) -> str:
        if out.get("kind") == "duplicate_name":
            return str(out.get("reply") or "")
        items = out.get("candidates") or []
        if out.get("approximate"):
            head = (
                f"系统里没有完全一致的签约人姓名「{out.get('keyword', '')}」，"
                f"名称相近的有 {out.get('total', len(items))} 家，请回复序号确认是不是这家，我再帮您查订单："
            )
        else:
            head = f"系统里查到 {out.get('total', len(items))} 家门店，请回复序号确认是哪一家，我再帮您查订单："
        lines = [head]
        for index, item in enumerate(items, 1):
            address = f"（{item['address']}）" if item.get("address") else ""
            lines.append(f"{index}. {item.get('name', '')}{address}")
        return chr(10).join(lines)

    def remember_orders(self, conversation_id: str, skill: dict, out: dict) -> None:
        """记住候选订单，等客户回复序号后直接给出物流信息或转人工。"""
        if not conversation_id:
            return
        state = self._load_state()
        state[conversation_id] = {
            "skill_id": skill.get("id", ""),
            "slots": {},
            "asked": 1,
            "orders": out.get("orders") or [],
            "expires_at": time.time() + self.slot_ttl,
            "updated_at": int(time.time()),
        }
        self._save_state(state)

    def remember_choice(self, conversation_id: str, skill: dict, out: dict) -> None:
        if not conversation_id:
            return
        state = self._load_state()
        state[conversation_id] = {
            "skill_id": skill.get("id", ""),
            "slots": {},
            "asked": 1,
            "candidates": out.get("candidates") or [],
            "candidate_total": out.get("total", 0),
            "expires_at": time.time() + self.slot_ttl,
            "updated_at": int(time.time()),
        }
        self._save_state(state)

    def _log_error(self, exc: BaseException) -> None:
        log_dir = self.workspace / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        try:
            with (log_dir / "skill-errors.log").open("a", encoding="utf-8") as stream:
                stream.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {type(exc).__name__}: {exc}{chr(10)}")
        except OSError:
            pass

    def _enqueue(
        self,
        route: Route,
        question: str,
        history: str,
        conversation: str,
    ) -> None:
        """执行通道不可用时的降级：写入待办队列，交人工或后续自动化消费。"""
        record = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "conversation": conversation,
            "skill_id": route.skill.get("id", ""),
            "skill": route.skill.get("skill", ""),
            "slots": {k: v for k, v in route.slots.items() if v},
            "question": question[:500],
            "history": history[-800:],
            "status": "PENDING",
        }
        path = self.workspace / str(self.block.get("queue_file", "skill-queue.jsonl"))
        try:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            pass

    def _prompt(
        self,
        skill: dict[str, Any],
        slots: dict[str, str],
        question: str,
        history: str,
        conversation: str,
    ) -> str:
        slot_lines = "\n".join(f"- {key}：{value}" for key, value in slots.items() if value) or "- 无"
        return f"""使用 ${skill.get('skill')} 处理一条企业微信客户咨询。

业务域：{skill.get('domain', '')}
客户：{conversation}
已确认参数：
{slot_lines}

最近对话：
{history or '（无）'}

客户问题：
{question}

输出要求：
1. 只输出可直接发给客户的纯文本，不要 JSON、不要解释、不要 Markdown 标题。
2. {skill.get('reply_hint') or '按该技能规范输出业务结论。'}
3. 不显示主订单号、数据库字段名、表名、SQL、内部编号、同步时间、工具名。
4. 查询不到数据时，只说明未查到，并给出客户下一步可提供的信息，不要编造。
5. 不承诺到达时间与赔付结果。
""".strip()

    def _call_cli(self, skill_name: str, prompt: str) -> str:
        block = self.block
        node = Path(str(block.get("node_path") or ""))
        cli = Path(str(block.get("codebuddy_cli") or ""))
        mcp_config = Path(str(block.get("mcp_config", self.config.get("mcp_config", ""))))
        if not node.is_file() or not cli.is_file() or not mcp_config.is_file():
            return ""
        env = dict(os.environ)
        config_dir = str(block.get("workbuddy_config_dir") or "")
        if config_dir:
            env.update(CODEBUDDY_CONFIG_DIR=config_dir, WORKBUDDY_CONFIG_DIR=config_dir)
        if block.get("skill_dir"):
            env["CODEBUDDY_SKILL_DIR"] = str(block["skill_dir"])
        env.update(
            CODEBUDDY_PROJECT_DIR=str(self.workspace),
            CODEBUDDY_SKIP_GIT_BASH_CHECK="1",
            PYTHONUTF8="1",
        )
        command = [
            str(node),
            str(cli),
            "--print",
            "--output-format",
            "json",
            "--permission-mode",
            str(block.get("permission_mode", "bypassPermissions")),
            "--max-turns",
            str(self.max_turns),
            "--mcp-config",
            str(mcp_config),
            "--strict-mcp-config",
            "--add-dir",
            str(self.workspace),
        ]
        try:
            done = subprocess.run(
                command,
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=self.cli_timeout,
                cwd=str(self.workspace),
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        self._log(skill_name, done.returncode, done.stdout, done.stderr)
        if done.returncode != 0:
            return ""
        return extract_result(done.stdout)

    def _log(self, skill_name: str, code: int, stdout: str, stderr: str) -> None:
        log_dir = self.workspace / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{skill_name}"
        try:
            (log_dir / f"{stem}.stdout.json").write_text(stdout, encoding="utf-8")
            (log_dir / f"{stem}.stderr.log").write_text(
                f"exit={code}\n{stderr}", encoding="utf-8"
            )
        except OSError:
            pass


def extract_result(stdout: str) -> str:
    try:
        parsed = json.loads(stdout)
    except ValueError:
        return ""
    items = parsed if isinstance(parsed, list) else [parsed]
    result = next(
        (item for item in items if isinstance(item, dict) and item.get("type") == "result"),
        None,
    )
    if not result or result.get("is_error") is True:
        return ""
    text = str(result.get("result") or "").strip()
    if not text:
        return ""
    return text[:1900]
