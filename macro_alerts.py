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
DRY = "--dry" in sys.argv
FORCE = "--force" in sys.argv
TZ = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0"}
MAX_CALLS = 400 if BLS_KEY else 20
MIN_GAP = 240  # 秒

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
    return now.timestamp() - c.get("last", 0) >= MIN_GAP


def main():
    state = json.load(open(STATE_PATH, encoding="utf-8")) if os.path.exists(STATE_PATH) else {}
    now = datetime.now(TZ)
    try:
        check_fomc(state)
    except Exception as ex:
        print("FOMC ERROR", repr(ex), file=sys.stderr)
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
            print("BLS ERROR", repr(ex), file=sys.stderr)
    if not DRY:
        json.dump(state, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
