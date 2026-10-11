"""彩晶(6116)官網「法人說明會」頁面監控：出現新場次就發 Discord。
官網對一般 HTTP 請求回 403，所以用 Playwright 無頭瀏覽器。狀態存 hannstar_state.json。
環境變數: DISCORD_WEBHOOK_PANEL"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

URL = "https://www.hannstar.com/investors/conference"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "hannstar_state.json")
HOOK = os.environ.get("DISCORD_WEBHOOK_PANEL", "")
TZ = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
JS_ROWS = """() => [...document.querySelectorAll('table tr')].map(tr => {
  const tds = [...tr.querySelectorAll('td')].map(td => (td.innerText || '').replace(/\\s+/g, ' ').trim());
  const a = tr.querySelector('a[href]');
  return { date: tds[0] || '', title: tds[1] || '', pdf: a ? a.href : '' };
}).filter(r => /^\\d{4}\\/\\d{1,2}\\/\\d{1,2}$/.test(r.date))"""


def post(embed):
    if not HOOK:
        print("[DRY]", json.dumps(embed, ensure_ascii=False))
        return
    req = urllib.request.Request(HOOK, data=json.dumps({"embeds": [embed]}).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "User-Agent": "tw-stock-alerts"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print("discord", r.status)


def fetch_rows():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=UA, locale="zh-TW", viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        resp = page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        status = resp.status if resp else None
        page.wait_for_selector("table tr td", timeout=30000)
        page.wait_for_timeout(2000)
        rows = page.evaluate(JS_ROWS)
        browser.close()
        return status, rows


def main():
    st = json.load(open(STATE_PATH, encoding="utf-8")) if os.path.exists(STATE_PATH) else {}
    st.setdefault("conference", [])
    st.setdefault("fail_count", 0)
    st.setdefault("last_status", "")
    st.setdefault("warned_fail", False)
    today = datetime.now(TZ).date().isoformat()
    try:
        status, rows = fetch_rows()
        if not rows:
            raise RuntimeError(f"HTTP {status} 但頁面沒有場次資料(可能改版)")
        seen = set(st["conference"])
        new = [r for r in rows if f"{r['date']}|{r['title']}" not in seen]
        first = not st["conference"]
        for r in sorted(new, key=lambda x: x["date"]):
            st["conference"].append(f"{r['date']}|{r['title']}")
            if first:
                continue  # 首次建立基準不通知
            desc = f"**{r['date']}　{r['title']}**\n"
            if r["pdf"]:
                desc += f"[簡報 PDF]({r['pdf']})\n"
            desc += f"\n官網：{URL}"
            post({"title": "彩晶(6116)｜官網新增法說會", "description": desc, "color": 0x2E86C1})
        st["fail_count"] = 0
        st["warned_fail"] = False
        st["last_status"] = f"ok {len(rows)} 場"
        st["last_ok_date"] = today
        print("ok", len(rows), "rows;", "new:", len(new), "first:", first)
    except Exception as e:
        st["fail_count"] += 1
        st["last_status"] = f"fail: {str(e)[:120]}"
        print("FAIL", repr(e), file=sys.stderr)
        if st["fail_count"] >= 6 and not st["warned_fail"]:  # 連續約 1.5 天都失敗才提醒一次
            st["warned_fail"] = True
            post({"title": "彩晶(6116)｜官網法說會頁檢查失敗",
                  "description": f"已連續 {st['fail_count']} 次無法讀取（可能被擋或官網改版）。\n錯誤：{str(e)[:200]}\n"
                                 f"法說會仍可從觀測站公告（受邀參加法說會）收到通知。", "color": 0xE67E22})
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
