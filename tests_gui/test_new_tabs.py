"""
tests_gui/test_new_tabs.py
Playwright GUI smoke test for the v5.00 nav rebuild (left rail + contextual
top tabs + unified filter bar + busy strip) and the v4.27 tabs it now hosts
(Sup/Res Viz, Algo Lab [Grid & Submit / P&L Breakdown], Correlation).

Unlike every other module in this repo, this test needs a browser AND a
*running* dashboard -- it does not spin one up itself, so it can't run inside
the plain `--self-test` convention used everywhere else. Run it manually
whenever the nav or these tabs change:

    1. Make sure trading_dashboard.py is running on port 5003
       (python back-trading/trading_dashboard.py)
    2. pip install playwright   (already present in this environment, v1.61.0)
       playwright install chromium   (one-time, if not already installed)
    3. python tests_gui/test_new_tabs.py [--url http://localhost:5003] [--headed]

Deliberately non-destructive: it only ever clicks "Preview (dry-run)" on the
Algo Lab > Grid & Submit tab, never "Submit Grid" -- a real click on Submit
Grid inserts real PENDING paper orders that trader/broker.py will submit to
IB. Screenshots are written to tests_gui/screenshots/ for a visual record.
"""

import sys
import argparse
from pathlib import Path

_HERE = Path(__file__).parent
_SHOTS = _HERE / "screenshots"

# rail group -> (caption text, [(tab button id, tab pane id), ...])
GROUPS = {
    "overview": ("Overview", [("btn-overview-tab", "tab-overview")]),
    "levels":   ("Levels",   [("btn-lines-tab", "tab-lines"), ("btn-sandbox-tab", "tab-sandbox")]),
    "explore":  ("Explore",  [("btn-srviz-tab", "tab-srviz"), ("btn-correlation-tab", "tab-correlation")]),
    "charts":   ("Charts",   [("btn-graph-tab", "tab-graph"), ("btn-all-tab", "tab-all"), ("btn-test-tab", "tab-test")]),
    "algolab":  ("Algo Lab", [("btn-algolab-grid-tab", "tab-algolab-grid"), ("btn-algolab-pnl-tab", "tab-algolab-pnl")]),
    "trading":  ("Trading",  [("btn-trades-tab", "tab-trades"), ("btn-sub-tab", "tab-submitted")]),
}


def run(base_url: str, headed: bool = False) -> bool:
    from playwright.sync_api import sync_playwright

    _SHOTS.mkdir(exist_ok=True)
    failures = []
    console_errors = []

    def check(label, cond):
        status = "PASS" if cond else "FAIL"
        print(f"  [{status}] {label}")
        if not cond:
            failures.append(label)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(str(e)))

        print(f"Loading {base_url} ...")
        # "load" not "networkidle" -- this dashboard polls prices every 5s by
        # design (pollPrices()/setInterval), so network never goes idle.
        page.goto(base_url, wait_until="load", timeout=15000)
        page.wait_for_timeout(1500)
        check("page title contains 'Trading Dashboard'", "Trading Dashboard" in page.title())
        check("version badge v5.00 visible", page.locator("text=v5.00").count() > 0)
        check("rail present with 6 groups", page.locator(".rail-item[data-group]").count() == 6)
        check("Overview is the default active pane", page.locator("#tab-overview").is_visible())
        check("Overview rail item starts active",
              "active" in (page.locator('.rail-item[data-group="overview"]').get_attribute("class") or ""))

        # ── Walk every rail group -> every tab in it ──────────────────────
        for group_id, (caption, tabs) in GROUPS.items():
            print(f"Rail group: {group_id}")
            page.click(f'.rail-item[data-group="{group_id}"]')
            page.wait_for_timeout(400)
            check(f"[{group_id}] rail item shows active",
                  "active" in (page.locator(f'.rail-item[data-group="{group_id}"]').get_attribute("class") or ""))
            # innerText reflects CSS text-transform:uppercase on this element,
            # so compare case-insensitively -- the underlying text is mixed-case.
            check(f"[{group_id}] tabstrip caption reads '{caption}'",
                  page.locator("#rail-group-caption").inner_text().strip().lower() == caption.lower())
            check(f"[{group_id}] only this group's tab buttons are visible",
                  page.locator(f'#mainTab li[data-group="{group_id}"]:visible').count() == len(tabs))

            for btn_id, pane_id in tabs:
                page.click(f"#{btn_id}")
                page.wait_for_timeout(1200)
                check(f"[{group_id}] {pane_id} pane becomes visible", page.locator(f"#{pane_id}").is_visible())
            page.screenshot(path=str(_SHOTS / f"group_{group_id}.png"))

        # ── Date-range control: visible only on Lines/Graph ───────────────
        page.click('.rail-item[data-group="levels"]')
        page.click("#btn-lines-tab")
        page.wait_for_timeout(300)
        check("date-range control visible on Lines", page.locator("#date-range-wrap").is_visible())
        page.click(".rail-item[data-group=\"explore\"]")
        page.click("#btn-srviz-tab")
        page.wait_for_timeout(300)
        check("date-range control hidden on Sup/Res Viz", not page.locator("#date-range-wrap").is_visible())

        # ── Busy strip: idle by default, lights up during a fetch ─────────
        page.click('.rail-item[data-group="algolab"]')
        page.click("#btn-algolab-grid-tab")
        page.wait_for_timeout(500)
        check("busy strip idle before any action", "idle" in (page.locator("#busy-strip").get_attribute("class") or ""))
        preview_btn = page.locator("button:has-text('Preview (dry-run)')")
        check("Preview button present", preview_btn.count() == 1)
        check("Submit Grid button present (NOT clicking it)", page.locator("button:has-text('Submit Grid')").count() == 1)
        if preview_btn.count():
            preview_btn.first.click()
            busy_seen = False
            for _ in range(10):
                cls = page.locator("#busy-strip").get_attribute("class") or ""
                if "idle" not in cls:
                    busy_seen = True
                    break
                page.wait_for_timeout(150)
            check("busy strip lit up during the preview fetch", busy_seen)
            page.wait_for_timeout(4000)  # live price fetch can take a few seconds
            msg = page.locator("#al-msg").inner_text()
            check(f"preview completed without error (msg: {msg!r})", "failed" not in msg.lower())
        check("busy strip returns to idle after fetch completes",
              "idle" in (page.locator("#busy-strip").get_attribute("class") or ""))

        # ── Overview quick-links navigate cross-group ─────────────────────
        page.click('.rail-item[data-group="overview"]')
        page.click("#btn-overview-tab")
        page.wait_for_timeout(500)
        page.click("text=Explore → Correlation")
        page.wait_for_timeout(500)
        check("Overview quick-link switched rail to Explore",
              "active" in (page.locator('.rail-item[data-group="explore"]').get_attribute("class") or ""))
        check("Overview quick-link opened Correlation pane", page.locator("#tab-correlation").is_visible())

        # ── No console/page errors accumulated during the whole walk ──────
        check(f"no console errors during test (saw {len(console_errors)})", len(console_errors) == 0)
        if console_errors:
            for e in console_errors[:10]:
                print("    console error:", e)

        browser.close()

    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        return False
    print("[gui-test] test_new_tabs: PASS")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:5003")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    args = parser.parse_args()
    sys.exit(0 if run(args.url, args.headed) else 1)
