#!/usr/bin/env python3
"""每天晚上整理證交所「三大法人買賣金額統計」與「信用交易統計(融資融券)」發 Discord。只用標準函式庫。"""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "market_state.json")
HOOK = os.environ.get("DISCORD_WEBHOOK_MARKET", "")
DRY = "--dry" in sys.argv
FORCE = "--force" in sys.argv
UA = {"User-Agent": "Mozilla/5.0"}


def get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))


def n(s):
    return int(str(s).replace(",", ""))


def yi(v, digits=2):
    """元 -> 億元"""
    return f"{v / 1e8:+,.{digits}f}"


def fmt_date(d):
    return f"{d[:4]}/{d[4:6]}/{d[6:]}"


def institutional():
    j = get_json("https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json&dayDate=&type=day")
    if j.get("stat") != "OK":
        return None, None
    rows = {r[0]: r for r in j["data"]}
    lines = []
    for name in ["外資及陸資(不含外資自營商)", "外資自營商", "投信", "自營商(自行買賣)", "自營商(避險)", "合計"]:
        r = rows.get(name)
        if not r:
            continue
        buy, sell, diff = n(r[1]), n(r[2]), n(r[3])
        if name == "外資自營商" and buy == 0 and sell == 0:
            continue
        icon = "🔴" if diff > 0 else ("🟢" if diff < 0 else "⚪")
        label = "外資及陸資" if name.startswith("外資及陸資") else name
        lines.append(f"{icon} **{label}**　買 {buy / 1e8:,.1f}　賣 {sell / 1e8:,.1f}　**{yi(diff)} 億**")
    return j["date"], "\n".join(lines)


def margin():
    j = get_json("https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&date=&selectType=MS")
    if j.get("stat") != "OK":
        return None, None
    t = j["tables"][0]
    rows = {r[0]: r for r in t["data"]}
    out = []
    for key, unit, scale, label in [
        ("融資(交易單位)", "張", 1, "融資餘額"),
        ("融券(交易單位)", "張", 1, "融券餘額"),
        ("融資金額(仟元)", "億元", 100000, "融資金額餘額"),
    ]:
        r = rows.get(key)
        if not r:
            continue
        prev, now = n(r[4]), n(r[5])
        d = now - prev
        if scale == 1:
            a, b, c = f"{now:,}", f"{d:+,}", unit
        else:
            a, b, c = f"{now / scale:,.1f}", f"{d / scale:+,.1f}", unit
        icon = "🔺" if d > 0 else ("🔻" if d < 0 else "▫️")
        out.append(f"{icon} **{label}**　{a} {c}　（較前日 {b}）")
    r = rows.get("融資(交易單位)")
    if r:
        out.append(f"　融資買進 {n(r[1]):,}／賣出 {n(r[2]):,}／現償 {n(r[3]):,}")
    r = rows.get("融券(交易單位)")
    if r:
        out.append(f"　融券買進(回補) {n(r[1]):,}／賣出 {n(r[2]):,}／現券償還 {n(r[3]):,}")
    return j["date"], "\n".join(out)


def send(embed):
    if DRY or not HOOK:
        print("[DRY]", json.dumps(embed, ensure_ascii=False, indent=1))
        return True
    data = json.dumps({"embeds": [embed]}).encode()
    req = urllib.request.Request(HOOK, data=data, headers={"Content-Type": "application/json", **UA})
    try:
        urllib.request.urlopen(req, timeout=30).read()
        return True
    except Exception as ex:
        print("DISCORD FAIL", repr(ex), file=sys.stderr)
        return False


def main():
    state = json.load(open(STATE_PATH, encoding="utf-8")) if os.path.exists(STATE_PATH) else {}
    d1, inst = institutional()
    d2, mar = margin()
    date = d1 if d1 == d2 else (d2 or d1)
    if not date:
        print("NO DATA", file=sys.stderr)
        return
    if d1 != d2:
        print("DATE MISMATCH", d1, d2, file=sys.stderr)
    if state.get("last") == date and not FORCE:
        print("already sent", date)
        return
    fields = []
    if inst:
        fields.append({"name": f"三大法人買賣金額（億元，{fmt_date(d1)}）", "value": inst})
    if mar:
        fields.append({"name": f"信用交易統計（上市，{fmt_date(d2)}）", "value": mar})
    embed = {
        "title": f"台股盤後｜三大法人與融資融券（{fmt_date(date)}）",
        "color": 0x2B7DE9,
        "fields": fields,
        "footer": {"text": "資料：臺灣證券交易所（上市）｜紅=買超/增加 綠=賣超/減少"},
    }
    if send(embed) and not DRY:
        state["last"] = date
        json.dump(state, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
