#!/usr/bin/env python3
"""每日比較主動式 ETF 持股：新增 / 剔除 / 加碼 / 減碼，發 Discord。只用標準函式庫。"""
import html as htmlmod
import http.cookiejar
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "etf_state.json")
HOOK = os.environ.get("DISCORD_WEBHOOK_ETF", "")
DRY = "--dry" in sys.argv
TZ = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0"}
STUB = 5000  # 股數 <= 此值視為沒持有（投信會留 1,000 股的零碎部位）

ETFS = [
    {"code": "00981A", "name": "主動統一台股增長", "src": "ezmoney", "id": "49YTW"},
    {"code": "00403A", "name": "主動統一升級50", "src": "ezmoney", "id": "63YTW"},
    {"code": "00988A", "name": "主動統一全球創新", "src": "ezmoney", "id": "61YTW"},
    {"code": "00991A", "name": "主動復華未來50", "src": "fhtrust", "id": "ETF23"},
    {"code": "00982A", "name": "主動群益台灣強棒", "src": "capital", "id": "399"},
]


def get(url, data=None, headers=None, opener=None):
    h = dict(UA)
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    op = opener or urllib.request.build_opener()
    with op.open(req, timeout=40) as r:
        raw = r.read()
        cs = r.headers.get_content_charset()
    for enc in ([cs] if cs else []) + ["utf-8", "big5hkscs"]:
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "ignore")


def num(s):
    return int(float(str(s).replace(",", "")))


# 每個來源回傳 (資料日期 'YYYY-MM-DD', {代號: (名稱, 股數)})
def fetch_ezmoney(e, on=None):
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    page = htmlmod.unescape(get(f"https://www.ezmoney.com.tw/ETF/Fund/Info?fundCode={e['id']}", opener=op))
    hold, dt = {}, None
    for m in re.finditer(r'\{"FundCode":"%s"[^{}]*\}' % re.escape(e["id"]), page):
        try:
            o = json.loads(m.group(0))
        except Exception:
            continue
        if o.get("AssetCode") != "ST" or not o.get("DetailCode"):
            continue
        hold[o["DetailCode"].strip()] = (o.get("DetailName", "").strip(), num(o["Share"]))
        dt = dt or o.get("TranDate", "")[:10]
    return dt, hold


def _trandate(v):
    v = str(v)
    m = re.match(r"/Date\((\d+)\)/", v)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, timezone.utc).astimezone(TZ).date().isoformat()
    return v[:10]


def fetch_ezmoney_pcf(e, post):
    """申購買回清單：post 為清單日期（民國年/月/日）。清單內的 TranDate 才是持股的收盤日。"""
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    get("https://www.ezmoney.com.tw/ETF/Transaction/PCF", opener=op)
    body = json.dumps({"fundCode": e["id"], "date": post, "specificDate": True}).encode()
    j = json.loads(get("https://www.ezmoney.com.tw/ETF/Transaction/GetPCF", data=body, opener=op,
                       headers={"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}))
    hold, dt = {}, None
    for a in j.get("asset") or []:
        if a.get("AssetCode") != "ST":
            continue
        for o in a.get("Details") or []:
            if o.get("DetailCode"):
                hold[o["DetailCode"].strip()] = (o.get("DetailName", "").strip(), num(o["Share"]))
                dt = dt or _trandate(o.get("TranDate"))
    return dt, hold


def fetch_fhtrust(e, on=None):
    start = on or datetime.now(TZ).date()
    for i in range(0, 6 if on is None else 1):
        d = (start - timedelta(days=i)).strftime("%Y/%m/%d")
        j = json.loads(get(f"https://www.fhtrust.com.tw/api/assets?fundID={e['id']}&qDate={d}"))
        x = (j.get("result") or [{}])[0]
        hold = {}
        for r in x.get("detail") or []:
            if r.get("ftype") == "股票" and r.get("stockid"):
                hold[r["stockid"].strip()] = (r.get("stockname", "").strip(), num(r["qshare"]))
        if hold:
            return (x.get("dDate") or d).replace("/", "-"), hold
    return None, {}


def fetch_capital(e, on=None):
    body = json.dumps({"fundId": e["id"], "date": on.isoformat() if on else None}).encode()
    j = json.loads(get("https://www.capitalfund.com.tw/CFWeb/api/etf/buyback", data=body,
                       headers={"Content-Type": "application/json"}))["data"]
    hold = {r["stocNo"].strip(): (r.get("stocName", "").strip(), num(r["share"])) for r in j.get("stocks") or []}
    return (j["pcf"]["date2"][:10] if hold else None), hold


FETCH = {"ezmoney": fetch_ezmoney, "fhtrust": fetch_fhtrust, "capital": fetch_capital}


def clean(h):
    return {k: v for k, v in h.items() if v[1] > STUB}


def diff(old, new):
    old, new = clean(old), clean(new)
    add = [(k, new[k][0], new[k][1]) for k in new if k not in old]
    out = [(k, old[k][0], old[k][1]) for k in old if k not in new]
    up, down = [], []
    for k in new:
        if k in old and new[k][1] != old[k][1]:
            (up if new[k][1] > old[k][1] else down).append((k, new[k][0], old[k][1], new[k][1]))
    add.sort(key=lambda r: -r[2])
    out.sort(key=lambda r: -r[2])
    up.sort(key=lambda r: -(r[3] - r[2]) / r[2])
    down.sort(key=lambda r: (r[3] - r[2]) / r[2])
    return add, out, up, down


def chunks(lines, limit=1000):
    cur, res = "", []
    for l in lines:
        if len(cur) + len(l) + 1 > limit:
            res.append(cur)
            cur = ""
        cur += l + "\n"
    if cur:
        res.append(cur)
    return res


def build(e, d_old, d_new, add, out, up, down):
    fields = []

    def sec(title, lines):
        if not lines:
            return
        for i, c in enumerate(chunks(lines[:30])):
            fields.append({"name": title if i == 0 else title + "（續）", "value": c})
        if len(lines) > 30:
            fields.append({"name": "​", "value": f"…另有 {len(lines) - 30} 檔"})

    sec(f"🆕 新增（{len(add)}）", [f"**{n}**（{k}）　{s:,} 股" for k, n, s in add])
    sec(f"❌ 剔除（{len(out)}）", [f"**{n}**（{k}）　原 {s:,} 股" for k, n, s in out])
    sec(f"🔺 加碼（{len(up)}）", [f"**{n}**（{k}）　+{b - a:,} 股（{a:,} → {b:,}，+{(b - a) / a * 100:.1f}%）" for k, n, a, b in up])
    sec(f"🔻 減碼（{len(down)}）", [f"**{n}**（{k}）　{b - a:,} 股（{a:,} → {b:,}，{(b - a) / a * 100:.1f}%）" for k, n, a, b in down])
    none = not (add or out or up or down)
    return {
        "title": f"{e['code']} {e['name']}｜持股異動",
        "description": f"比較 **{d_old} → {d_new}**" + ("\n兩個交易日持股沒有變化。" if none else ""),
        "color": 0x2B7DE9 if not none else 0x808080,
        "fields": fields[:24],
        "footer": {"text": "資料：投信官網每日持股；股數 ≤ 5,000 視為未持有"},
    }


def send(embed):
    if DRY or not HOOK:
        print("[DRY]", json.dumps(embed, ensure_ascii=False, indent=1))
        return
    data = json.dumps({"embeds": [embed]}).encode()
    req = urllib.request.Request(HOOK, data=data, headers={"Content-Type": "application/json", **UA})
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as ex:
        print("DISCORD FAIL", repr(ex), file=sys.stderr)


def prev_snapshot(e, dt):
    """第一次執行時，直接查前一個持股日，立刻產生比較。"""
    d = date.fromisoformat(dt)
    if e["src"] == "ezmoney":
        today = datetime.now(TZ).date()
        for i in range(0, 8):
            x = today - timedelta(days=i)
            try:
                pdt, ph = fetch_ezmoney_pcf(e, f"{x.year - 1911}/{x.month:02d}/{x.day:02d}")
            except Exception:
                continue
            if ph and pdt and pdt < dt:
                return pdt, ph
        return None
    for i in range(1, 6):
        try:
            pdt, ph = FETCH[e["src"]](e, d - timedelta(days=i))
        except Exception:
            continue
        if ph and pdt and pdt < dt:
            return pdt, ph
    return None


def main():
    state = json.load(open(STATE_PATH, encoding="utf-8")) if os.path.exists(STATE_PATH) else {}
    for e in ETFS:
        try:
            dt, hold = FETCH[e["src"]](e)
            if not hold or not dt:
                print("NO DATA", e["code"], file=sys.stderr)
                continue
            st = state.get(e["code"])
            if st is None:
                p = prev_snapshot(e, dt)
                if p:
                    send(build(e, p[0], dt, *diff({k: tuple(v) for k, v in p[1].items()}, hold)))
                else:
                    send({"title": f"{e['code']} {e['name']}｜開始追蹤",
                          "description": f"已記下 **{dt}** 的持股（{len(clean(hold))} 檔）作為基準，之後每天比較新增、剔除、加碼、減碼。",
                          "color": 0x808080})
            elif st["date"] != dt:
                old = {k: tuple(v) for k, v in st["hold"].items()}
                send(build(e, st["date"], dt, *diff(old, hold)))
            else:
                continue
            state[e["code"]] = {"date": dt, "hold": {k: list(v) for k, v in hold.items()}}
        except Exception as ex:
            print("ERROR", e["code"], repr(ex), file=sys.stderr)
    if not DRY:
        json.dump(state, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
