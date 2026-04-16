from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from os import environ
from pathlib import Path
from shutil import which


def _xdg_dir(env_name: str, fallback: str) -> Path:
    return Path(environ.get(env_name, str(Path.home() / fallback))) / "chattergpt"


def _detect_browser_executable() -> str | None:
    configured = environ.get("CHATTERGPT_BROWSER")
    if configured:
        return configured
    for candidate in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        path = which(candidate)
        if path:
            return path
    return None


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


def _profile_dir_for_target(name: str, executable_path: str) -> Path:
    override = environ.get("CHATTERGPT_BROWSER_PROFILE_DIR")
    if override:
        return Path(override).expanduser()
    slug = name.lower().replace(" ", "-")
    if "/snap/bin/" in executable_path:
        snap_name = Path(executable_path).name
        return Path.home() / "snap" / snap_name / "common" / f"chattergpt-{slug}-profile"
    return _xdg_dir("XDG_DATA_HOME", ".local/share") / f"{slug}-browser-profile"


def _detect_browser_targets() -> list[BrowserTarget]:
    explicit_cdp_url = environ.get("CHATTERGPT_CDP_URL")
    explicit_browser = environ.get("CHATTERGPT_BROWSER")
    targets: list[BrowserTarget] = []
    definitions = (
        ("Chromium", ("chromium", "chromium-browser"), 9222),
        ("Brave", ("brave-browser", "brave", "brave-browser-stable"), 9223),
        ("Chrome", ("google-chrome", "google-chrome-stable"), 9224),
    )
    for name, commands, port in definitions:
        path = explicit_browser if explicit_browser and name == "Chromium" else None
        if not path:
            for command in commands:
                path = which(command)
                if path:
                    break
        if not path:
            continue
        cdp_url = explicit_cdp_url if explicit_cdp_url and name == "Chromium" else f"http://127.0.0.1:{port}"
        targets.append(
            BrowserTarget(
                name=name,
                executable_path=path,
                cdp_url=cdp_url,
                profile_dir=_profile_dir_for_target(name, path),
                launch_args=(
                    f"--remote-debugging-port={port}",
                    "--remote-allow-origins=*",
                    "--no-first-run",
                    "--new-window",
                ),
            )
        )
    return targets


def _select_target(targets: list[BrowserTarget]) -> BrowserTarget | None:
    preferred = environ.get("CHATTERGPT_BROWSER_TARGET")
    if preferred:
        for target in targets:
            if target.name.lower() == preferred.lower():
                return target
    return targets[0] if targets else None


def _default_browser_profile_dir(browser_executable_path: str | None) -> Path:
    override = environ.get("CHATTERGPT_BROWSER_PROFILE_DIR")
    if override:
        return Path(override).expanduser()
    if browser_executable_path and "/snap/bin/" in browser_executable_path:
        return Path.home() / "snap" / "chromium" / "common" / "chattergpt-browser-profile"
    return _xdg_dir("XDG_DATA_HOME", ".local/share") / "browser-profile"


@dataclass(slots=True)
class Settings:
    app_name: str = "Chattergpt"
    base_url: str = "https://chatgpt.com/"
    headless: bool = False
    sync_limit: int = 25
    poll_interval_seconds: float = 0.75
    backend_mode: str = environ.get("CHATTERGPT_BACKEND_MODE", "attach")
    auto_launch_browser: bool = environ.get("CHATTERGPT_AUTO_LAUNCH_BROWSER", "1") not in {"0", "false", "False"}
    browser_executable_path: str | None = _detect_browser_executable()
    database_path: Path = _xdg_dir("XDG_DATA_HOME", ".local/share") / "chattergpt.db"
    cache_dir: Path = _xdg_dir("XDG_CACHE_HOME", ".cache")
    backend_log_path: Path | None = None
    browser_profile_dir: Path | None = None
    browser_targets: list[BrowserTarget] | None = None
    selected_browser_name: str | None = environ.get("CHATTERGPT_BROWSER_TARGET")

    def __post_init__(self) -> None:
        if self.backend_log_path is None:
            self.backend_log_path = self.cache_dir / "backend.log"
        if self.browser_profile_dir is None:
            self.browser_profile_dir = _default_browser_profile_dir(self.browser_executable_path)
        if self.browser_targets is None:
            self.browser_targets = _detect_browser_targets()
        if self.selected_browser_name is None:
            selected = _select_target(self.browser_targets)
            self.selected_browser_name = selected.name if selected else None

    def ensure_directories(self) -> None:
        if self.browser_profile_dir is None:
            raise RuntimeError("browser_profile_dir must be initialized before ensure_directories().")
        self.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        for target in self.browser_targets or []:
            target.profile_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def log_timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
