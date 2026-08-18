# ORCHESTRATOR — Briefing for a Higher-Level Claude Code Session Coordinating This Project Alongside Its Siblings
> For a "meta" Claude Code session (or human) operating multiple Claude Code instances in
> parallel across this project and related algo projects — not for a session working inside
> just one repo. If you're only ever going to `cd` into one project and stay there, read
> that project's own `CLAUDE_STATE.md`/`ORIENTATION.md`/`OPERATIONS.md` instead; this doc is
> about the seams *between* projects.
> Written 2026-08-18.

---

## 1. The map

```
                         IB Gateway (paper, port 4002, via IBC — C:\IBC, not a git repo)
                                        │
        ┌───────────────────┬──────────┼──────────────┬───────────────────┐
        │                   │          │              │                   │
  CriticalCorallations2026  │    Fetcher2026           │             (other algo
  ("CC2026") :5003          │    :5050 + :5004         │              projects —
  trading brain: dashboard, │    tick-CSV + OHLCV-bars │              see §6)
  broker.py, decider.py,    │    fetch pipelines        │
  Algo Lab, Correlation     │                            │
        │                   │                            │
        └─────────┬─────────┘                            │
                   │                                      │
              galao.db  ◄──────────────────────── GevaExtract :5005
        (shared trading DB,        writes commands rows tagged
         C:\Projects\              source='geva_extract' via
         CriticalCorallations2026\ insert-commands.py (WAL-safe)
         trader\data\galao.db)     — also scrapes Facebook for
                                    S/R lines into its own geva.db
```

Also present on this machine but not part of the live trading loop: `Backtrader2026`
(separate backtesting system, own Claude session), `Galgo2026` (the pre-split original
monolith — mostly legacy, but its `june\` subtree is still where Fetcher2026's tick-CSV
history and `fetch_progress.db` actually live, a known path debt — see §5), and
`CorrellationAnalyzer` (referenced in `Fetcher2026\BARS1S_STATUS.md` §0h as a consumer of
1-second bars for signal research — check for its own docs if you need current detail, not
covered here).

Each of CC2026, Fetcher2026, and GevaExtract has **"its own Claude session"** by design —
they were split out specifically so a session working in one doesn't need the full context
of the others. This doc exists for the case where you (or a coordinating process) need to
reason about *all* of them at once — dependency ordering, shared-resource contention,
cross-repo incidents.

---

## 2. Golden rules for operating multiple sessions/projects in parallel

1. **IB Gateway's historical-data pacing limit (60 requests / 10-min rolling window) is
   account-wide, not per-process.** Every fetcher, backfill script, or dashboard price
   query drawing from port 4002 shares this one budget. Before spinning up a new
   concurrent IB-hitting process, check what's already running (`Get-CimInstance
   Win32_Process -Filter "Name='python.exe'"` and inspect command lines) — three heavy
   fetchers running at once has already caused a real, documented throughput collapse
   (`Fetcher2026\BARS1S_STATUS.md` §0d: ~88% failure rate per request when 3 fetchers +
   the dashboard all hit Gateway simultaneously). Fetcher2026's own bars pipeline solved
   this internally with a weighted single-active-group scheduler
   (`bars_fetch_watchdog.py`) — don't fight that scheduler by manually launching a second
   fetcher instance outside it.

2. **`galao.db` has exactly one writer-safety mechanism: the `source` column on
   `commands`.** CC2026's own decider writes `source='critical_line'` (and `algo_lab` for
   Algo Lab-submitted paper trades); GevaExtract writes `source='geva_extract'` via a
   Python bridge script (`insert-commands.py`) that opens WAL mode correctly. `broker.py`
   polls by status, mostly source-agnostic. **If you add a fourth order-creation surface
   anywhere in this ecosystem, give it its own distinct `source` tag** — this is the only
   thing preventing the independent UIs from corrupting each other's bookkeeping. Never
   write directly to `commands` from a new script without going through this convention.

3. **Never open two concurrent writer connections to the same SQLite DB file without WAL
   mode.** `galao.db` is WAL; `geva.db` (GevaExtract's own) is loaded whole-file into
   memory by `sql.js` and only the JS side ever calls `.save()` — its read path for
   `galao.db` deliberately never calls `.save()` either, specifically so it can't disturb
   the WAL file the Python broker is actively writing. Follow this pattern (read-only via
   an in-memory load, OR proper WAL writes) for any new cross-project DB access.

4. **A bare `python` on PATH is not reliable across this ecosystem.** Different shells /
   sessions can resolve it to different interpreters (a project-local `.venv`, a global
   install, etc.), and the required package set differs (`psutil` is needed by
   Fetcher2026's watchdogs but may be absent from an unrelated venv that happens to shadow
   `python` on PATH). This exact mismatch crash-looped a production watchdog on
   2026-08-17. **Always launch cross-project scripts with a fully-qualified interpreter
   path**, and confirm which interpreter has which packages before assuming a script will
   run cleanly in a shell you didn't set up yourself.

5. **Don't touch another project's forbidden/legacy ports without checking who depends on
   them first.** `trader/visualizer/app.py` (port 5001) and `back-trading/algo_dashboard.py`
   (port 5002) are both marked "do not start" in CC2026's own docs — but GevaExtract's
   `server.js` still calls `localhost:5001/api/cancel-all` for an IB global-cancel relay.
   Retiring a "legacy" port from one project's perspective can silently break a feature in
   another. Grep all three repos for a port number before deciding it's safe to kill.

6. **Config-file resolution is a repeated footgun across this ecosystem.** Both
   `lib/config_loader.py` implementations (CC2026's and Fetcher2026's — they diverged, not
   shared code despite the similar name) cache on first call and resolve to whichever
   `config.yaml` is nearest the *launching* script. CC2026 alone has two
   (`trader/config.yaml` vs `back-trading/config.yaml`). If you're writing anything that
   imports across these repos' module boundaries, verify which config actually loaded —
   don't assume.

7. **Windows Scheduled Tasks running as SYSTEM can't see the interactive user's config
   files, and can't easily be killed from a non-elevated shell either.** This caused a real
   19-day silent outage (`Fetcher2026\BARS1S_STATUS.md` §0m /
   `Fetcher2026\OPERATIONS.md` §4) — a SYSTEM-context watchdog kept trying to restart IB
   Gateway, failing every time because `%USERPROFILE%` under SYSTEM doesn't resolve to
   `C:\Users\<you>`, and repeated Access Denied errors blocked killing it even as a local
   admin (UAC token filtering). If you're setting up any new scheduled automation for this
   ecosystem, register it under the interactive user's principal, not SYSTEM, unless you
   have a specific reason and have verified path resolution under that account first.

8. **Paper trading only, everywhere, always.** Every project in this ecosystem connects
   exclusively to IB port 4002 (paper). Port 4001 (live) must never be wired into any of
   these projects' configs. Treat any P&L figure anywhere in this ecosystem as simulated.

---

## 3. Before touching anything: check current health

```powershell
foreach ($p in 4002,5003,5004,5005,5050) {
  $conn = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
  if ($conn) { "$p LISTENING (pid $($conn[0].OwningProcess))" } else { "$p NOT LISTENING" }
}
Invoke-WebRequest http://localhost:5003/api/session/status -UseBasicParsing
Get-NetTCPConnection -LocalPort 4002 -State Established -ErrorAction SilentlyContinue | Measure-Object
Get-ScheduledTaskInfo -TaskName GevaAutoTrade
Get-ScheduledTaskInfo -TaskName DailyExtract -TaskPath "\GevaExtract\"
```

Full breakdown of what "healthy" looks like per component: `Fetcher2026\OPERATIONS.md` and
`GevaExtract\OPERATIONS.md` (the latter covers CC2026 + Gateway too — read it first for
anything Gateway-related). Standing known-issue list (don't re-diagnose these from
scratch, they're already documented):

| Issue | Where documented | Status |
|---|---|---|
| SYSTEM-context Gateway-restart watchdog misconfigured | `Fetcher2026\OPERATIONS.md` §4 | Diagnosed, not fixed — needs elevated session |
| GevaExtract auto-trade builds 0 candidates off stale lines | `GevaExtract\OPERATIONS.md` incident log | Diagnosed, fix drafted, needs sign-off (changes live trading logic) |
| CC2026's own `PostToolUse` self-test hook points at a nonexistent path | `CLAUDE_STATE.md` | Cosmetic, low priority |
| 412 MNQ + 400 MES `commands` rows mislabeled `source='geva_extract'` | `trading_dashboard.py` v5.02 release note | Data-quality finding only, galao.db deliberately left untouched |
| Fetcher2026's `paths.db` in `trader/config.yaml` points at a dead, empty pre-split `galao.db` copy instead of CC2026's live one | `FETCHER2026.md` (Galgo2027 briefing) | Breaks Fetcher2026's priority-queue feature silently; not fixed |

---

## 4. Coordination protocol for running multiple sessions concurrently

- **One session per repo, by default.** The 3-way split exists specifically so this works
  without constant cross-talk. Only break this (have one session touch another project's
  files) for a good reason, and say so explicitly in that project's own state doc when you
  do — CC2026's `CLAUDE_STATE.md` already has a documented instance of this ("That Fetcher
  session has also directly edited files in *this* repo before... cross-repo work does
  happen here, verify with git diff/git status before trusting a handoff summary at face
  value").
- **Before any session restarts a shared dependency** (IB Gateway, or any long-running
  fetcher/watchdog another project depends on), check whether other projects have live
  work in flight that a restart would interrupt (e.g. killing Gateway mid-fetch loses
  Fetcher2026's chunk progress until its own resume logic kicks back in — usually fine,
  but not free).
- **Before force-killing any process by name/pattern** (e.g. "kill all python.exe"),
  enumerate full command lines first (`Get-CimInstance Win32_Process | Select
  CommandLine`) — this ecosystem runs many `python.exe`/`node.exe` processes
  simultaneously across all three projects plus IB Gateway's own `java.exe`, and a blind
  kill-by-name will take down things you didn't intend to touch.
- **Git remotes are per-project and intentionally separate** — CC2026 pushes to `cc2026`
  (not `origin`, which is a stale abandoned remote), Fetcher2026 and GevaExtract each push
  to their own `origin`. Never assume a `git push` from one repo affects another.
- **Version numbers are per-project strings in each dashboard's own HTML, not a shared
  scheme.** They can and do drift silently between sessions (CC2026 went from v4.26 to
  v5.03 across 8 commits without its own `CLAUDE_STATE.md` being updated — see that file's
  2026-08-18 entry). Never trust a doc's claimed "current version" without checking `git
  log --oneline -5` in that repo directly first.

---

## 5. Known cross-project path/DB entanglements (fix candidates for a real consolidation)

These are the specific seams that make "operate in parallel" harder than it should be —
worth fixing centrally rather than working around repeatedly:

1. Fetcher2026's live tick-CSV output and `fetch_progress.db` both still land under
   `C:\Projects\Galgo2026\june\...` — a path outside all three current repos, a holdover
   from before the split. If `Galgo2026` is ever archived/deleted, this breaks silently.
2. Fetcher2026's `trader/config.yaml` → `paths.db` points at a dead, empty pre-split copy
   of `galao.db` instead of CC2026's live one (see table in §3).
3. Two independent, non-shared `lib/config_loader.py` implementations (CC2026's,
   Fetcher2026's) with the same footgun (global cache-on-first-call, nearest-file
   resolution) — worth unifying into one shared module if these projects ever merge.
4. `geva.db` (GevaExtract) has no git history at all — a machine loss would lose 2+ months
   of scraped Facebook line history with no recovery path except re-scraping (which itself
   depends on Facebook not having changed its DOM/detection in the meantime).

A prior effort to consolidate all of this into a single successor project ("Galgo2027",
`C:\Projects\Galgo2027\`) was started 2026-07-21/22 but is incomplete and partially stale —
see `CRITICALCORALLATIONS2026.md` and `FETCHER2026.md` in that folder (both need a refresh
given the v5.03 nav rebuild) and note `GEVAEXTRACT.md` there was never finished. If you're
picking that effort back up, start by re-verifying each doc against current `git log`
output in its respective repo rather than trusting the existing text, per the lesson in
§4's last bullet.

---

## 6. Other algo projects on this machine (lighter touch — not part of the live trading loop)

- **`C:\Projects\Backtrader2026`** — separate backtesting system, own Claude session. Not
  investigated as part of this doc; check its own docs if you need to coordinate with it.
- **`C:\Projects\Galgo2026`** — the pre-split original monolith. Mostly superseded, but
  still load-bearing in one specific way: Fetcher2026's live data currently lands under
  its `june\` subtree (see §5.1). Don't delete/archive this until that path debt is paid
  off and verified.
- **`CorrellationAnalyzer`** — referenced by `Fetcher2026\BARS1S_STATUS.md` §0h as a
  consumer of 1-second bar data for its own signal-research plan
  (`CORRELATION_TRADING_PLAN.md`). Not otherwise covered by this doc — treat as an external
  consumer of Fetcher2026's output, not a project this ecosystem needs to actively manage.

---

## 7. Permissions

All three projects run with `"defaultMode": "bypassPermissions"` — no confirmation prompts
within a session. This orchestration doc doesn't change that, but a coordinating process
spanning multiple sessions should still apply its own judgment about irreversible
cross-project actions (killing a shared Gateway process, force-pushing, deleting a legacy
path another project secretly depends on) the same way any single session would — "allow
all" is about not prompting for routine tool calls, not a license to skip the underlying
judgment about blast radius.
