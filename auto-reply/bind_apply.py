#!/usr/bin/env python
"""把核对后的清单写入绑定表。

支持的输入：
1) bindings-out/binding-review.csv：确认(是/否)=是 且 确认绑定手机号 有值
2) 任意 CSV：需含“企微客户ID”“手机号”两列（后台直接绑，全部置 active）

用法:
  python bind_apply.py                       # 用 binding-review.csv
  python bind_apply.py 我的绑定表.csv        # 用两列简表
  python bind_apply.py --dry-run             # 只校验不写库
"""
import csv, json, os, re, sys
from pathlib import Path

WS = Path(__file__).resolve().parent
OUT = WS / "bindings-data" / "review"
ACCOUNT = os.environ.get("WECOM_ACCOUNT", "")  # 企微账号 ID：运行前用环境变量 WECOM_ACCOUNT 指定
sys.path.insert(0, str(WS))
import bindings


def norm_phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    return digits if re.fullmatch(r"1\d{10}", digits) else ""


def unionid_map():
    """从联系人导出里拿 peer_id -> unionid，迁移换机时用。"""
    path = OUT / "wecom-contacts.csv"
    mapping = {}
    if path.is_file():
        with path.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("企微客户ID") and r.get("unionid"):
                    mapping[str(r["企微客户ID"])] = str(r["unionid"])
    return mapping


def rows_from_review(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            ok = str(r.get("确认(是/否)") or "").strip().lower() in ("是", "y", "yes", "1", "true")
            phone = norm_phone(r.get("确认绑定手机号"))
            if ok and phone:
                yield {"peer_id": str(r.get("企微客户ID") or "").strip(), "phone": phone,
                       "name": str(r.get("企微显示名") or ""), "note": "review确认",
                       "unionid": str(r.get("unionid") or "")}
            elif ok and not phone:
                print("跳过（确认为是但手机号无效）：", r.get("企微显示名"), repr(r.get("确认绑定手机号")))


def rows_from_simple(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            phone = norm_phone(r.get("手机号") or r.get("签约手机号"))
            peer = str(r.get("企微客户ID") or r.get("peer_id") or "").strip()
            if phone and re.fullmatch(r"\d{5,}", peer):
                yield {"peer_id": peer, "phone": phone,
                       "name": str(r.get("企微显示名") or r.get("姓名") or ""), "note": "批量导入",
                       "unionid": str(r.get("unionid") or "")}
            else:
                print("跳过无效行：", r)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    path = Path(args[0]) if args else OUT / "binding-review.csv"
    if not path.is_file():
        raise SystemExit("找不到输入文件：" + str(path))
    rows = list(rows_from_review(path) if "binding-review" in path.name else rows_from_simple(path))
    db = bindings.connect()
    uids = unionid_map()
    applied = 0
    seen = set()
    for row in rows:
        key = (ACCOUNT, row["peer_id"])
        if key in seen:
            print("重复行跳过：", row["peer_id"])
            continue
        seen.add(key)
        if not dry:
            bindings.set_binding(ACCOUNT, row["peer_id"], row["phone"], state="active",
                                 source="admin", note=row.get("note", ""),
                                 unionid=row.get("unionid") or uids.get(row["peer_id"], ""), db=db)
        applied += 1
    if not dry:
        db.commit()
    db.close()
    total = len(bindings.list_bindings()) if not dry else applied
    print(json.dumps({"ok": True, "input": str(path), "applied": applied, "dry_run": dry,
                      "total_bindings": total if not dry else None}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
