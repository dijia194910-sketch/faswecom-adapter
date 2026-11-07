#!/usr/bin/env python
"""把企微联系人表和门店签约人表做匹配，生成“待确认绑定清单”。

用法:
  python bind_review.py                      # 用只读库里的门店表当对照
  python bind_review.py 同事的表.xlsx        # 用同事给的表（xlsx 需 openpyxl）
  python bind_review.py 同事的表.csv
"""
import csv, difflib, json, re, sys
from pathlib import Path

WS = Path(__file__).resolve().parent
OUT = WS / "bindings-data" / "review"


def norm(text):
    value = re.sub(r"@微信$", "", str(text or ""))
    return re.sub(r"[\s\-_·・,，.。()（）【】\[\]]+", "", value).casefold()


def load_contacts():
    path = OUT / "wecom-contacts.csv"
    if not path.is_file():
        raise SystemExit("缺少 bindings-out/wecom-contacts.csv，请先运行 bind_export.py")
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("是否外部客户") == "是"]


def load_store_table(path=None):
    """返回 [{name, phone, address}]。"""
    rows = []
    if path:
        path = Path(path)
        if path.suffix.lower() in (".xlsx", ".xlsm"):
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            sheet = wb[wb.sheetnames[0]]
            header = [str(c.value or "").strip() for c in next(sheet.iter_rows(max_row=1))]
            idx = {h: i for i, h in enumerate(header)}
            def col(cands):
                for c in cands:
                    for h, i in idx.items():
                        if c in h:
                            return i
                return None
            i_name = col(["签约人", "姓名", "签约"])
            i_phone = col(["手机号", "电话", "手机", "签约手机"])
            i_addr = col(["地址", "门店地址"])
            for row in sheet.iter_rows(min_row=2, values_only=True):
                name = str(row[i_name] or "").strip() if i_name is not None else ""
                phone = str(row[i_phone] or "").strip() if i_phone is not None else ""
                addr = str(row[i_addr] or "").strip() if i_addr is not None else ""
                if name or phone:
                    rows.append({"name": name, "phone": phone, "address": addr})
        else:
            raw = path.read_bytes()
            text = None
            for enc in ("utf-8-sig", "gbk", "gb18030"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                raise SystemExit("无法识别表格编码：" + str(path))
            import io
            for r in csv.DictReader(io.StringIO(text)):
                def pick(*keys):
                    for key in keys:
                        for k, v in r.items():
                            if key in str(k) and str(v or "").strip():
                                return str(v).strip()
                    return ""
                rows.append({
                    "name": pick("加盟商名称", "签约人姓名", "姓名", "签约人"),
                    "phone": pick("手机号", "签约手机号", "手机", "电话"),
                    "address": pick("门店地址", "地址"),
                    "state": pick("状态"),
                    "ext_id": pick("销帮帮id", "销帮帮ID"),
                })
    else:
        # 退而求其次：只读库里的门店表（字段完全对应）
        sys.path.insert(0, str(WS))
        import km_order
        cfg = json.loads((WS / "config.json").read_text("utf-8"))
        skill = km_order.KmOrderSkill(cfg, WS)
        for r in skill._query("SELECT franchisee_name, mobile, address FROM mds_franchisee"):
            rows.append({"name": str(r[0] or "").strip(), "phone": str(r[1] or "").strip(),
                         "address": str(r[2] or "").strip()})
    return [r for r in rows if r["name"] or r["phone"]]


def mask(phone):
    phone = str(phone or "")
    return phone[:3] + "****" + phone[-4:] if len(phone) >= 8 else phone


def main():
    table_path = sys.argv[1] if len(sys.argv) > 1 else None
    contacts = load_contacts()
    table = load_store_table(table_path)
    by_name = {}
    for item in table:
        by_name.setdefault(norm(item["name"]), []).append(item)
    print(json.dumps({"contacts": len(contacts), "store_rows": len(table)}, ensure_ascii=False))

    out_rows = []
    for c in contacts:
        name = c["企微显示名"]
        hist = [x for x in re.split(r"[；;、,，]", c.get("历史查询过的签约人") or "") if x]
        cands = []
        # 1) 历史查询过的签约人（最可信的业务信号）
        for h in hist:
            for item in by_name.get(norm(h), []):
                cands.append((item, 0.99, "历史查询"))
        # 2) 与企微显示名 / 会话标题（备注名）精确同名
        for alias in (name, c.get("会话显示名") or ""):
            for item in by_name.get(norm(alias), []):
                cands.append((item, 0.95, "同名"))
        # 3) 模糊匹配
        if not cands:
            scored = []
            probes = [x for x in (name, c.get("会话显示名") or "") if x]
            for key, items in by_name.items():
                ratio = max((difflib.SequenceMatcher(None, norm(p), key).ratio() for p in probes),
                            default=0.0)
                if ratio >= 0.55 and key:
                    for item in items:
                        scored.append((item, round(ratio, 2), "模糊"))
            scored.sort(key=lambda x: -x[1])
            cands = scored[:3]
        seen, uniq = set(), []
        for item, score, why in cands:
            key = (item["name"], item["phone"])
            if key in seen:
                continue
            seen.add(key)
            uniq.append((item, score, why))
        row = {
            "企微显示名": name,
            "会话显示名": c.get("会话显示名", ""),
            "企微客户ID": c["企微客户ID"],
            "来消息数": c.get("来消息数", ""),
            "历史查询过的签约人": c.get("历史查询过的签约人", ""),
        }
        for i in range(3):
            if i < len(uniq):
                item, score, why = uniq[i]
                row[f"候选{i+1}姓名"] = item["name"]
                row[f"候选{i+1}手机号"] = mask(item["phone"])
                row[f"候选{i+1}地址"] = item["address"][:30]
                row[f"候选{i+1}依据"] = why
                row[f"候选{i+1}完整手机号"] = item["phone"]
            else:
                row[f"候选{i+1}姓名"] = ""
                row[f"候选{i+1}手机号"] = ""
                row[f"候选{i+1}地址"] = ""
                row[f"候选{i+1}依据"] = ""
                row[f"候选{i+1}完整手机号"] = ""
        row["确认绑定手机号"] = ""
        row["确认(是/否)"] = ""
        out_rows.append(row)

    path = OUT / "binding-review.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        writer.writeheader()
        writer.writerows(out_rows)
    with_cand = sum(1 for r in out_rows if r["候选1姓名"])
    strong = sum(1 for r in out_rows if r["候选1依据"] in ("历史查询", "同名"))
    print(json.dumps({"ok": True, "path": str(path), "rows": len(out_rows),
                      "with_candidate": with_cand, "strong_candidate": strong},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
