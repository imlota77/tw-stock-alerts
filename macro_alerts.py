#!/usr/bin/env python3
"""美國官方總經數據提醒：CPI / PPI / 非農就業（BLS）＋ FOMC 聲明（聯準會 RSS）→ Discord。只用標準函式庫。

- BLS 公開 API 沒金鑰每天只有 25 次查詢，所以：只在可能公布的時段查、最短間隔 4 分鐘、每天最多 20 次。
  設了環境變數 BLS_API_KEY（免費申請）就放寬到 400 次。
- 狀態存 macro_state.json：每個指標最後一期、FOMC 已看過的 guid。
"""
import html as htmlmod
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "macro_state.json")
HOOK = os.environ.get("DISCORD_WEBHOOK_MACRO", "")
BLS_KEY = os.environ.get("BLS_API_KEY", "")
FRED_KEY = os.environ.get("FRED_API_KEY", "")
DRY = "--dry" in sys.argv
FORCE = "--force" in sys.argv
TZ = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0"}
MAX_CALLS = 400 if BLS_KEY else 20
MIN_GAP = 240  # 秒（公布時段內）
OFF_GAP = 600 if BLS_KEY else 3600  # 秒（非公布時段；有金鑰每 10 分鐘，沒有每小時，省額度）
ERRORS = {}

SERIES = {
    "cpi": "CUSR0000SA0", "cpi_nsa": "CUUR0000SA0",
    "core": "CUSR0000SA0L1E", "core_nsa": "CUUR0000SA0L1E",
    "ppi": "WPSFD4", "ppi_nsa": "WPUFD4",
    "nfp": "CES0000000001", "unemp": "LNS14000000", "ahe": "CES0500000003",
}
MONTHS = [f"{i}月" for i in range(1, 13)]


def http(url, data=None, headers=None, timeout=40):
    h = dict(UA)
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def send(embed):
    if DRY or not HOOK:
        print("[DRY]", json.dumps(embed, ensure_ascii=False, indent=1).encode("utf-8", "replace").decode("utf-8", "replace"))
        return True
    data = json.dumps({"embeds": [embed]}).encode()
    try:
        http(HOOK, data=data, headers={"Content-Type": "application/json"}, timeout=30)
        return True
    except Exception as ex:
        print("DISCORD FAIL", repr(ex), file=sys.stderr)
        return False


# ---------------- BLS ----------------
def bls_fetch():
    y = datetime.now(TZ).year
    body = {"seriesid": list(SERIES.values()), "startyear": str(y - 2), "endyear": str(y)}
    if BLS_KEY:
        body["registrationkey"] = BLS_KEY
    j = json.loads(http("https://api.bls.gov/publicAPI/v2/timeseries/data/", data=json.dumps(body).encode(),
                        headers={"Content-Type": "application/json"}))
    if j.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(str(j.get("message"))[:200])
    out = {}
    for s in j["Results"]["series"]:
        d = {}
        for x in s["data"]:
            if x["period"].startswith("M") and x["period"] != "M13" and x["value"] not in ("-", ""):
                d[(int(x["year"]), int(x["period"][1:]))] = float(x["value"])
        out[s["seriesID"]] = d
    return out


def prev(ym, n=1):
    y, m = ym
    m -= n
    while m <= 0:
        m += 12
        y -= 1
    return (y, m)


def pct(a, b):
    return (a / b - 1) * 100 if a is not None and b else None


def f(v, d=1, sign=True):
    return "—" if v is None else (f"{v:+.{d}f}%" if sign else f"{v:.{d}f}%")


def latest(d):
    return max(d) if d else None


def label(ym):
    return f"{ym[0]}年{ym[1]}月"


def price_block(name, sa, nsa):
    p = latest(sa)
    p1 = prev(p)
    mom, mom1 = pct(sa.get(p), sa.get(p1)), pct(sa.get(p1), sa.get(prev(p, 2)))
    q = latest(nsa)
    yoy = pct(nsa.get(q), nsa.get(prev(q, 12)))
    yoy1 = pct(nsa.get(prev(q)), nsa.get(prev(q, 13)))
    return f"**{name}**\n月增 **{f(mom, 2)}**（前月 {f(mom1, 2)}）\n年增 **{f(yoy)}**（前月 {f(yoy1)}）"


def build_cpi(S):
    p = latest(S[SERIES["cpi"]])
    return p, {
        "title": f"🇺🇸 美國 CPI｜{label(p)}",
        "description": price_block("整體 CPI", S[SERIES["cpi"]], S[SERIES["cpi_nsa"]]) + "\n\n" +
                       price_block("核心 CPI（不含食品、能源）", S[SERIES["core"]], S[SERIES["core_nsa"]]),
        "color": 0xE0A100,
        "footer": {"text": "資料：美國勞工統計局 BLS｜月增為季調、年增為未季調｜無市場預期值"},
        "url": "https://www.bls.gov/cpi/",
    }


def build_ppi(S):
    p = latest(S[SERIES["ppi"]])
    return p, {
        "title": f"🇺🇸 美國 PPI｜{label(p)}",
        "description": price_block("最終需求 PPI", S[SERIES["ppi"]], S[SERIES["ppi_nsa"]]),
        "color": 0xE0A100,
        "footer": {"text": "資料：美國勞工統計局 BLS｜月增為季調、年增為未季調｜無市場預期值"},
        "url": "https://www.bls.gov/ppi/",
    }


def build_jobs(S, old):
    n, u, a = S[SERIES["nfp"]], S[SERIES["unemp"]], S[SERIES["ahe"]]
    p = latest(n)
    p1 = prev(p)
    chg = n[p] - n[p1]
    chg1 = n[p1] - n[prev(p, 2)]
    lines = [f"非農新增就業 **{chg:+,.0f} 千人**（前月修正後 {chg1:+,.0f} 千人）"]
    if old and old.get("chg1_reported") is not None and abs(old["chg1_reported"] - chg1) >= 1:
        lines[0] += f"，前月原公布 {old['chg1_reported']:+,.0f}"
    lines.append(f"失業率 **{u[latest(u)]:.1f}%**（前月 {u.get(prev(latest(u)), float('nan')):.1f}%）")
    q = latest(a)
    lines.append(f"平均時薪 **${a[q]:.2f}**，月增 {f(pct(a[q], a.get(prev(q))), 2)}、年增 {f(pct(a[q], a.get(prev(q, 12))))}")
    return p, chg, {
        "title": f"🇺🇸 美國非農就業｜{label(p)}",
        "description": "\n".join(lines),
        "color": 0x2B7DE9,
        "footer": {"text": "資料：美國勞工統計局 BLS（季調）｜無市場預期值"},
        "url": "https://www.bls.gov/ces/",
    }, chg1


# ---------------- FOMC ----------------
def fomc_text(link):
    try:
        s = http(link).decode("utf-8", "ignore")
    except Exception:
        return ""
    s = re.sub(r"(?s)<(script|style)[^>]*>.*?</\1>", "", s)
    t = htmlmod.unescape(re.sub(r"(?s)<[^>]+>", " ", s))
    t = re.sub(r"\s+", " ", t)
    i = t.find("For release")
    t = t[i:] if i >= 0 else t
    t = re.sub(r"^For release at [^S]*?(Share|EDT|EST)\s*(-->\s*)*", "", t)
    j = t.find("For media inquiries")
    return (t[:j] if j > 0 else t[:2500]).strip()


def check_fomc(state):
    root = ET.fromstring(http("https://www.federalreserve.gov/feeds/press_monetary.xml").lstrip(b"\xef\xbb\xbf"))
    items = []
    for it in root.iter("item"):
        g = (it.findtext("guid") or it.findtext("link") or "").strip()
        items.append((g, (it.findtext("title") or "").strip(), (it.findtext("link") or "").strip(), (it.findtext("pubDate") or "").strip()))
    seen = set(state.get("fomc_seen", []))
    if not seen and not FORCE:
        new = [x for x in items if re.search(r"FOMC statement", x[1], re.I)][:1]  # 第一次只補發最新一則利率聲明
    else:
        new = [x for x in items if x[0] not in seen]
    failed = set()
    for g, title, link, pub in reversed(new):
        if not re.search(r"FOMC|Federal Open Market Committee|economic projections|Implementation Note", title, re.I):
            seen.add(g)  # 貼現率會議紀錄、人事、工作小組等非利率決策公告不發
            continue
        try:
            dt = parsedate_to_datetime(pub).astimezone(TZ).strftime("%Y/%m/%d %H:%M")
        except Exception:
            dt = pub
        body = fomc_text(link)
        body = re.sub(r"^((Share|-->)\s*)+", "", body)
        body = re.sub(r"\s*(-->\s*)+", " ", body)
        rate = re.search(r"The Committee decided[^.]*federal funds rate[^.]*\.", body)
        desc = (f"**重點：** {rate.group(0).strip()}\n\n" if rate and "statement" in title.lower() else "")
        desc += (body[:1500] + ("…" if len(body) > 1500 else "")) if body else "（內文請見連結）"
        desc += f"\n\n（英文原文；中文翻譯可再請我處理）\n{link}"
        ok = send({"title": f"🇺🇸 聯準會 FOMC｜{title}", "description": desc[:4000], "color": 0xC0392B,
                   "footer": {"text": f"資料：美國聯邦準備理事會｜發布時間（台灣）{dt}"}, "url": link})
        if ok:
            seen.add(g)
        else:
            failed.add(g)
    state["fomc_seen"] = sorted(seen | {x[0] for x in items if x[0] not in failed})[-200:]


# ---------------- PCE（FRED 轉載 BEA 資料，免金鑰） ----------------
def fred_series(sid):
    start = f"{datetime.now(TZ).year - 2}-01-01"
    d = {}
    if FRED_KEY:  # 官方 API（穩定）；失敗再退回網頁 CSV
        try:
            j = json.loads(http(f"https://api.stlouisfed.org/fred/series/observations?series_id={sid}&api_key={FRED_KEY}"
                                f"&file_type=json&observation_start={start}", timeout=30))
            for o in j.get("observations", []):
                if o.get("value") not in (".", "", None):
                    y, m = o["date"].split("-")[:2]
                    d[(int(y), int(m))] = float(o["value"])
            if d:
                return d
        except Exception as ex:
            print("FRED API ERROR", repr(ex)[:120].replace(FRED_KEY, "***"), file=sys.stderr)
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}"
    txt = None
    for attempt in range(3):  # FRED 網頁偶爾很慢，重試
        try:
            txt = http(url, timeout=45).decode("utf-8", "ignore")
            break
        except Exception:
            if attempt == 2:
                raise
    for line in txt.splitlines()[1:]:
        a = line.split(",")
        if len(a) == 2 and a[1] not in (".", ""):
            y, m = a[0].split("-")[:2]
            d[(int(y), int(m))] = float(a[1])
    return d


def build_pce():
    head, core = fred_series("PCEPI"), fred_series("PCEPILFE")
    p = min(latest(head), latest(core))

    def blk(name, d):
        mom, mom1 = pct(d.get(p), d.get(prev(p))), pct(d.get(prev(p)), d.get(prev(p, 2)))
        yoy, yoy1 = pct(d.get(p), d.get(prev(p, 12))), pct(d.get(prev(p)), d.get(prev(p, 13)))
        return f"**{name}**\n月增 **{f(mom, 2)}**（前月 {f(mom1, 2)}）\n年增 **{f(yoy)}**（前月 {f(yoy1)}）"

    return p, {
        "title": f"🇺🇸 美國 PCE 物價指數｜{label(p)}",
        "description": blk("整體 PCE", head) + "\n\n" + blk("核心 PCE（不含食品、能源，聯準會最看重）", core),
        "color": 0xE0A100,
        "footer": {"text": "資料：美國經濟分析局 BEA（經 FRED 轉載，季調）｜無市場預期值"},
        "url": "https://www.bea.gov/data/personal-consumption-expenditures-price-index",
    }


# ---------------- 台指期（TX）月結算日 ----------------
def tw_closed_days():
    days = set()
    try:
        for r in json.loads(http("https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule")):
            txt = r.get("Name", "") + r.get("Description", "")
            if "開始交易" in txt or "最後交易" in txt:
                continue
            d = r.get("Date", "")
            if len(d) == 7:
                days.add(datetime(int(d[:3]) + 1911, int(d[3:5]), int(d[5:7])).date())
    except Exception as ex:
        print("HOLIDAY ERROR", repr(ex), file=sys.stderr)
    return days


def is_trading(d, closed):
    return d.weekday() < 5 and d not in closed


def settle_date(y, m, closed):
    d = datetime(y, m, 1).date()
    d += timedelta(days=(2 - d.weekday()) % 7 + 14)  # 第三個週三
    while not is_trading(d, closed):
        d += timedelta(days=1)
    return d


def prev_trading(d, closed):
    d -= timedelta(days=1)
    while not is_trading(d, closed):
        d -= timedelta(days=1)
    return d


def fetch_tx_settlement(sd):
    """台灣期貨交易所「最後結算價」頁：每列 日期|契約月份|臺股期貨結算價|…（每日 CSV 裡到期契約的結算價欄位是 0，不能用）"""
    s = http("https://www.taifex.com.tw/cht/5/futIndxFSP").decode("utf-8", "ignore")
    s = re.sub(r"(?s)<(script|style)[^>]*>.*?</\1>", "", s)
    t = re.sub(r"[\s|]+", "|", htmlmod.unescape(re.sub(r"(?s)<[^>]+>", "|", s)))
    m = re.search(re.escape(sd.strftime("%Y/%m/%d")) + r"\|" + sd.strftime("%Y%m") + r"\|([\d,]+(?:\.\d+)?)\|", t)
    return float(m.group(1).replace(",", "")) if m else None


def check_tx(state, now):
    closed = tw_closed_days()
    today = now.date()
    sent = state.setdefault("tx", {})
    nxt = (today.year + (today.month == 12), today.month % 12 + 1)
    for sd in (settle_date(today.year, today.month, closed), settle_date(nxt[0], nxt[1], closed)):
        key = sd.isoformat()
        q = "季月" if sd.month in (3, 6, 9, 12) else ""
        tag = f"{sd.month}月台指期{q}結算日"
        eve = prev_trading(sd, closed)
        if today == sd - timedelta(days=7) and now.hour >= 8 and not sent.get(key + "|week"):
            if send({"title": f"🗓️ 一週後（{sd.strftime('%m/%d')}）是{tag}",
                     "description": f"下週{'一二三四五六日'[sd.weekday()]} {sd.strftime('%m/%d')} 是 TX 月契約最後交易日。\n"
                                    "有持倉的人可提前規劃：是否換倉（轉到下個月契約）、是否降低部位或調整避險。",
                     "color": 0x8E44AD, "footer": {"text": "台灣期貨交易所｜規則：每月第三個週三，遇休市順延"}}):
                sent[key + "|week"] = True
        if today == eve and now.hour >= 18 and not sent.get(key + "|eve"):
            if send({"title": f"📅 明天是{tag}（{sd.strftime('%m/%d')}）",
                     "description": "月契約最後交易日。結算價為當天最後一段時間（約 13:00–13:30）加權指數的平均，**不是收盤價**；"
                                    "結算日前後常有換倉與避險動作，盤面波動可能放大。\n週三同時是台指選擇權週契約結算日。",
                     "color": 0x8E44AD, "footer": {"text": "台灣期貨交易所｜規則：每月第三個週三，遇休市順延"}}):
                sent[key + "|eve"] = True
        if today == sd and now.hour >= 8 and not sent.get(key + "|day"):
            if send({"title": f"⏰ 今天是{tag}（{sd.strftime('%m/%d')}）",
                     "description": "今天是 TX 月契約最後交易日，13:30 收盤；結算價約 13:45–15:00 後可查。\n留意換倉（轉到下個月契約）與尾盤波動。",
                     "color": 0x8E44AD, "footer": {"text": "台灣期貨交易所"}}):
                sent[key + "|day"] = True
        if today == sd and now.hour >= 15 and not sent.get(key + "|res"):
            try:
                px = fetch_tx_settlement(sd)
            except Exception as ex:
                print("TX ERROR", repr(ex), file=sys.stderr)
                px = None
            if px:
                desc = (f"**最後結算價 {px:,.0f}**\n這是 TX 月契約到期的結算價（約為最後 30 分鐘加權指數平均），"
                        "持有到期的部位以此價格現金結算。")
                if send({"title": f"✅ {tag}｜{sd.strftime('%Y/%m/%d')} 結算結果", "description": desc, "color": 0x8E44AD,
                         "footer": {"text": "資料：台灣期貨交易所「最後結算價」"}}):
                    sent[key + "|res"] = True
    for k in list(sent):
        if k.split("|")[0] < (today - timedelta(days=90)).isoformat():
            sent.pop(k)


# ---------------- 主流程 ----------------
def bls_allowed(now, state):
    if FORCE or "--bls" in sys.argv:
        return True
    if now.weekday() >= 5:
        return False
    # 美東 8:30 公布 = 台灣 20:30（夏令）/21:30（冬令）；早上 8 點與 23 點各補查一次
    if now.hour not in (8, 20, 21, 22, 23):
        return False
    if now.hour == 20 and now.minute < 20:
        return False
    c = state.get("bls_calls", {})
    today = now.strftime("%Y-%m-%d")
    if c.get("day") != today:
        c = {"day": today, "n": 0, "last": 0}
    if c["n"] >= MAX_CALLS:
        return False
    hm = now.hour * 60 + now.minute
    in_window = 20 * 60 + 25 <= hm <= 20 * 60 + 55 or 21 * 60 + 25 <= hm <= 21 * 60 + 55
    return now.timestamp() - c.get("last", 0) >= (MIN_GAP if in_window else OFF_GAP)


def main():
    state = json.load(open(STATE_PATH, encoding="utf-8")) if os.path.exists(STATE_PATH) else {}
    now = datetime.now(TZ)
    try:
        check_fomc(state)
    except Exception as ex:
        print("FOMC ERROR", repr(ex), file=sys.stderr)
    try:
        check_tx(state, now)
    except Exception as ex:
        print("TX CHECK ERROR", repr(ex), file=sys.stderr); ERRORS["tx"] = repr(ex)[:200]
    if FORCE or "--bls" in sys.argv or (now.weekday() < 5 and now.hour in (8, 20, 21, 22, 23)):
        try:
            p, embed = build_pce()
            if state.get("pce") != list(p) and send(embed):
                state["pce"] = list(p)
        except Exception as ex:
            print("PCE ERROR", repr(ex), file=sys.stderr); ERRORS["pce"] = repr(ex)[:200]
    if bls_allowed(now, state):
        c = state.get("bls_calls", {})
        if c.get("day") != now.strftime("%Y-%m-%d"):
            c = {"day": now.strftime("%Y-%m-%d"), "n": 0, "last": 0}
        c["n"] += 1
        c["last"] = now.timestamp()
        state["bls_calls"] = c
        try:
            S = bls_fetch()
            for key, builder in (("cpi", build_cpi), ("ppi", build_ppi)):
                p, embed = builder(S)
                if state.get(key) != list(p):
                    if send(embed):
                        state[key] = list(p)
            p, chg, embed, chg1 = build_jobs(S, state.get("jobs"))
            old = state.get("jobs") or {}
            if old.get("period") != list(p):
                if send(embed):
                    state["jobs"] = {"period": list(p), "chg": chg, "chg1_reported": chg1}
        except Exception as ex:
            print("BLS ERROR", repr(ex), file=sys.stderr); ERRORS["bls"] = repr(ex)[:200]
    state["last_errors"] = ERRORS
    if not DRY:
        json.dump(state, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
