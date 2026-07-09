# Phase 43 — fix(app_probe): screenshot Tauri apps via their devUrl

## Root cause

`logs/api-20260630-212838.log` showed the `visual: a native desktop
application window is open...` criterion failing every round with
`screenshot: false`, oscillating fix_num 1→3 with no forward progress.

`AppProbe.detect_start_command()` picked `"npm start"` for the workspace
(a Tauri project — `src-tauri/` present) since `package.json` exists.
`AppProbe.launch()` then calls `detect_port()`, which found no port (Tauri
has no HTTP server of its own — the native webview just renders a URL), so
`handle.port` was `None`. `AppProbe.screenshot()` returns `None` immediately
whenever `handle.port is None`, so the acceptance tester always evaluated
the visual criterion with zero evidence and the LLM returned blind failures.

## Fix

`agent/orchestration/app_probe.py`:
- `detect_start_command()` now returns `"npm run tauri dev"` when
  `src-tauri/` exists and `package.json` is present — the actual command
  needed to build+launch the app, not the generic (and here, wrong)
  `"npm start"`.
- Added `_read_tauri_dev_port()`, which reads `src-tauri/tauri.conf.json`'s
  `build.devUrl` (Tauri v2) / `build.devPath` (v1) and extracts the port.
  In dev mode, Tauri's native webview literally renders that URL — pointing
  the existing Playwright-based `screenshot()` at it captures the real
  rendered UI, with no new capture mechanism needed.
- `detect_port()` checks the Tauri dev URL first, before the generic
  port-hint heuristics.
- `launch()` adds a 3 s settle delay after the dev URL responds, only for
  Tauri projects, before returning the handle.

## Known limitation (not fully closed by this fix)

Port-ready only means vite is serving the frontend — it does not confirm
the Rust binary has finished compiling or that the native window actually
opened. A screenshot could now come back `screenshot: true` while the
window itself crashed on launch. The separate `command exits 0: cargo
check` criterion covers the compile side, and the 3 s settle delay gives
the (usually fast, incremental) cargo build a head start, but this is a
mitigation, not a guarantee. If this surfaces as a new false-positive
oscillation, the next step would be polling for the actual OS-level window
(process-tree + `MainWindowHandle`) before trusting the screenshot.

## Verification

- Added `TestDetectStartCommand`/`TestDetectPort` cases in
  `tests/unit/test_app_probe.py` for: Tauri preferred over npm start,
  src-tauri without package.json falls back correctly, v2 `devUrl` parsing,
  v1 `devPath` parsing, dev URL taking priority over vite port hints, and
  malformed `tauri.conf.json` falling back to `None` rather than raising.
  19/19 pass.
- `pytest tests/unit tests/integration` — FAILED-line diff against the
  pre-change baseline is identical; no new regressions.
