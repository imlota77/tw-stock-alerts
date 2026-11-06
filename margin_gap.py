#!/usr/bin/env python3
"""大盤跌幅 vs 融資退幅 每日比較圖，發 Discord（DISCORD_WEBHOOK_MARKET）。

口徑（已用 2026/11/05 截圖驗證）：
- 大盤跌幅 = 收盤 / 近 N 個交易日「盤中最高」的最高點 - 1（證交所 MI_5MINS_HIST）
- 融資退幅 = 融資金額餘額 / 近 N 個交易日融資金額餘額最高點 - 1（證交所 MI_MARGN 融資金額(仟元)）
- 差距 = 融資退幅 - 大盤跌幅（百分點；越小代表融資砍得越接近大盤跌幅）
"""
import datetime
import io
import json
import os
import sys
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "margin_gap_state.json")
HOOK = os.environ.get("DISCORD_WEBHOOK_MARKET", "")
DRY = "--dry" in sys.argv
FORCE = "--force" in sys.argv
UA = {"User-Agent": "Mozilla/5.0"}
WINDOW = 60      # 找波段高點的交易日數
BARS = 6         # 左圖顯示天數
TREND = 8        # 右圖顯示天數


def get_json(url):
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as ex:
            print("GET FAIL", url, repr(ex), file=sys.stderr)
            time.sleep(3)
    return {}


def n(s):
    return float(str(s).replace(",", ""))


def roc(s):
    y, m, d = s.split("/")
    return f"{int(y) + 1911}{m}{d}"


def months_back(k):
    t = datetime.date.today().replace(day=1)
    out = []
    for _ in range(k):
        out.append(t.strftime("%Y%m01"))
        t = (t - datetime.timedelta(days=1)).replace(day=1)
    return out[::-1]


def index_data():
    close, high = {}, {}
    for ym in months_back(4):
        j = get_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?date={ym}&response=json")
        for r in j.get("data") or []:
            close[roc(r[0])] = (n(r[4]), n(r[5]))
        j = get_json(f"https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?date={ym}&response=json")
        for r in j.get("data") or []:
            high[roc(r[0])] = n(r[2])
        time.sleep(1)
    return close, high


def margin_on(date):
    j = get_json(f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={date}&selectType=MS&response=json")
    if j.get("stat") != "OK" or j.get("date") != date:
        return None
    rows = {r[0]: r for r in j["tables"][0]["data"]}
    r = rows.get("融資金額(仟元)")
    return int(n(r[5])) if r else None


def compute(dates, close, high, mg):
    out = []
    for i, d in enumerate(dates):
        win = dates[max(0, i - WINDOW + 1): i + 1]
        hi_d = max(win, key=lambda x: high.get(x, close[x][0]))
        mg_win = [x for x in win if x in mg]
        if d not in mg or not mg_win:
            continue
        mp_d = max(mg_win, key=lambda x: mg[x])
        idx_pct = (close[d][0] / high.get(hi_d, close[hi_d][0]) - 1) * 100
        mg_pct = (mg[d] / mg[mp_d] - 1) * 100
        prev = dates[i - 1] if i else None
        out.append({
            "date": d, "idx": idx_pct, "mg": mg_pct, "gap": abs(idx_pct) - abs(mg_pct),
            "hi_d": hi_d, "hi": high.get(hi_d), "mp_d": mp_d, "mp": mg[mp_d],
            "close": close[d][0], "chg": close[d][1],
            "mg_chg": (mg[d] - mg[prev]) / 1e5 if prev in mg else None,
        })
    return out


def md(d):
    return f"{d[4:6]}/{d[6:]}"


def render(rows):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    for f in ["Noto Sans CJK TC", "Microsoft JhengHei", "Noto Sans CJK JP"]:
        if any(f == x.name for x in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = f
            break
    plt.rcParams["axes.unicode_minus"] = False
    PUR, RED, GAP, ORG, INK, MUT = "#6D28D9", "#DC2626", "#F5E6C8", "#D97706", "#1F2937", "#6B7280"

    last = rows[-1]
    bars = rows[-BARS:]
    trend = rows[-TREND:]
    prev_gap = rows[-2]["gap"] if len(rows) > 1 else None
    word = "縮小" if prev_gap is not None and last["gap"] < prev_gap else "擴大"

    fig = plt.figure(figsize=(16, 9), dpi=110, facecolor="#F3F4F6")
    fig.text(0.04, 0.955, "上市大盤・信用交易", fontsize=13, color=MUT)
    fig.text(0.96, 0.955, f"資料日期 {last['date'][:4]}/{md(last['date'])}", fontsize=13, color=MUT, ha="right")
    t1 = fig.text(0.04, 0.885, f"大盤跌幅 vs 融資退幅：差距{word}到 ", fontsize=30, color=INK, weight="bold")
    bb = t1.get_window_extent(renderer=fig.canvas.get_renderer()).transformed(fig.transFigure.inverted())
    fig.text(bb.x1, 0.885, f"{last['gap']:.2f}", fontsize=30, color="#C2410C", weight="bold")

    # 左：跌幅 vs 退幅
    ax = fig.add_axes([0.06, 0.08, 0.50, 0.70])
    ax.set_title(f"跌幅 vs 退幅　近 {len(bars)} 個交易日・大盤 {md(last['hi_d'])} 盤中高點 {last['hi']:,.0f}、融資 {md(last['mp_d'])} 高點 {last['mp'] / 1e5:,.0f} 億起算",
                 loc="left", fontsize=12, color=INK, pad=10)
    y = 0
    ticks, labels = [], []
    lo = min(min(r["idx"], r["mg"]) for r in bars)
    for r in reversed(bars):
        ax.barh(y + 0.22, r["idx"], height=0.38, color=PUR)
        ax.barh(y - 0.22, r["idx"], height=0.38, color=GAP, edgecolor="#E5C98F", linestyle="--", linewidth=0.8)
        ax.barh(y - 0.22, r["mg"], height=0.38, color=RED)
        ax.text(-0.25, y + 0.22, f"{r['idx']:.2f}%", va="center", ha="left", fontsize=10, color="white", weight="bold")
        ax.text(-0.25, y - 0.22, f"{r['mg']:.2f}%", va="center", ha="left", fontsize=10, color="white", weight="bold")
        ax.text(lo * 1.03, y, f"差距 {r['gap']:.2f}", va="center", ha="left", fontsize=13, color=ORG, weight="bold")
        ticks.append(y)
        mc = f"融資 {r['mg_chg']:+,.0f} 億" if r["mg_chg"] is not None else ""
        labels.append(f"{md(r['date'])}\n指數 {r['chg']:+,.0f}\n{mc}")
        y += 1.2
    ax.set_yticks(ticks)
    ax.set_yticklabels(labels, fontsize=10, color=INK)
    ax.set_xlim(0, lo * 1.25)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    for s in ["top", "right", "left"]:
        ax.spines[s].set_visible(False)
    ax.set_facecolor("white")
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color=PUR, label="大盤跌幅"), Patch(color=RED, label="融資退幅"), Patch(color=GAP, label="差距（百分點）")],
              loc="lower left", ncol=3, frameon=False, fontsize=11, bbox_to_anchor=(0, 1.04))

    # 右：差距走勢
    bx = fig.add_axes([0.64, 0.30, 0.32, 0.46])
    bx.set_title("差距走勢　百分點", loc="left", fontsize=13, color=INK, pad=10)
    xs = list(range(len(trend)))
    gs = [r["gap"] for r in trend]
    bx.plot(xs, gs, color=ORG, marker="o", markerfacecolor="white", linewidth=2.2)
    bx.fill_between(xs, gs, min(gs) - 1, color=ORG, alpha=0.15)
    for x, g in zip(xs, gs):
        bx.text(x, g + 0.15, f"{g:.2f}", ha="center", fontsize=11, color="#C2410C" if x == xs[-1] else INK, weight="bold")
    bx.set_xticks(xs)
    bx.set_xticklabels([md(r["date"]) for r in trend], fontsize=10)
    bx.set_ylim(min(gs) - 1, max(gs) + 1)
    for s in ["top", "right"]:
        bx.spines[s].set_visible(False)
    bx.grid(axis="y", alpha=0.3)

    ratio = last["mg"] / last["idx"] * 100 if last["idx"] else 0
    mc = f"融資{'減少' if (last['mg_chg'] or 0) < 0 else '增加'} {abs(last['mg_chg'] or 0):,.0f} 億，" if last["mg_chg"] is not None else ""
    pg = f"差距從 {prev_gap:.2f} {word}到 {last['gap']:.2f}，" if prev_gap is not None else ""
    note = (f"{md(last['date'])} 大盤{'下跌' if last['chg'] < 0 else '上漲'} {abs(last['chg']):,.0f} 點，{mc}\n"
            f"退幅 {last['mg']:.2f}%。{pg}\n融資退幅為大盤跌幅的 {ratio:.0f}%。")
    fig.text(0.64, 0.10, note, fontsize=13, color=INK, linespacing=1.7)
    fig.text(0.96, 0.02, "資料：臺灣證券交易所（上市）｜差距 = 大盤跌幅 - 融資退幅（百分點）｜大盤以波段盤中高點、融資以融資金額波段高點起算", fontsize=9, color=MUT, ha="right")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    return buf.getvalue(), note


def send(png, text):
    if DRY or not HOOK:
        open(os.path.join(HERE, "margin_gap_preview.png"), "wb").write(png)
        print("[DRY]", text)
        return True
    b = uuid.uuid4().hex
    payload = json.dumps({"content": text, "attachments": [{"id": 0, "filename": "margin_gap.png"}]}, ensure_ascii=False).encode()
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\nContent-Type: application/json\r\n\r\n").encode() + payload + \
           (f"\r\n--{b}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"margin_gap.png\"\r\nContent-Type: image/png\r\n\r\n").encode() + png + \
           f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(HOOK, data=body, headers={"Content-Type": f"multipart/form-data; boundary={b}", **UA})
    try:
        urllib.request.urlopen(req, timeout=60).read()
        return True
    except Exception as ex:
        print("DISCORD FAIL", repr(ex), file=sys.stderr)
        return False


def main():
    state = json.load(open(STATE_PATH, encoding="utf-8")) if os.path.exists(STATE_PATH) else {}
    mg = {k: v for k, v in (state.get("margin") or {}).items()}
    close, high = index_data()
    dates = sorted(close)
    if not dates:
        print("NO INDEX DATA", file=sys.stderr)
        return
    today = dates[-1]
    if state.get("last") == today and not FORCE:
        print("already sent", today)
        return
    need = [d for d in dates[-(WINDOW + TREND):] if d not in mg]
    for d in need:
        v = margin_on(d)
        if v:
            mg[d] = v
        time.sleep(1.2)
    state["margin"] = {k: mg[k] for k in sorted(mg)[-(WINDOW + TREND + 20):]}
    json.dump(state, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if today not in mg:
        print("waiting for margin data", today, file=sys.stderr)
        return
    rows = compute(dates, close, high, mg)
    png, note = render(rows)
    last = rows[-1]
    text = f"## 大盤跌幅 vs 融資退幅（{last['date'][:4]}/{md(last['date'])}）\n大盤 {last['idx']:.2f}%｜融資 {last['mg']:.2f}%｜差距 **{last['gap']:.2f}** 百分點"
    if send(png, text) and not DRY:
        state["last"] = today
        json.dump(state, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
