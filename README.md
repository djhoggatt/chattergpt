# Chattergpt

Chattergpt is a terminal UI for ChatGPT that uses a playwright-driven browser session to interact with the application.

This is a bad way of doing things, use at your own risk.

## Requirements

Install dependencies:

```bash
python3 -m pip install -e .
```

Optional:

- If you already have Chromium or Chrome installed, Chattergpt will try to use that automatically, preferring your detected local Chromium.
- Set `CHATTERGPT_BROWSER=/path/to/browser` to force a specific browser binary.
- Set `CHATTERGPT_BROWSER_TARGET=Chromium`, `Brave`, or `Chrome` if you want to force one detected browser family without changing the path.
- Set `CHATTERGPT_BROWSER_PROFILE_DIR=/path/to/profile` to force the browser profile location.
- Set `CHATTERGPT_AUTO_LAUNCH_BROWSER=0` if you want attach mode to only connect and never start the browser for you.

Run:

```bash
chattergpt
```

Useful keys:

- `F5` refreshes auth state and conversation sync
- `F6` raises the Playwright-controlled browser window
- `Alt+Up`, `Alt+Down`, and `Alt+Enter` navigate the sidebar

## Notes

- The browser backend is intentionally isolated because ChatGPT frontend changes will require maintenance.
- Chromium defaults to `http://127.0.0.1:9222`, Brave to `http://127.0.0.1:9223`, and Chrome to `http://127.0.0.1:9224`.
