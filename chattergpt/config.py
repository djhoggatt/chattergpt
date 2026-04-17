from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from os import environ
from pathlib import Path
from shutil import which


def _xdg_dir(env_name: str, fallback: str) -> Path:
    return Path(environ.get(env_name, str(Path.home() / fallback))) / "chattergpt"


def _env_flag(name: str, default: bool) -> bool:
    raw = environ.get(name)
    if raw is None:
        return default
    return raw not in {"0", "false", "False", "no", "No"}


def _display_browser_flag() -> bool:
    return _env_flag("CHATTERGPT_DISPLAY_BROWSER", False)


def _detect_browser_executable() -> str | None:
    configured = environ.get("CHATTERGPT_BROWSER")
    if configured:
        return configured
    for candidate in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        path = which(candidate)
        if path:
            return path
    return None


def _detect_virtual_display_executable() -> str | None:
    return which("Xvfb")


def _browser_name(executable_path: str | None) -> str:
    if not executable_path:
        return "Browser"
    name = Path(executable_path).name.lower()
    if "brave" in name:
        return "Brave"
    if "chrome" in name:
        return "Chrome"
    if "chromium" in name:
        return "Chromium"
    return Path(executable_path).name


@dataclass(slots=True)
class BrowserTarget:
    name: str
    executable_path: str
    cdp_url: str
    profile_dir: Path
    launch_args: tuple[str, ...]

    @property
    def launch_command(self) -> str:
        parts = [self.executable_path, *self.launch_args, f"--user-data-dir={self.profile_dir}"]
        return " ".join(parts)


def _default_browser_profile_dir(browser_executable_path: str | None) -> Path:
    override = environ.get("CHATTERGPT_BROWSER_PROFILE_DIR")
    if override:
        return Path(override).expanduser()
    if browser_executable_path and "/snap/bin/" in browser_executable_path:
        snap_name = Path(browser_executable_path).name
        return Path.home() / "snap" / snap_name / "common" / "chattergpt-browser-profile"
    return _xdg_dir("XDG_DATA_HOME", ".local/share") / "browser-profile"


def _build_browser_target(executable_path: str | None, profile_dir: Path | None) -> BrowserTarget | None:
    if executable_path is None or profile_dir is None:
        return None
    return BrowserTarget(
        name=_browser_name(executable_path),
        executable_path=executable_path,
        cdp_url=environ.get("CHATTERGPT_CDP_URL", "http://127.0.0.1:9222"),
        profile_dir=profile_dir,
        launch_args=(
            "--remote-debugging-port=9222",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--new-window",
        ),
    )


@dataclass(slots=True)
class Settings:
    app_name: str = "Chattergpt"
    base_url: str = "https://chatgpt.com/"
    display_browser: bool = _display_browser_flag()
    sync_limit: int = 25
    poll_interval_seconds: float = 0.75
    browser_executable_path: str | None = _detect_browser_executable()
    virtual_display_executable: str | None = _detect_virtual_display_executable()
    virtual_display_size: str = environ.get("CHATTERGPT_VIRTUAL_DISPLAY_SIZE", "1024x768x24")
    virtual_display_number: int = int(environ.get("CHATTERGPT_VIRTUAL_DISPLAY_NUMBER", "99"))
    database_path: Path = _xdg_dir("XDG_DATA_HOME", ".local/share") / "chattergpt.db"
    cache_dir: Path = _xdg_dir("XDG_CACHE_HOME", ".cache")
    backend_log_path: Path | None = None
    browser_profile_dir: Path | None = None
    browser_target: BrowserTarget | None = None

    def __post_init__(self) -> None:
        if self.backend_log_path is None:
            self.backend_log_path = self.cache_dir / "backend.log"
        if self.browser_profile_dir is None:
            self.browser_profile_dir = _default_browser_profile_dir(self.browser_executable_path)
        if self.browser_target is None:
            self.browser_target = _build_browser_target(self.browser_executable_path, self.browser_profile_dir)

    def ensure_directories(self) -> None:
        if self.browser_profile_dir is None:
            raise RuntimeError("browser_profile_dir must be initialized before ensure_directories().")
        self.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def log_timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
