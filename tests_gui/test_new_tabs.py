"""
tests_gui/test_new_tabs.py
Playwright GUI smoke test for the v4.27 tabs (Sup/Res Viz, Algo Lab, Correlation).

Unlike every other module in this repo, this test needs a browser AND a
*running* dashboard -- it does not spin one up itself, so it can't run inside
the plain `--self-test` convention used everywhere else. Run it manually
whenever those tabs change:

    1. Make sure trading_dashboard.py is running on port 5003
       (python back-trading/trading_dashboard.py)
    2. pip install playwright   (already present in this environment, v1.61.0)
       playwright install chromium   (one-time, if not already installed)
    3. python tests_gui/test_new_tabs.py [--url http://localhost:5003] [--headed]

Deliberately non-destructive: it only ever clicks "Preview (dry-run)" on the
Algo Lab tab, never "Submit Grid" -- a real click on Submit Grid inserts real
PENDING paper orders that trader/broker.py will submit to IB. Screenshots are
written to tests_gui/screenshots/ for a visual record of each run.
"""

import sys
import argparse
from pathlib import Path

_HERE = Path(__file__).parent
_SHOTS = _HERE / "screenshots"


def run(base_url: str, headed: bool = False) -> bool:
    from playwright.sync_api import sync_playwright

    _SHOTS.mkdir(exist_ok=True)
    failures = []

    def check(label, cond):
        status = "PASS" if cond else "FAIL"
        print(f"  [{status}] {label}")
        if not cond:
            failures.append(label)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page(viewport={"width": 1400, "height": 900})

        print(f"Loading {base_url} ...")
        # "load" not "networkidle" -- this dashboard polls prices every 5s by
        # design (pollPrices()/setInterval), so network never goes idle.
        page.goto(base_url, wait_until="load", timeout=15000)
        page.wait_for_timeout(1500)
        check("page title contains 'Trading Dashboard'", "Trading Dashboard" in page.title())
        check("version badge v4.27 visible", page.locator("text=v4.27").count() > 0)

        # ── Sup/Res Viz tab ──────────────────────────────────────────────
        print("Sup/Res Viz tab ...")
        page.click("#btn-srviz-tab")
        page.wait_for_timeout(1500)
        check("srviz tab-pane visible", page.locator("#tab-srviz").is_visible())
        check("srviz symbol selector present", page.locator("#sv-sym").count() == 1)
        check("srviz chart div rendered something",
              page.locator("#sv-chart .plotly, #sv-chart .text-muted").count() > 0)
        page.screenshot(path=str(_SHOTS / "srviz.png"))

        # ── Algo Lab tab (preview only -- never submit) ──────────────────
        print("Algo Lab tab ...")
        page.click("#btn-algolab-tab")
        page.wait_for_timeout(1500)
        check("algolab tab-pane visible", page.locator("#tab-algolab").is_visible())
        check("algo lab grid badge populated",
              "grid:" in (page.locator("#al-grid-badge").inner_text() or ""))
        check("P&L summary table has rows or empty-state row",
              page.locator("#al-summary-tbody tr").count() > 0)

        preview_btn = page.locator("button:has-text('Preview (dry-run)')")
        check("Preview button present", preview_btn.count() == 1)
        check("Submit Grid button present (NOT clicking it)",
              page.locator("button:has-text('Submit Grid')").count() == 1)
        if preview_btn.count():
            preview_btn.first.click()
            page.wait_for_timeout(4000)  # live price fetch can take a few seconds
            msg = page.locator("#al-msg").inner_text()
            check(f"preview completed without error (msg: {msg!r})",
                  "failed" not in msg.lower())
        page.screenshot(path=str(_SHOTS / "algolab.png"))

        # ── Correlation tab ───────────────────────────────────────────────
        print("Correlation tab ...")
        page.click("#btn-correlation-tab")
        page.wait_for_timeout(1500)
        check("correlation tab-pane visible", page.locator("#tab-correlation").is_visible())
        check("heatmap rendered", page.locator("#corr-heatmap .plotly").count() > 0)
        missing_badge_visible = page.locator("#corr-missing").is_visible()
        check("missing-symbols badge hidden now that all 4 symbols have bars.db data "
              "(MNQ was backfilled during this session -- flip this check if that regresses)",
              not missing_badge_visible)
        page.screenshot(path=str(_SHOTS / "correlation.png"))

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
