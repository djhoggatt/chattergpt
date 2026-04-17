# Chattergpt

Chattergpt is a terminal UI for ChatGPT that uses a playwright-driven browser session to interact with the application.

This is a bad way of doing things, use at your own risk.

If you don't want to display the browser while using the TUI, then install xvfb, and the program will automatically launch the browser window in the xvfb virtual display. If you already have xvfb installed, then you will need to launch the session at least one time in the visible window in order to authenticate into ChatGPT.

## Dependencies

 - Python
 - xvfb (if you want to hide the browser)
 - chromium (or another compatible browser)

## Requirements

Install dependencies:

```bash
python3 -m pip install -e .
```

Optional:

- If you already have Chromium or Chrome installed, Chattergpt will try to use that automatically, preferring your detected local Chromium.
- Set `CHATTERGPT_BROWSER=/path/to/browser` to force a specific browser binary.
- Set `CHATTERGPT_BROWSER_PROFILE_DIR=/path/to/profile` to force the browser profile location.
- By default, Chattergpt launches a managed browser inside a virtual display if `Xvfb` is available.
- Set `CHATTERGPT_DISPLAY_BROWSER=1` to keep the managed browser visible for login or debugging.
- Set `CHATTERGPT_VIRTUAL_DISPLAY_SIZE=1024x768x24` to adjust the virtual screen size.

Run:

```bash
chattergpt
```

## Controls

- `F5` refreshes auth state and conversation sync
- `F6` raises the controlled browser window when it is visible
- `Alt+Up` and `Alt+Down` move the sidebar selection and open the highlighted chat when you release the keys
- `Alt+Enter` opens the currently highlighted sidebar item immediately
- `Enter` in the message pane sends the current prompt
- `Shift+Enter` in the message pane inserts a newline

## Notes

- The browser backend is intentionally isolated because ChatGPT frontend changes will require maintenance.
- If you run in virtual-display mode and need to authenticate, rerun once with `CHATTERGPT_DISPLAY_BROWSER=1`, log in, then go back to normal mode.
- This code makes heavy use of vibe-coding. Use at your own risk.
