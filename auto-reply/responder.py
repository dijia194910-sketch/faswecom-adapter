#!/usr/bin/env python
"""直连版回复引擎。

知识来源：IMA 客服知识库（MCP over HTTP）
决策与措辞：DeepSeek Chat Completions
发送通道：faswecom MCP over HTTP

不依赖 WorkBuddy CLI，单次回复耗时通常在 3～10 秒。
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

RISK_PATTERN = re.compile(
    r"投诉|举报|12315|消协|起诉|律师|法院|赔偿|退款|退货|退费|"
    r"曝光|媒体|差评威胁|密码|验证码|身份证|银行卡|转账|"
    r"发票抬头|特批|特殊申请|保证|承诺|一定|必须今天"
)

# 客户主动要人工：整句等于下列词，或明确出现“转人工/转接人工”
HUMAN_EXACT = {
    "人工", "真人", "人呢", "真人呢", "人工呢", "客服呢", "客服", "人工客服",
    "找人工", "要人工", "转人工", "转人工客服", "人工服务",
}
HUMAN_SUBSTR = re.compile(r"转.{0,3}人工|转接.{0,2}人工")
# 「真人呢」「没有真人吗」「有真人吗」「人工在不在」这类追问，也算要人工
HUMAN_ASK = re.compile(r"(真人|人工|客服).{0,6}(呢|吗|嘛|在不在|有没有|没有)")
# 出现这些词说明在打听信息，不是要求接人工
HUMAN_ASK_BLOCK = re.compile(r"电话|号码|热线|收费|费用|价格|多少钱|时间|几点|上班|工作时间|怎么联系")

# 寒暄与口头语：给引导语，不进转人工队列
SOCIAL_WORDS = {
    "你好", "您好", "老板好", "在吗", "在不在", "在的", "在么", "有人吗",
    "哈喽", "hello", "hi", "嗨", "早上好", "中午好", "下午好", "晚上好", "早",
    "谢谢", "谢谢你", "感谢", "好的", "好", "行", "收到", "嗯", "嗯嗯", "哦",
    "ok", "okay", "拜拜", "再见", "麻烦了", "辛苦", "辛苦啦",
}

# 长词优先，避免「好的」被「好」抢先剥掉
_SOCIAL_SORTED = tuple(sorted(SOCIAL_WORDS, key=len, reverse=True))

WELCOME_REPLY = "你好，我是客服助手，有什么问题呀~"
WELCOME_AGAIN = "在的，您说~"
# 客户已在人工队列里，打招呼时别再发菜单
WELCOME_WAITING = "在的，人工正在帮您跟进，请稍等片刻哈~"
THANKS_REPLY = "好的，有需要随时找我。"

# 转人工（主动要、被动兜底）统一这一句：先告诉客户正在转，再推人工
AGENT_REQUEST_REPLY = "好的，现在帮您转接人工处理，请稍等片刻哈~"
NOTICE_REPLY = AGENT_REQUEST_REPLY

# 长文本（订单明细等）按拟人节奏输入需要几十秒，先回一句让客户知道在处理
LONG_REPLY_ACK = "好的，请稍等，正在帮您查询~"
LONG_REPLY_ACK_MIN = 200

# 给了运单号之后再补一条查询指引
LOGISTICS_TIP = (
    "详细的物流信息您可以通过：微信小程序搜索快递100，根据发给您的运单号进行查询哦~"
)

# 推送真实送达后才这么说
HANDOFF_OK_REPLY = "当前问题我帮您转人工，请稍等片刻哈~"

THANKS_WORDS = {
    "谢谢", "谢谢你", "感谢", "好的", "好", "行", "收到", "嗯", "嗯嗯", "哦",
    "ok", "okay", "麻烦了", "辛苦", "辛苦啦", "拜拜", "再见",
}


def _plain(text: str) -> str:
    return re.sub(r"[\s，。,.!！?？~～「」]+", "", str(text or "")).lower()


def wants_human(text: str) -> bool:
    """强规则：客户明确要人工才判 True。

    三类都算：整句就是「人工/真人/人呢」、句中出现「转人工」、
    以及「真人呢」「没有真人吗」这种追问。
    """
    raw = _plain(text)
    if not raw:
        return False
    if raw in HUMAN_EXACT:
        return True
    if HUMAN_SUBSTR.search(raw):
        return True
    if HUMAN_ASK.search(raw) and not HUMAN_ASK_BLOCK.search(raw):
        return True
    return False


def social_reply(text: str) -> tuple[str, bool]:
    """返回（回复文本, 是否欢迎菜单）。感谢类不复述菜单。"""
    raw = _plain(text)
    if raw.rstrip("啊呀哦呢哈啦吧嘛") in THANKS_WORDS:
        return THANKS_REPLY, False
    # 「好的收到」「谢谢啦辛苦」这类全由确认词拼成的，也当致谢
    if _only_words_from(raw, THANKS_WORDS):
        return THANKS_REPLY, False
    return WELCOME_REPLY, True


def _only_words_from(raw: str, vocab: set[str]) -> bool:
    """整句能否被 vocab 里的词完整切分（不残留任何其他字）。"""
    if not raw:
        return False
    rest = raw
    for _ in range(8):
        if not rest:
            return True
        hit = ""
        for word in sorted(vocab, key=len, reverse=True):
            if rest.startswith(word):
                hit = word
                break
        if not hit:
            return False
        rest = rest[len(hit):]
    return not rest


def is_social(text: str) -> bool:
    """寒暄、口头语、空内容：不该走业务链路，也不该占人工队列。

    支持组合说法：「你好，在吗」「在吗你好」「你好在吗在不在」。
    做法是整句只由寒暄词拼成就判定为寒暄；只要剥出任何业务内容就不算。
    """
    raw = _plain(text)
    if not raw:
        return True
    if raw in SOCIAL_WORDS:
        return True
    if raw.rstrip("啊呀哦呢哈啦吧嘛") in SOCIAL_WORDS:
        return True
    if len(raw) > 16:
        return False
    return _social_only(raw)


def _social_only(raw: str) -> bool:
    """把整句按寒暄词从左往右剥，剥空即纯寒暄。"""
    rest = raw
    for _ in range(8):
        if not rest:
            return True
        for word in _SOCIAL_SORTED:
            if rest.startswith(word):
                rest = rest[len(word):]
                break
        else:
            return False
    return not rest


CLOSING_MARKERS = (
    "谢谢", "谢啦", "感谢", "辛苦", "麻烦您了", "麻烦你了",
    "好的", "好嘞", "好哒", "行了", "可以了", "不用了", "不需要了",
    "收到", "嗯嗯", "知道了", "明白了", "了解了", "没事了", "没问题了",
    "解决了", "已解决", "搞定", "结束", "先这样", "就这样",
    "拜拜", "再见", "晚安", "回头聊", "ok",
)
# 出现这些词说明客户还在提新问题，不能当结束语
CLOSING_BLOCKERS = (
    "但是", "不过", "可是", "然后", "还有", "另外", "而且", "另外",
    "请问", "问一下", "问下", "想问", "怎么", "为什么", "如何", "哪里",
    "帮我", "麻烦帮我", "能不能", "可以帮",
)


def is_closing(text: str) -> bool:
    """客户的结束语：含道谢/确认/收尾词、整句很短、且没有转折或新问题。"""
    raw = _plain(text)
    if not raw or len(raw) > 12:
        return False
    lowered = raw.lower()
    if any(block in lowered for block in CLOSING_BLOCKERS):
        return False
    return any(marker in lowered for marker in CLOSING_MARKERS)


def should_send_menu(workspace: Path, conversation_id: str, cooldown: int) -> bool:
    """同一会话在冷却期内只回一次欢迎菜单，避免连说两次“你好”收到两遍。"""
    if cooldown <= 0:
        return True
    path = workspace / "welcome-state.json"
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text("utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (ValueError, OSError):
            data = {}
    now = time.time()
    last = float(data.get(conversation_id) or 0)
    if now - last < cooldown:
        return False
    data[conversation_id] = now
    data = {key: value for key, value in data.items() if now - float(value or 0) < 86400}
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    return True


def waiting_for_agent(workspace: Path, conversation_id: str) -> bool:
    """该客户是否还在人工队列里（有未结工单）。"""
    try:
        import handoff  # noqa: PLC0415

        return handoff.open_ticket(workspace, conversation_id) is not None
    except Exception:  # noqa: BLE001
        return False


SYSTEM_PROMPT = """你是广州爱美熊网络科技有限公司的客服回复助手，服务对象是美团团购与美团外卖商家客户。
你必须严格依据给定的《客服知识库原文》作答，规则如下：

1. 只输出 JSON，不要输出任何解释性文字。
2. 若知识库中存在与客户问题实质一致的标准问题（含相似问法），decision 填 AUTO：
   - reply_text 使用该标准问题的默认回答，可以做口语化润色，但不得改变操作步骤、入口名称、数字、时效与事实。
   - matched_question 填命中的标准问题原文。
3. 若需要拼接两条以上知识、知识库只覆盖问题的一部分、或客户问题本身有歧义需要核实订单/账户，decision 填 DRAFT。
4. 若涉及投诉、退款争议、赔偿、法律责任、监管、账号密码验证码、要求特批价格或时限承诺，decision 填 ESCALATE。
5. 知识库没有对应内容时，decision 填 ESCALATE，不得编造。
6. 不得声称已查询客户账户、订单、库存或后台系统。
7. reply_text 为可直接发给客户的中文纯文本，可用 1～4 个短步骤，不使用 Markdown 标题、不用表情符号。

JSON 字段：decision、reply_text、reason、matched_question、confidence（0~1 的小数）。"""


class McpError(RuntimeError):
    pass


class Mcp:
    """极简 MCP streamable-HTTP 客户端（JSON-RPC over HTTP，兼容 SSE 响应）。"""

    def __init__(self, url: str, headers: dict[str, str] | None = None, timeout: int = 45):
        self.url = url
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if headers:
            self.headers.update(headers)
        self.timeout = timeout
        self.session_id: str | None = None
        self._seq = 0
        self._initialized = False

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = dict(self.headers)
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        request = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"), headers=headers
        )
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
            raw = response.read().decode("utf-8", "replace")
            self.session_id = response.headers.get("mcp-session-id") or self.session_id
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            raise McpError(f"HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise McpError(f"无法连接 {self.url}: {exc.reason}") from exc

        for line in raw.splitlines():
            if line.startswith("data: "):
                chunk = line[6:].strip()
                if chunk:
                    return json.loads(chunk)
        if not raw.strip():
            return {}
        return json.loads(raw)

    def initialize(self) -> None:
        if self._initialized:
            return
        self._seq += 1
        self._post(
            {
                "jsonrpc": "2.0",
                "id": self._seq,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "workbuddy-wecom-live", "version": "2.0"},
                },
            }
        )
        self._seq += 1
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        self._initialized = True

    def tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.initialize()
        self._seq += 1
        payload = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._seq,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        if "error" in payload:
            raise McpError(f"{name} 调用失败：{payload['error']}")
        result = payload.get("result") or {}
        if result.get("isError"):
            raise McpError(f"{name} 返回错误：{result}")
        content = result.get("content") or []
        for item in content:
            if item.get("type") == "text":
                text = str(item.get("text") or "")
                try:
                    return json.loads(text)
                except ValueError:
                    return text
        return result


# ---------------------------------------------------------------- IMA 知识库


class KnowledgeBase:
    def __init__(self, config: dict[str, Any], cache_dir: Path):
        ima = config.get("ima") or {}
        token = read_ima_token(ima)
        if not token:
            raise McpError("未读取到 IMA 连接器凭据")
        self.client = Mcp(
            str(ima.get("url", "https://ima.qq.com/mcp")),
            {"Authorization": token},
        )
        self.kb_id = str((config.get("knowledge_base") or {}).get("id", ""))
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = int(config.get("kb_cache_seconds", 900))

    def search(self, query: str) -> list[dict[str, Any]]:
        data = self.client.tool(
            "search_knowledge", {"knowledge_base_id": self.kb_id, "query": query}
        )
        items = (data or {}).get("searched_knowledge_list") or []
        hits = []
        for item in items:
            knowledge = item.get("knowledge") or {}
            if knowledge.get("media_id"):
                hits.append(
                    {
                        "media_id": knowledge["media_id"],
                        "title": knowledge.get("title", ""),
                        "introduction": knowledge.get("introduction", ""),
                    }
                )
        return hits

    def content(self, media_id: str, title: str) -> str:
        path = self.cache_dir / f"{media_id}.txt"
        if path.is_file() and time.time() - path.stat().st_mtime < self.ttl:
            return path.read_text("utf-8")
        data = self.client.tool("fetch_media_content", {"media_id": media_id})
        if isinstance(data, dict):
            text = str(
                data.get("content")
                or data.get("text")
                or data.get("markdown")
                or json.dumps(data, ensure_ascii=False)
            )
        else:
            text = str(data)
        path.write_text(text, encoding="utf-8")
        return text


def read_ima_token(ima: dict[str, Any]) -> str:
    token = str(ima.get("token") or "")
    if not token:
        state_file = Path(str(ima.get("state_file", "")))
        if not state_file.is_file():
            return ""
        try:
            states = json.loads(state_file.read_text("utf-8"))
            token = (
                states.get("headerOverrides", {})
                .get(str(ima.get("key", "ima-mcp")), {})
                .get("Authorization", "")
            )
        except (ValueError, OSError):
            return ""
    token = str(token)
    if token and not token.lower().startswith("bearer "):
        token = f"Bearer {token}"
    return token


# ---------------------------------------------------------------- 模型决策


def adapter_llm() -> dict[str, Any] | None:
    """跟随适配器「模型与配置」里当前使用的模型，实现一键切换。"""
    try:
        import sys as _sys

        root = "G:\\适配器"
        if root not in _sys.path:
            _sys.path.insert(0, root)
        from chatlocal.wecom_models import ModelCatalog

        catalog = ModelCatalog(Path(root) / "data" / "wecom-models.json")
        data = catalog.read()
        active = data.get("active")
        if not active:
            return None
        entry = next((m for m in data.get("models", []) if m.get("id") == active), None)
        if not entry:
            return None
        key = catalog.key(data, entry.get("credential_id") or entry["provider"])
        if not key:
            return None
        base = str(entry["api_base"]).rstrip("/")
        return {
            "url": base + "/chat/completions",
            "model": str(entry["model"]),
            "key": key,
            "name": str(entry.get("name") or ""),
        }
    except Exception:
        return None


class Brain:
    def __init__(self, config: dict[str, Any]):
        llm = config.get("llm") or {}
        self.url, self.model, self.key = llm_target(config)
        if not self.key:
            raise McpError("未读取到大模型 API Key")
        self.max_tokens = int(llm.get("max_tokens", 1200))
        self.timeout = int(llm.get("timeout_seconds", 60))

    def ask(self, kb_text: str, question: str, history: str) -> dict[str, Any]:
        user_block = (
            f"【客服知识库原文】\n{kb_text[:14000]}\n\n"
            f"【最近对话】\n{history[:1200]}\n\n"
            f"【客户最新问题】\n{question}\n\n"
            "请按系统规则输出 JSON。"
        )
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_block},
            ],
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.key}",
            },
        )
        try:
            raw = urllib.request.urlopen(request, timeout=self.timeout).read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise McpError(f"模型接口 HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise McpError(f"模型接口不可达：{exc.reason}") from exc
        payload = json.loads(raw)
        text = str(payload["choices"][0]["message"].get("content") or "").strip()
        return parse_json(text)


def read_llm_key(llm: dict[str, Any]) -> str:
    inline = str(llm.get("api_key") or "")
    if inline:
        return inline
    models_file = Path(str(llm.get("models_file", "")))
    model_id = str(llm.get("model", ""))
    if not models_file.is_file():
        return ""
    try:
        models = json.loads(models_file.read_text("utf-8"))
    except (ValueError, OSError):
        return ""
    for item in models if isinstance(models, list) else []:
        if isinstance(item, dict) and item.get("id") == model_id:
            return str(item.get("apiKey") or "")
    return ""


def llm_target(config: dict[str, Any]) -> tuple[str, str, str]:
    """解析自动回复用的大模型：默认跟随适配器当前模型，失败回退 WorkBuddy 配置。"""
    llm = config.get("llm") or {}
    source = str(llm.get("source") or "adapter").lower()
    if source != "workbuddy":
        adapter = adapter_llm()
        if adapter:
            return adapter["url"], adapter["model"], adapter["key"]
    return (
        str(llm.get("url", "https://api.deepseek.com/chat/completions")),
        str(llm.get("model", "deepseek-v4-flash")),
        read_llm_key(llm),
    )


def parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except ValueError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else {}
        except ValueError:
            return {}
    return {}


SUMMARY_SYSTEM = """你是客服工单助手。根据客户最近对话，用 2 到 3 句中文说清两件事：
1) 客户在问什么、想解决什么问题；
2) 已经给过什么答复、客户为什么还需要人工。
要求：只输出摘要正文，不要标题、不要编号、不要 markdown；不超过 120 字；不得编造对话里没有的信息；不确定的地方不要写。"""


def summarize_conversation(config: dict[str, Any], lines: list[str], question: str = "") -> str:
    """把最近对话压成工单摘要。失败返回空串，由调用方降级。"""
    if not lines and not question:
        return ""
    llm = config.get("llm") or {}
    url, model, key = llm_target(config)
    if not key:
        return ""
    body_text = chr(10).join(lines[-20:]) or question
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SUMMARY_SYSTEM},
            {"role": "user", "content": f"【最近对话】\n{body_text[:4000]}\n\n请输出摘要。"},
        ],
        "max_tokens": 260,
        "temperature": 0.2,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        raw = urllib.request.urlopen(request, timeout=int(llm.get("timeout_seconds", 60))).read()
        data = json.loads(raw.decode("utf-8"))
        text = str(data["choices"][0]["message"].get("content") or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    return re.sub(r"\s+", " ", text)[:200]


GENTLE_SYSTEM = """你是广州爱美熊的客服助手。客户刚说了结束语（谢谢 / 好的 / 收到 / 再见之类）。
用一句话温柔体贴地回应：先自然接住对方的谢意或确认，再轻轻留一句“随时来找我”。
要求：不超过 40 字；像真人客服说话，不要“感谢您的咨询”这类模板腔；不用感叹号堆叠；不用 emoji。"""


def gentle_reply(config: dict[str, Any], text: str) -> str:
    """结束语用模型生成一句温柔回应，失败返回空串。"""
    llm = config.get("llm") or {}
    url, model, key = llm_target(config)
    if not key:
        return ""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": GENTLE_SYSTEM},
            {"role": "user", "content": f"客户说：{str(text)[:200]}"},
        ],
        "max_tokens": 300,
        "temperature": 0.8,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        raw = urllib.request.urlopen(request, timeout=int(llm.get("timeout_seconds", 30))).read()
        data = json.loads(raw.decode("utf-8"))
        out = str(data["choices"][0]["message"].get("content") or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    out = out.strip().strip('"').replace(chr(10), " ")
    return out[:60]



# ---------------------------------------------------------------- faswecom


def faswecom_client(config: dict[str, Any]) -> Mcp:
    fa = config.get("faswecom") or {}
    url = str(fa.get("url", ""))
    token = str(fa.get("token", ""))
    if not token:
        mcp_json = Path(str(config.get("mcp_config", "")))
        if mcp_json.is_file():
            try:
                servers = json.loads(mcp_json.read_text("utf-8")).get("mcpServers", {})
                bridge = servers.get("faswecom-live") or {}
                env = bridge.get("env") or {}
                token = str(env.get("FASWECOM_MCP_TOKEN") or "")
                url = url or str(env.get("FASWECOM_MCP_URL") or "")
            except (ValueError, OSError):
                pass
    if not url or not token:
        raise McpError("未配置 faswecom 连接信息")
    # 长文本按拟人节奏输入可能需要几十秒，客户端超时放宽到 240 秒
    return Mcp(url, {"Authorization": f"Bearer {token}"}, timeout=240)


def ai_takeover(config: dict[str, Any]) -> bool:
    """适配器操作台的“智能客服接管”总开关。

    关闭时 watcher 只记录不回复，把客户消息留给人工；读取失败按接管处理，
    避免适配器一次没连上就把自动回复整个停掉。
    """
    try:
        client = faswecom_client(config)
        data = client.tool("get_status", {}) or {}
        return data.get("ai_takeover") is not False
    except Exception:  # noqa: BLE001
        return True


def resolve_recipient(client: Mcp, name: str) -> tuple[str | None, str]:
    data = client.tool("search_wecom_recipients", {"query": name})
    candidates = (data or {}).get("candidates") or []
    exact = [c for c in candidates if str(c.get("name")) == name and c.get("kind") == "private"]
    if len(exact) == 1:
        return str(exact[0]["id"]), ""
    if len(candidates) == 1 and candidates[0].get("kind") == "private":
        return str(candidates[0]["id"]), ""
    return None, f"接收人不唯一或非私聊（候选 {len(candidates)} 个）"


SEND_PENDING = {"PENDING", "QUEUED", "SENDING", "DISPATCHED", "SERVER_ACCEPTED"}


def _await_dispatch(client: Mcp, dispatch_id: str, timeout: float) -> str:
    """等一条发送单落到终态。PENDING 不是失败，别当没发。"""
    deadline = time.time() + max(0.0, timeout)
    state = ""
    while time.time() < deadline:
        try:
            receipt = client.tool("get_wecom_operation", {"dispatch_id": dispatch_id})
        except McpError:
            return state
        state = str((receipt or {}).get("state") or state)
        if state and state not in SEND_PENDING:
            return state
        time.sleep(1.5)
    return state


_SEND_SEQ = 0


def ack_before_long(config: dict[str, Any], conversation: str, trigger: dict[str, Any],
                    client: Mcp | None = None) -> None:
    """正式发送长文本之前，先回一句“请稍等”，避免客户长时间收不到任何消息。"""
    try:
        fa = client or faswecom_client(config)
        recipient_id, _ = resolve_recipient(fa, conversation)
        if not recipient_id:
            return
        text = LONG_REPLY_ACK + str(config.get("ai_suffix", "（AI回复）"))
        send_reply(fa, recipient_id, text, int(trigger.get("id") or 0),
                   int(trigger.get("timestamp") or 0))
    except Exception:  # noqa: BLE001
        pass


def send_reply(
    client: Mcp, recipient_id: str, text: str, message_id: int, timestamp_ms: int
) -> dict[str, Any]:
    """发送一条客户可见消息。

    幂等键必须每次唯一：同一条客户消息若因超时被重试，复用旧键会让适配器
    直接返回那条卡在 PENDING 的单，消息永远发不出去。
    """
    global _SEND_SEQ
    _SEND_SEQ += 1
    key = f"wecom-live-{message_id}-{timestamp_ms}-{_SEND_SEQ}"
    dispatch = client.tool(
        "send_wecom_message",
        {"recipient_id": recipient_id, "text": text, "idempotency_key": key},
    )
    dispatch_id = str((dispatch or {}).get("dispatch_id") or "")
    state = str((dispatch or {}).get("state") or "")
    if dispatch_id and state in SEND_PENDING | {""}:
        state = _await_dispatch(client, dispatch_id, 10.0) or state
    return {"dispatch_id": dispatch_id, "state": state, "key": key}


# ---------------------------------------------------------------- 主入口


def history_text(client: Mcp, conversation_id: str, limit: int = 8) -> str:
    try:
        data = client.tool(
            "read_conversation",
            {"platform": "wecom", "conversation_id": conversation_id, "limit": limit},
        )
    except McpError:
        return ""
    messages = (data or {}).get("messages") or []
    lines = []
    for item in messages[-limit:]:
        who = "客户" if not item.get("is_self") else "我方"
        content = str(item.get("content") or "").strip()
        if content:
            lines.append(f"{who}：{content}")
    return "\n".join(lines)


def make_router(config: dict[str, Any], workspace: Path):
    """技能路由层；文件缺失或异常时降级为不启用，不影响知识库链路。"""
    try:
        import skill_router  # noqa: PLC0415
    except ImportError:
        return None
    try:
        return skill_router.SkillRouter(config, workspace)
    except Exception:  # noqa: BLE001
        return None


def kb_search(config: dict[str, Any], workspace: Path, question: str) -> tuple[list[dict[str, Any]], str]:
    kb = KnowledgeBase(config, workspace / "kb-cache")
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    variants = [question, question[:20], re.sub(r"[？?！!。，,]", " ", question)[:20]]
    for query in variants:
        query = query.strip()
        if not query:
            continue
        for hit in kb.search(query):
            if hit["media_id"] not in seen:
                seen.add(hit["media_id"])
                hits.append(hit)
        if len(hits) >= 2:
            break
    blocks = []
    for hit in hits[:2]:
        body = kb.content(hit["media_id"], hit["title"])
        blocks.append(f"《{hit['title']}》" + chr(10) + body)
    return hits, (chr(10) + chr(10)).join(blocks)


def finish_send(
    result: dict[str, Any],
    reply: str,
    client: Mcp | None,
    config: dict[str, Any],
    mode: str,
    suffix: str,
    conversation: str,
    trigger: dict[str, Any],
) -> dict[str, Any]:
    text = reply.strip()
    if not text:
        result["decision"] = "ESCALATE"
        result["reason"] = result["reason"] or "无可用回复文本"
        return result
    text = text if text.endswith(suffix) else f"{text}{suffix}"
    text = text[:1990]
    result["reply_text"] = text

    if str(result.get("decision")) != "AUTO" and not result.get("send_notice"):
        return result
    if mode != "live":
        result["send_state"] = "DRY_RUN"
        return result

    fa = client or faswecom_client(config)
    recipient_id, note = resolve_recipient(fa, conversation)
    if not recipient_id:
        result["decision"] = "ESCALATE"
        result["reason"] = note or "接收人无法唯一识别"
        return result
    result["recipient_id"] = recipient_id
    receipt = send_reply(fa, recipient_id, text, int(trigger["id"]), int(trigger["timestamp"]))
    result["dispatch_id"] = receipt["dispatch_id"]
    result["send_state"] = receipt["state"] or "UNKNOWN"
    return result


def dispatch_route(
    config: dict[str, Any],
    workspace: Path,
    conversation: str,
    conversation_id: str,
    question: str,
    route: Any,
    router: Any,
    result: dict[str, Any],
    history: str,
    mode: str,
    suffix: str,
    trigger: dict[str, Any],
    client: Mcp | None = None,
) -> dict[str, Any]:
    skill = route.skill or {}
    result["route"] = route.kind
    result["skill"] = str(skill.get("id") or "")
    result["reason"] = result["reason"] or route.reason

    if route.kind in ("need_slot", "ambiguous"):
        result.update(decision="AUTO", confidence=0.9, reply_text=route.prompt)
        return finish_send(result, route.prompt, client, config, mode, suffix, conversation, trigger)

    if route.kind == "order_reply":
        # 客户确认的订单本身有运单号：直接把物流信息发过去
        result.update(decision="AUTO", confidence=0.92, reply_text=route.prompt)
        return finish_send(result, route.prompt, client, config, mode, suffix, conversation, trigger)

    if route.kind == "handoff":
        # 需要人工核实（未记录运单号 / 序号反复无效）：先回话，再走转人工链路
        result.update(decision="ESCALATE", reason=route.reason or "需人工核实",
                      escalation_reply=route.prompt)
        return result

    if route.kind == "skill":
        text = router.execute(route, question, history, conversation, conversation_id)
        if not text:
            fallback = str(skill.get("fallback_reply") or "").strip()
            if fallback and getattr(router, "last_backend", "") == "queue":
                result.update(
                    decision="AUTO",
                    confidence=0.7,
                    reply_text=fallback,
                    reason=f"技能 {skill.get('skill', '')} 已入队，执行通道待接通",
                )
                return finish_send(
                    result, fallback, client, config, mode, suffix, conversation, trigger
                )
            result.update(
                decision="ESCALATE",
                reason=f"技能 {skill.get('skill', '')} 未返回可用结果",
            )
            return result
        if len(text) >= LONG_REPLY_ACK_MIN and mode == "live":
            # 长文本输入要几十秒，先让客户知道在查
            ack_before_long(config, conversation, trigger, client)
        result.update(decision="AUTO", confidence=0.9, reply_text=text)
        return finish_send(result, text, client, config, mode, suffix, conversation, trigger)

    result.update(decision="ESCALATE", reason=route.reason or "无法路由")
    return result


WAYBILL_PATTERN = re.compile(r"运单号\s*([0-9A-Za-z]{6,})")


def _has_waybill(text: Any) -> bool:
    """回复里是否真的给了运单号（「运单号 未记录」不算）。"""
    return bool(WAYBILL_PATTERN.search(str(text or "")))


def _deliver_followup(
    config: dict[str, Any],
    conversation: str,
    trigger: dict[str, Any],
    text: str,
    result: dict[str, Any],
) -> None:
    """在首条回复之后补发一条。失败不影响主回复。"""
    mode = str(config.get("mode", "dry_run"))
    if mode != "live":
        result["followup_state"] = "DRY_RUN"
        return
    suffix = str(config.get("ai_suffix", "（AI回复）"))
    body = text if text.endswith(suffix) else f"{text}{suffix}"
    try:
        fa = faswecom_client(config)
        recipient_id, _note = resolve_recipient(fa, conversation)
        if not recipient_id:
            result["followup_state"] = "NO_RECIPIENT"
            return
        # 首条刚发完，两条挨太近适配器会把后一条挂起
        gap = float((config.get("skills") or {}).get("followup_gap_seconds", 1.5))
        if gap > 0:
            time.sleep(gap)
        receipt = send_reply(
            fa,
            recipient_id,
            body,
            int(trigger["id"]),
            int(trigger["timestamp"]) + 1,
        )
        result["followup_state"] = receipt.get("state") or "UNKNOWN"
        result["followup_dispatch_id"] = receipt.get("dispatch_id") or ""
    except Exception as exc:  # noqa: BLE001
        result["followup_state"] = f"ERROR:{type(exc).__name__}"


def handle(
    config: dict[str, Any],
    workspace: Path,
    conversation: str,
    conversation_id: str,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """对外入口：转人工必须先给客户回话，再推人工客服。

    顺序不能反：适配器一次只处理一条发送请求，两条并发时后发的会一直
    停在 PENDING（等待适配器接收请求），客户就收不到任何回复。
    """
    result = _handle(config, workspace, conversation, conversation_id, messages)

    # 给了运单号，再补一条自助查询指引
    if str(result.get("decision")) == "AUTO" and _has_waybill(result.get("reply_text")):
        result["followup_text"] = LOGISTICS_TIP
        _deliver_followup(config, conversation, messages[-1], LOGISTICS_TIP, result)

    if str(result.get("decision")) != "ESCALATE":
        return result

    # 1) 先把客户这句回出去，绝不让人工推送拖住客户
    if result.get("escalation_reply"):
        result["reply_text"] = str(result["escalation_reply"])
    elif str(result.get("reason") or "").startswith("客户明确要求人工"):
        result["reply_text"] = AGENT_REQUEST_REPLY
    else:
        result["reply_text"] = NOTICE_REPLY
    result["send_notice"] = True
    result = finish_send(
        result,
        str(result["reply_text"]),
        None,
        config,
        str(config.get("mode", "dry_run")),
        str(config.get("ai_suffix", "（AI回复）")),
        conversation,
        messages[-1],
    )

    # 2) 再推人工客服；失败只影响后台，不影响客户侧
    record: dict[str, Any] = {}
    try:
        import handoff  # noqa: PLC0415

        # 适配器的发送要占用企微窗口，两条请求挨太近后一条会卡在 PENDING
        gap = float((config.get("handoff") or {}).get("send_gap_seconds", 2.5))
        if gap > 0:
            # 客户回复与转人工通知之间的间隔也随机化，避免固定节奏
            time.sleep(gap + random.uniform(0.6, 2.2))
        record = handoff.notify(
            config, workspace, conversation, conversation_id, messages,
            str(result.get("reason") or ""),
        ) or {}
    except Exception as exc:  # noqa: BLE001
        record = {"state": "ERROR", "error": f"{type(exc).__name__}: {exc}"[:200]}
    result["handoff"] = record
    return result


def _handle(
    config: dict[str, Any],
    workspace: Path,
    conversation: str,
    conversation_id: str,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    mode = str(config.get("mode", "dry_run"))
    suffix = str(config.get("ai_suffix", "（AI回复）"))
    min_conf = float(config.get("min_confidence", 0.8))
    draft_min = float(config.get("draft_min_confidence", 0.6))
    question = chr(10).join(str(m["content"]).strip() for m in messages[-3:]).strip()
    trigger = messages[-1]
    result: dict[str, Any] = {
        "decision": "ESCALATE",
        "send_state": "NOT_SENT",
        "dispatch_id": "",
        "reply_text": "",
        "reason": "",
        "matched_question": "",
        "confidence": 0.0,
        "recipient_id": "",
        "route": "",
        "skill": "",
    }

    # G0 红线：关键词拦截，不检索、不路由
    if RISK_PATTERN.search(question):
        result["reason"] = "命中高风险词，转人工"
        return result

    # 客户主动要人工：不再检索与路由
    if wants_human(question):
        result.update(decision="ESCALATE", reason="客户明确要求人工", reply_text=NOTICE_REPLY)
        return result

    router = make_router(config, workspace)

    # G2 槽位续填：上一轮已在等签约人姓名，本轮内容按槽位处理
    if router is not None and router.pending(conversation_id):
        route = router.route(conversation_id, question)
        if route.kind != "none":
            return dispatch_route(
                config, workspace, conversation, conversation_id, question,
                route, router, result, "", mode, suffix, trigger,
            )
        result["reason"] = route.reason or "槽位收集失败"

    # 客户致谢/结束语：温柔回应并结单
    if is_closing(question):
        soft = gentle_reply(config, question)
        reply = soft or THANKS_REPLY
        try:
            import handoff  # noqa: PLC0415

            if handoff.close_ticket(workspace, conversation_id, "客户致谢，会话结束"):
                result["ticket_closed"] = True
        except Exception:  # noqa: BLE001
            pass
        result.update(
            decision="AUTO",
            route="closing",
            confidence=0.95,
            reason="客户结束语，温柔回应并结单",
            reply_text=reply,
        )
        return finish_send(
            result, reply, None, config, mode, suffix, conversation, trigger
        )

    # 寒暄与口头语：给引导语即可，不进转人工队列
    if is_social(question):
        reply, is_menu = social_reply(question)
        cooldown = int((config.get("skills") or {}).get("welcome_cooldown_seconds", 600))
        if is_menu and waiting_for_agent(workspace, conversation_id):
            # 客户已在人工队列里，别再发能力菜单
            reply = WELCOME_WAITING
        elif is_menu and not should_send_menu(workspace, conversation_id, cooldown):
            reply = WELCOME_AGAIN
        result.update(
            decision="AUTO",
            route="greeting",
            confidence=0.95,
            reason="寒暄或口头语，回复引导语",
            reply_text=reply,
        )
        return finish_send(
            result, reply, None, config, mode, suffix, conversation, trigger
        )

    # G1 知识库优先
    try:
        hits, kb_text = kb_search(config, workspace, question)
    except McpError as exc:
        result["reason"] = f"知识库检索失败：{exc}"
        return result

    if hits:
        fa = faswecom_client(config)
        history = history_text(fa, conversation_id)
        answer = Brain(config).ask(kb_text, question, history)
        decision = str(answer.get("decision") or "").upper()
        reply = str(answer.get("reply_text") or "").strip()
        confidence = float(answer.get("confidence") or 0)
        result.update(
            decision=decision or "ESCALATE",
            reply_text=reply,
            reason=str(answer.get("reason") or "")[:300],
            matched_question=str(answer.get("matched_question") or "")[:120],
            confidence=confidence,
        )
        if decision == "AUTO" and reply and confidence >= min_conf:
            result["route"] = "kb"
            return finish_send(result, reply, fa, config, mode, suffix, conversation, trigger)
        if router is not None:
            route = router.route(conversation_id, question)
            if route.kind in ("skill", "need_slot", "ambiguous"):
                result["reason"] = (
                    f"知识库未给出定论（{decision or 'EMPTY'}），转技能：{route.reason}"
                )
                return dispatch_route(
                    config, workspace, conversation, conversation_id, question,
                    route, router, result, history, mode, suffix, trigger, fa,
                )
        # 知识库部分覆盖：模型给了可用的待核实答复，先发给客户，别让对话断掉
        if decision == "DRAFT" and reply and confidence >= draft_min:
            result["route"] = "kb_draft"
            result["send_notice"] = True
            result["reason"] = (
                f"知识库部分覆盖（置信度 {confidence:.2f}），答复并留人工出口："
                f"{str(answer.get('reason') or '')[:180]}"
            )
            return finish_send(result, reply, fa, config, mode, suffix, conversation, trigger)
        # 其余情况一律转人工，交给入口统一话术
        result["decision"] = "ESCALATE"
        result["reason"] = (
            f"知识库未给出可自动答复的结论（{decision or 'EMPTY'}，置信度 {confidence:.2f}）："
            f"{str(answer.get('reason') or '')[:180]}"
        )
        return result

    # G2 知识库未命中 → 技能语义路由
    result["reason"] = "知识库未命中"
    if router is None:
        return result
    route = router.route(conversation_id, question)
    if route.kind == "none":
        result["reason"] = f"知识库未命中，且无技能匹配（{route.reason}）"
        return result
    return dispatch_route(
        config, workspace, conversation, conversation_id, question,
        route, router, result, "", mode, suffix, trigger,
    )
