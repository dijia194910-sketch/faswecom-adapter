#!/usr/bin/env python
"""导出企微联系人（id+名字）为 CSV，供绑定核对。"""
import csv, json, re, sys
from pathlib import Path

ADAPTER = Path(r"G:\适配器")
WS = Path(__file__).resolve().parent
OUT = WS / "bindings-data" / "review"

sys.path.insert(0, str(ADAPTER))
from chatlocal import wecom_windows as w
from chatlocal.wecom_reader import Reader
from chatlocal.wecom_quickdb import read as qread

ACCOUNT = "1688855540964207"


def history_names():
    """从历史事件里挖出'这个客户查过哪个签约人'。"""
    found = {}
    path = WS / "events.jsonl"
    if not path.is_file():
        return found
    pat = re.compile(r"([^\s（(]{2,20})（(?:近\s*\d+\s*天|全部历史)")
    for line in path.read_text("utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        cid = str(e.get("conversation_id") or "")
        text = str(e.get("reply_text") or "")
        m = pat.search(text)
        if cid and m:
            found.setdefault(cid, [])
            if m.group(1) not in found[cid]:
                found[cid].append(m.group(1))
    return found


def store_stats():
    import sqlite3
    stats = {}
    db = sqlite3.connect("file:" + str(ADAPTER / "data/chats.sqlite3") + "?mode=ro", uri=True)
    for r in db.execute("""SELECT conversation_id, COUNT(*), MAX(timestamp), MAX(conversation)
                           FROM messages WHERE platform='wecom' AND is_self=0
                           GROUP BY conversation_id"""):
        cid, count, last, title = r
        m = re.fullmatch(r"(\d+):S:(\d+)_(\d+)", cid or "")
        if not m:
            continue
        peer = m.group(3) if m.group(2) == m.group(1) else m.group(2)
        stats[peer] = {"count": count, "last": last, "conversation_id": cid, "title": title}
    db.close()
    return stats


def main():
    OUT.mkdir(exist_ok=True)
    store = ADAPTER / "data"
    src = w.select_source(w.discover(), ACCOUNT, store)
    reader = Reader(src, cache=store / "wecom-keys", temporary=store / "wecom-private")
    key = reader.key()
    users = qread(src / "user.db", key,
                  "SELECT id,name,mobile,external_corp_name,unionid,position,corp_id FROM user_table", ())
    hist = history_names()
    stats = store_stats()

    rows = []
    for u in users:
        peer = str(u["id"])
        external = bool(str(u["external_corp_name"] or "").strip() or str(u["unionid"] or "").strip())
        st = stats.get(peer) or {}
        cid = st.get("conversation_id", "")
        rows.append({
            "企微客户ID": peer,
            "企微显示名": str(u["name"] or ""),
            "会话显示名": str(st.get("title") or ""),
            "是否外部客户": "是" if external else "否",
            "企微资料手机号": str(u["mobile"] or ""),
            "unionid": str(u["unionid"] or ""),
            "会话ID": cid,
            "来消息数": st.get("count", 0),
            "最后来消息时间": (__import__("datetime").datetime.fromtimestamp(st["last"] / 1000).strftime("%Y-%m-%d %H:%M") if st.get("last") else ""),
            "历史查询过的签约人": "；".join(hist.get(cid, [])[:3]),
        })
    rows.sort(key=lambda r: (r["是否外部客户"] != "是", -(r["来消息数"] or 0)))
    path = OUT / "wecom-contacts.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"ok": True, "path": str(path), "rows": len(rows),
                      "external": sum(1 for r in rows if r["是否外部客户"] == "是"),
                      "with_history_name": sum(1 for r in rows if r["历史查询过的签约人"])},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
