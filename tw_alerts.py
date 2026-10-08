#!/usr/bin/env python3
"""台股追蹤:月營收 / 季財報 / 法說會 -> Discord。資料來源:公開資訊觀測站。"""
import json, os, re, sys, time, urllib.request, urllib.error
from datetime import datetime, date, timedelta, timezone

TZ = timezone(timedelta(hours=8))  # 台灣無夏令時間
HOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
CUR_HOOK = []  # 目前處理中的股票專屬 webhook 清單(沒有就用預設)
DRY = "--dry" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
UA = "Mozilla/5.0 (tw-stock-alerts)"
COLORS = {"rev": 0x2ECC71, "report": 0x3498DB, "meeting": 0x9B59B6, "board": 0xE67E22, "warn": 0xE74C3C}
QNUM = {"一": 1, "二": 2, "三": 3, "四": 4}


# ---------- http ----------
def http(url, data=None, headers=None, method=None, retries=3, timeout=40):
    h = {"User-Agent": UA}
    h.update(headers or {})
    err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code in (404, 403):
                return e.code, b""
            err = e
        except Exception as e:
            err = e
        time.sleep(2 * (i + 1))
    print("HTTP FAIL", url, err, file=sys.stderr)
    return 0, b""


def mops(path, body):
    s, b = http("https://mops.twse.com.tw/mops/api/" + path, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"})
    if s != 200:
        return None
    try:
        j = json.loads(b)
    except Exception:
        return None
    return j.get("result") if j.get("code") == 200 else None


# ---------- discord ----------
def send(embed):
    hooks = CUR_HOOK or ([HOOK] if HOOK else [])
    if DRY or not hooks:
        print("[DRY]", json.dumps(embed, ensure_ascii=False, indent=1))
        return True
    ok = False
    for hook in hooks:
        s, b = http(hook, data=json.dumps({"embeds": [embed]}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
        time.sleep(1.2)
        if s not in (200, 204):
            print("DISCORD FAIL", s, b[:200], file=sys.stderr)
        else:
            ok = True
    return ok


def mk(code, name, kind, color, desc, fields=None, url=None):
    e = {"title": f"{name}（{code}）｜{kind}", "color": COLORS[color], "description": desc,
         "footer": {"text": "資料來源:公開資訊觀測站"},
         "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if fields:
        e["fields"] = fields
    if url:
        e["url"] = url
    return e


# ---------- helpers ----------
def roc(d):
    return d.year - 1911


def parse_roc_date(s):
    m = re.search(r"(\d{2,3})/(\d{1,2})/(\d{1,2})", s or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def num(s):
    try:
        return float(str(s).replace(",", "").replace("%", "").strip())
    except Exception:
        return None


def pct(v):
    return "—" if v is None else f"{v:+.2f}%"


def yi(v_kilo):  # 仟元 -> 億元
    return "—" if v_kilo is None else f"{v_kilo / 100000:,.2f} 億"


def to_kilo(v, unit):
    if v is None:
        return None
    if "百萬" in unit:
        return v * 1000
    if "千" in unit or "仟" in unit:
        return v
    if unit.strip() in ("元", "新台幣元"):
        return v / 1000
    return v


def ratio(a, b):
    return None if a is None or not b else a / b * 100


def growth(a, b):
    return None if a is None or not b else (a - b) / abs(b) * 100


def r1(v):
    return "—" if v is None else f"{v:.1f}%"


# ---------- revenue ----------
_rev_cache = {}


def rev_rows(roc_y, m):
    key = (roc_y, m)
    if key in _rev_cache:
        return _rev_cache[key]
    rows = {}
    for mkt in ("sii", "otc"):
        for kind in ("0", "1"):
            url = f"https://mopsov.twse.com.tw/nas/t21/{mkt}/t21sc03_{roc_y}_{m}_{kind}.html"
            s, b = http(url, retries=2)
            if s != 200 or not b:
                continue
            t = b.decode("big5hkscs", errors="ignore")
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", t, flags=re.S | re.I):
                cells = [re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ").strip()
                         for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, flags=re.S | re.I)]
                for i, c in enumerate(cells):
                    if re.fullmatch(r"\d{4,6}[A-Z]?", c) and len(cells) >= i + 10 and num(cells[i + 2]) is not None:
                        rows.setdefault(c, cells[i:i + 11])
                        break
    _rev_cache[key] = rows
    return rows


def check_revenue(stock, st, today, first):
    code, name = stock["code"], stock["name"]
    y, m = today.year, today.month
    months = []
    for back in (2, 1):  # oldest -> newest
        mm, yy = m - back, y
        while mm <= 0:
            mm += 12
            yy -= 1
        months.append((yy - 1911, mm))
    found = []
    for ry, mm in months:
        key = f"{ry}{mm:02d}"
        row = rev_rows(ry, mm).get(code)
        if row and key not in st["rev"]:
            found.append((key, ry, mm, row))
    for idx, (key, ry, mm, row) in enumerate(found):
        st["rev"].append(key)
        if first and idx < len(found) - 1:
            continue  # 首次只顯示最新一個月
        cur, prev, ly, mom, yoy, cum, cum_ly, cum_yoy = (num(x) for x in row[2:10])
        note = row[10] if len(row) > 10 and row[10] not in ("-", "") else ""
        prefix = "（上線首次載入的最近一筆）\n" if first else ""
        desc = (f"{prefix}**{ry + 1911} 年 {mm} 月合併營收：{yi(cur)}元**\n"
                f"• 月增率(MoM)：**{pct(mom)}**（上月 {yi(prev)}）\n"
                f"• 年增率(YoY)：**{pct(yoy)}**（去年同月 {yi(ly)}）\n"
                f"• 累計營收：{yi(cum)}（年增 {pct(cum_yoy)}，去年累計 {yi(cum_ly)}）")
        if note:
            desc += f"\n• 備註：{note}"
        send(mk(code, name, f"{ry + 1911}年{mm}月營收", "rev", desc))
    prev_y, prev_m = (y, m - 1) if m > 1 else (y - 1, 12)
    pkey = f"{prev_y - 1911}{prev_m:02d}"
    now = datetime.now(TZ)
    if pkey not in st["rev"] and pkey not in st["warned"] and (
            (today.day == 10 and now.hour >= 21) or today.day == 11):
        st["warned"].append(pkey)
        send(mk(code, name, "營收尚未公布", "warn",
                f"法定期限為每月 10 日，{prev_y} 年 {prev_m} 月營收截至目前仍查不到。"))


# ---------- events (重大訊息) ----------
_list_cache = {}


def mops_list(code, ry):
    key = (code, ry)
    if key not in _list_cache:
        res = mops("t05st01", {"companyId": code, "year": str(ry), "month": "all", "firstDay": "", "lastDay": ""})
        _list_cache[key] = (res or {}).get("data", [])
    return _list_cache[key]


def detail(item):
    p = item[5]
    res = mops(p["apiName"], p["parameters"])
    try:
        row = res["data"][0]
        return row[-1], row
    except Exception:
        return "", []


def ev_key(item):
    return f"{item[2]}|{item[3]}|{item[4].replace(chr(10), '')[:30]}"


def classify(title):
    t = re.sub(r"\s+", "", title)
    if "代子公司" in t or "代母公司" in t or "差異調節" in t:
        return None
    fin = "財務報告" in t or "財務報表" in t
    if "董事會" in t and "召開日期" in t and fin:
        return "board"
    if fin and ("相關資訊" in t or "決議通過" in t or "經董事會" in t or "業經董事會" in t):
        return "report"
    if "受邀參加" in t:
        return "invite"
    if "法人說明會" in t:
        return "meeting"
    return None


def parse_title_period(title):
    t = re.sub(r"\s+", "", title)
    m0 = re.search(r"(\d{2,3})年度?第([一二三四1-4])季", t)
    if m0:
        return int(m0.group(1)), QNUM.get(m0.group(2)) or int(m0.group(2))
    m = re.search(r"(\d{2,3})年(上半年度|前三季|度)", t)
    if not m:
        return None
    w = m.group(2)
    if w == "上半年度":
        q = 2
    elif w == "前三季":
        q = 3
    else:
        q = 4
    return int(m.group(1)), q


FIELDS = [("rev", r"營業收入"), ("gp", r"營業毛利\(毛損\)"), ("op", r"營業利益\(損失\)"), ("pre", r"稅前淨利\(淨損\)"),
          ("ni", r"本期淨利\(淨損\)"), ("nip", r"歸屬於母公司業主淨利\(損\)"), ("eps", r"基本每股盈餘\(損失\)"),
          ("assets", r"期末總資產"), ("liab", r"期末總負債"), ("equity", r"期末歸屬於母公司業主之權益")]


def parse_report(text):
    out = {}
    for k, label in FIELDS:
        m = re.search(label + r"\s*\(([^)]*)\)\s*[:：]\s*(\(?-?[\d,\.]+\)?)", text)
        if m:
            v = num(m.group(2).strip("()"))
            if m.group(2).startswith("(") and v is not None:
                v = -v
            out[k] = v if k == "eps" else to_kilo(v, m.group(1))
    return out


def get_report(code, fy, q, today_roc):
    for ry in ((fy, fy + 1) if q == 4 else (fy,)):
        if ry > today_roc:
            continue
        for it in mops_list(code, ry):
            if classify(it[4]) == "report" and parse_title_period(it[4]) == (fy, q):
                txt, _ = detail(it)
                d = parse_report(txt)
                if d:
                    return d
    return None


def fmt_report(code, fy, q, cur, today_roc):
    prev_q = get_report(code, fy, q - 1, today_roc) if q > 1 else None
    ly = get_report(code, fy - 1, q, today_roc)
    qname = "年度" if q == 4 else f"第{q}季"
    period = {1: "Q1 (1~3月)", 2: "Q2 (4~6月)", 3: "Q3 (7~9月)", 4: "Q4 (10~12月)"}[q]
    lines = [f"**{fy + 1911} 年{qname}合併財報已公布**（累計 1~{q * 3} 月）"]
    ly_gm = ratio((ly or {}).get("gp"), (ly or {}).get("rev"))
    ly_om = ratio((ly or {}).get("op"), (ly or {}).get("rev"))
    lines += [
        f"• 營收：**{yi(cur.get('rev'))}**（年增 {pct(growth(cur.get('rev'), (ly or {}).get('rev')))}）",
        f"• 毛利率：**{r1(ratio(cur.get('gp'), cur.get('rev')))}**" + (f"（去年同期 {r1(ly_gm)}）" if ly_gm is not None else ""),
        f"• 營業利益：{yi(cur.get('op'))}，營益率 **{r1(ratio(cur.get('op'), cur.get('rev')))}**" + (f"（去年同期 {r1(ly_om)}）" if ly_om is not None else ""),
        f"• 稅後淨利：{yi(cur.get('ni'))}（年增 {pct(growth(cur.get('ni'), (ly or {}).get('ni')))}）",
        f"• **累計 EPS：{cur.get('eps', '—')} 元**" + (f"（去年同期 {ly.get('eps')} 元）" if ly and ly.get('eps') is not None else ""),
    ]
    if q > 1 and prev_q:
        keys = ("rev", "gp", "op", "ni", "eps")
        sq = {k: cur[k] - prev_q[k] for k in keys if cur.get(k) is not None and prev_q.get(k) is not None}
        sly = None
        if ly:
            ly_prev = get_report(code, fy - 1, q - 1, today_roc)
            if ly_prev:
                sly = {k: ly[k] - ly_prev[k] for k in keys if ly.get(k) is not None and ly_prev.get(k) is not None}
        lines.append(f"\n**單季推算（{period}，累計相減）**")
        lines.append(f"• 營收：{yi(sq.get('rev'))}" + (f"（年增 {pct(growth(sq.get('rev'), sly.get('rev')))}）" if sly else ""))
        lines.append(f"• 毛利率：{r1(ratio(sq.get('gp'), sq.get('rev')))}，營益率：{r1(ratio(sq.get('op'), sq.get('rev')))}")
        if sq.get("eps") is not None:
            lines.append(f"• 單季 EPS：**{sq['eps']:.2f} 元**" + (f"（去年同季 {sly['eps']:.2f} 元）" if sly and sly.get('eps') is not None else ""))
    if cur.get("assets"):
        lines.append(f"\n期末總資產 {yi(cur['assets'])}，總負債 {yi(cur.get('liab'))}（負債比 {r1(ratio(cur.get('liab'), cur['assets']))}）")
    return "\n".join(lines)


def handle_item(stock, st, kind, item, first, today, today_roc):
    code, name = stock["code"], stock["name"]
    txt, row = detail(item)
    prefix = "（上線首次載入的最近一筆）\n" if first else ""
    pub = f"公告時間：{item[2]} {item[3]}"
    if kind == "report":
        per = parse_title_period(item[4])
        cur = parse_report(txt)
        if per and cur:
            if f"{per[0]}Q{per[1]}" not in st["reports"]:
                st["reports"].append(f"{per[0]}Q{per[1]}")
            desc = prefix + fmt_report(code, per[0], per[1], cur, today_roc) + f"\n\n{pub}"
            send(mk(code, name, f"{per[0] + 1911}年{'年度' if per[1] == 4 else 'Q' + str(per[1])}財報", "report", desc))
        else:
            send(mk(code, name, "財報公布", "report", prefix + item[4].strip() + "\n\n" + txt[:1500] + f"\n\n{pub}"))
    elif kind == "board":
        d = None
        m = re.search(r"董事會預計召開日期[:：]\s*(\d{2,3}/\d{1,2}/\d{1,2})", txt)
        if m:
            d = parse_roc_date(m.group(1))
        per = re.search(r"年季[:：]\s*(\S+)", txt)
        desc = (prefix + f"**董事會預計召開日：{d.isoformat() if d else '見公告'}**（通常當天收盤後公布財報）\n"
                f"• 提報財報：{per.group(1) if per else item[4].strip()}\n\n{pub}")
        pk = parse_title_period(per.group(1)) if per else None
        if d and d >= today:
            st["reminders"].append({"date": d.isoformat(), "kind": "board", "text": per.group(1) if per else "財報",
                                    "period": f"{pk[0]}Q{pk[1]}" if pk else "", "done": []})
        if first and d and (today - d).days > 60:
            return
        send(mk(code, name, "財報日期預告", "board", desc))
    elif kind in ("meeting", "invite"):
        def g(label):
            mm = re.search(label + r"[:：]\s*(.+)", txt)
            return mm.group(1).strip() if mm else None
        d = parse_roc_date(g(r"召開法人說明會之日期") or g(r"日期") or (row[8] if len(row) > 8 else ""))
        label = "活動日期" if kind == "invite" else "法說會日期"
        desc = (prefix + f"**{label}：{d.isoformat() if d else '見公告'}　{g('召開法人說明會之時間') or g('時間') or ''}**\n"
                f"• 地點：{g('召開法人說明會之地點') or g('地點') or '見公告'}\n"
                f"• 主題：{g('法人說明會擇要訊息') or g('擇要訊息') or item[4].strip()}\n\n{pub}")
        tm = re.search(r"(\d{1,2})\s*時\s*(\d{1,2})?", g("召開法人說明會之時間") or g("時間") or "")
        tstr = f"{int(tm.group(1)):02d}:{int(tm.group(2) or 0):02d}" if tm else ""
        if d and d >= today:
            st["meetings"].append({"date": d.isoformat(), "mat": False, "late": False, "text": "法說會" if kind == "meeting" else "投資活動"})
            st["reminders"].append({"date": d.isoformat(), "kind": "meeting", "text": "法說會" if kind == "meeting" else "投資活動",
                                    "time": tstr, "done": []})
        if first and d and (today - d).days > 200:
            return
        send(mk(code, name, "受邀參加投資活動" if kind == "invite" else "法說會公告", "meeting", desc))


def check_events(stock, st, today, first):
    code = stock["code"]
    today_roc = roc(today)
    items = []
    for ry in (today_roc - 1, today_roc):
        items += mops_list(code, ry)
    items.sort(key=lambda it: (it[2], it[3]))
    latest = {}
    todo = []
    for it in items:
        k = classify(it[4])
        if not k:
            continue
        key = ev_key(it)
        if key in st["seen"]:
            continue
        todo.append((k, it, key))
        latest[k] = key
    for k, it, key in todo:
        st["seen"].append(key)
        if first and latest.get(k) != key:
            continue
        handle_item(stock, st, k, it, first, today, today_roc)


def check_reminders(stock, st, today):
    code, name = stock["code"], stock["name"]
    now = datetime.now(TZ)
    for r in st["reminders"]:
        d = date.fromisoformat(r["date"])
        diff = (d - today).days
        done = r["done"]

        def once(tag):
            if tag in done:
                return False
            done.append(tag)
            return True

        if r["kind"] == "meeting":
            tm = r.get("time") or ""
            tline = f" {tm}" if tm else ""
            if diff == 3 and once("d3"):
                send(mk(code, name, f"3天後{r['text']}", "meeting", f"**{d.isoformat()}{tline} 有{r['text']}**（還有 3 天）。簡報與錄音會在會後上傳。"))
            if diff == 1 and once("d1"):
                send(mk(code, name, f"明天{r['text']}", "meeting", f"**明天（{d.isoformat()}）{tline} 有{r['text']}**，簡報與錄音會在會後上傳。"))
            if diff == 0:
                if once("d0"):
                    send(mk(code, name, f"今天{r['text']}", "meeting", f"**今天（{d.isoformat()}）{tline} 有{r['text']}**，簡報與錄音會在會後上傳。"))
                if tm and "soon" not in done:
                    mh, mm = int(tm[:2]), int(tm[3:])
                    start = now.replace(hour=mh, minute=mm, second=0, microsecond=0)
                    if now < start and now.hour >= max(8, min(mh - 1, 13)) and once("soon"):
                        send(mk(code, name, f"{r['text']}即將開始", "meeting", f"**{tm} 開始{r['text']}**，請準備。"))
        else:
            label = r["text"]
            if diff == 3 and once("d3"):
                send(mk(code, name, "財報3天後公布", "board", f"**{d.isoformat()} 董事會預計通過 {label}**（還有 3 天），通常收盤後公布。"))
            if diff == 1 and once("d1"):
                send(mk(code, name, "財報明天公布", "board", f"**明天（{d.isoformat()}）董事會預計通過 {label}**，通常收盤後公布。"))
            if diff == 0:
                if once("d0"):
                    send(mk(code, name, "財報今天公布", "board", f"**今天（{d.isoformat()}）董事會預計通過 {label}**，通常收盤後公布，公布後會立刻通知。"))
                if now.hour >= 16 and r.get("period") not in st["reports"] and once("pm"):
                    send(mk(code, name, "財報今晚留意", "board", f"**今天預計公布 {label}**，目前還沒查到，持續追蹤中。"))
    st["reminders"] = [r for r in st["reminders"] if (today - date.fromisoformat(r["date"])).days <= 3]


def check_materials(stock, st, today):
    code, name = stock["code"], stock["name"]
    for m in st["meetings"]:
        d = date.fromisoformat(m["date"])
        if m["mat"] or d > today or (today - d).days > 14:
            continue
        if (today - d).days >= 1 and not m.get("late"):
            m["late"] = True
            send(mk(code, name, f"{m.get('text', '法說會')}簡報尚未上傳", "warn",
                    f"**{d.isoformat()} 的{m.get('text', '法說會')}已結束，簡報/錄音還沒上傳到公開資訊觀測站**，持續追蹤，上傳後會立刻通知。"))
        links = []
        for lang, label in (("M", "中文"), ("E", "英文")):
            found = None
            for seq in ("001", "002"):
                for ext in ("pptx", "pdf"):
                    url = f"https://mopsov.twse.com.tw/nas/STR/{code}{d.strftime('%Y%m%d')}{lang}{seq}.{ext}"
                    s, _ = http(url, method="HEAD", retries=1)
                    if s == 200:
                        found = url
                        break
                if found:
                    break
            if found:
                links.append(f"[{label}簡報]({found})")
        if links:
            m["mat"] = True
            send(mk(code, name, "法說會簡報已上傳", "meeting", f"**{d.isoformat()} 法說會簡報已公開**\n" + "　".join(links)))
    st["meetings"] = [m for m in st["meetings"] if (today - date.fromisoformat(m["date"])).days <= 30]


def deadline_warn(stock, st, today):
    code, name = stock["code"], stock["name"]
    y = today.year
    cands = [(date(y, 5, 15), y - 1911, 1), (date(y, 8, 14), y - 1911, 2),
             (date(y, 11, 14), y - 1911, 3), (date(y, 3, 31), y - 1912, 4)]
    for dl, fy, q in cands:
        key = f"{fy}Q{q}"
        days = (dl - today).days
        if days < 0 or days > 7 or key in st["reports"]:
            continue
        thr = min(x for x in (7, 3, 1, 0) if days <= x)
        tag = f"dl{key}_{thr}"
        if tag in st["warned"]:
            continue
        for x in (7, 3, 1, 0):
            if x >= thr:
                st["warned"].append(f"dl{key}_{x}")
        send(mk(code, name, "財報期限將到", "warn",
                f"{fy + 1911} 年{'年度' if q == 4 else '第' + str(q) + '季'}財報法定期限為 **{dl.isoformat()}**（還有 {days} 天），目前尚未查到公布，公布後會立刻通知。"))


def main():
    wl = json.load(open(os.path.join(HERE, "watchlist.json"), encoding="utf-8"))
    state = json.load(open(STATE_PATH, encoding="utf-8")) if os.path.exists(STATE_PATH) else {}
    today = datetime.now(TZ).date()
    if "--date" in sys.argv:
        today = date.fromisoformat(sys.argv[sys.argv.index("--date") + 1])
    global CUR_HOOK
    for stock in wl:
        code = stock["code"]
        envs = stock.get("webhook_envs") or ([stock["webhook_env"]] if stock.get("webhook_env") else [])
        CUR_HOOK = [os.environ[e] for e in envs if os.environ.get(e)]
        if envs and not CUR_HOOK and not DRY:
            print("MISSING webhook env", envs, file=sys.stderr)
            continue
        st = state.setdefault(code, {})
        first = not st.get("init")
        for k, v in (("seen", []), ("rev", []), ("reports", []), ("warned", []), ("meetings", []), ("reminders", [])):
            st.setdefault(k, v)
        try:
            check_revenue(stock, st, today, first)
            check_events(stock, st, today, first)
            check_reminders(stock, st, today)
            check_materials(stock, st, today)
            deadline_warn(stock, st, today)
            st["init"] = True
        except Exception as e:
            print("ERROR", code, repr(e), file=sys.stderr)
    if not DRY:
        json.dump(state, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
