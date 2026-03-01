"""
GitHub Copilot Status Watcher for VS Code
==========================================
Monitors Copilot activity in VS Code and exposes three states:

    GENERATING  – Copilot is actively producing a response
    AWAITING    – A response just finished; waiting for user action
    IDLE        – Copilot is ready, nothing happening

Detection is multi-layered:
  1. Log monitoring  – Tails the Copilot Chat log for request lifecycle events
  2. Process monitor – Tracks CPU of the Copilot extension-host process
  3. Lock file       – Discovers active Copilot instances automatically

Output:
  - Philips Hue bulb color control (auto-discovery + pairing)
  - WS2812B LED light-tower stub (ready for future integration)

Requirements:
    pip install psutil phue

Usage:
    python main.py                                  # console only
    python main.py --hue                            # + Hue bulb
    python main.py --hue --hue-bulb "Living Room"   # specific bulb
    python main.py --json                           # JSON lines
    python main.py --status-file status.json        # write to file

First-time Hue setup:
    Run with --hue, then press the button on the Hue bridge when prompted.
    Credentials are saved to ~/.copilot_hue.conf for future runs.

Platform support:
    Windows  – %APPDATA%\Code\logs
    macOS    – ~/Library/Application Support/Code/logs
    Linux    – ~/.config/Code/logs  (or $XDG_CONFIG_HOME/Code/logs)
"""

from __future__ import annotations

import argparse
import enum
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

try:
    import psutil
except ImportError:
    print("psutil is required. Install with:  pip install psutil")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# How often (seconds) to poll process CPU / check for new log lines
POLL_INTERVAL: float = 0.5

# After a request completes, stay in AWAITING for this many seconds
# before transitioning to IDLE (gives user time to review/accept).
AWAITING_TIMEOUT: float = 45.0

# CPU % threshold (combined across tracked processes) – above this we
# consider Copilot active.  Set higher to avoid false positives from
# normal VS Code background activity.
CPU_THRESHOLD: float = 15.0

# VS Code log directory – resolved per platform at startup
def _get_vscode_logs_dir() -> Path:
    """Return the VS Code logs directory for the current platform."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        return Path(appdata) / "Code" / "logs"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "logs"
    else:  # Linux / other Unix
        xdg_config = os.environ.get("XDG_CONFIG_HOME", "")
        base = Path(xdg_config) if xdg_config else Path.home() / ".config"
        return base / "Code" / "logs"


VSCODE_LOGS_DIR = _get_vscode_logs_dir()
COPILOT_LOCK_DIR = Path.home() / ".copilot" / "ide"

# Copilot Chat log filename (constant across sessions)
COPILOT_CHAT_LOG_NAME = "GitHub Copilot Chat.log"

# Hue config file (stores bridge IP + username after first pairing)
HUE_CONFIG_PATH = Path.home() / ".copilot_hue.conf"

# Default bulb name to control (override with --hue-bulb)
DEFAULT_HUE_BULB = "My Hue Light"

# Hue transition time in deciseconds (1 = 100ms, 5 = 500ms)
HUE_TRANSITION_TIME = 5

# Hue brightness override (None = auto from RGB, 1-100 = fixed percentage)
HUE_BRIGHTNESS_PCT: Optional[int] = 100

# ---------------------------------------------------------------------------
# State definitions
# ---------------------------------------------------------------------------

class CopilotState(enum.Enum):
    """Possible Copilot activity states."""
    GENERATING = "GENERATING"   # Actively producing a response
    AWAITING   = "AWAITING"     # Response done, waiting for user
    IDLE       = "IDLE"         # Nothing happening, ready
    OFFLINE    = "OFFLINE"      # VS Code or Copilot not running


# Colors mapped to states – ready for WS2812B integration
STATE_COLORS: dict[CopilotState, tuple[int, int, int]] = {
    CopilotState.GENERATING: (0, 128, 255),    # Blue  – thinking
    CopilotState.AWAITING:   (128, 0, 255),   # Orange – your turn
    CopilotState.IDLE:       (0, 90, 0),      # Green  – ready
    CopilotState.OFFLINE:    (255, 0, 0),      # Red    – offline
}


@dataclass
class StatusSnapshot:
    """Immutable snapshot of the current status."""
    state: CopilotState
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    detail: str = ""
    color_rgb: tuple[int, int, int] = (0, 0, 0)

    def __post_init__(self):
        self.color_rgb = STATE_COLORS[self.state]

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "timestamp": self.timestamp,
            "detail": self.detail,
            "color_rgb": list(self.color_rgb),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


# ---------------------------------------------------------------------------
# Lock-file discovery  (find active Copilot instances)
# ---------------------------------------------------------------------------

def discover_copilot_pids() -> list[int]:
    """Read .copilot/ide/*.lock files and return PIDs of running instances."""
    pids: list[int] = []
    if not COPILOT_LOCK_DIR.is_dir():
        return pids
    for lock_file in COPILOT_LOCK_DIR.glob("*.lock"):
        try:
            data = json.loads(lock_file.read_text(encoding="utf-8"))
            pid = data.get("pid")
            if pid and psutil.pid_exists(pid):
                pids.append(pid)
        except (json.JSONDecodeError, OSError):
            continue
    return pids


# ---------------------------------------------------------------------------
# Log-file discovery  (find the active Copilot Chat log)
# ---------------------------------------------------------------------------

def find_latest_copilot_log() -> Optional[Path]:
    """Return the path to the newest non-empty Copilot Chat log file."""
    if not VSCODE_LOGS_DIR.is_dir():
        return None

    candidates: list[Path] = []
    for session_dir in VSCODE_LOGS_DIR.iterdir():
        if not session_dir.is_dir():
            continue
        for log_file in session_dir.rglob(COPILOT_CHAT_LOG_NAME):
            if log_file.stat().st_size > 0:
                candidates.append(log_file)

    if not candidates:
        return None

    # Pick the most recently modified
    return max(candidates, key=lambda p: p.stat().st_mtime)


# ---------------------------------------------------------------------------
# Log-line parser
# ---------------------------------------------------------------------------

# Patterns observed in Copilot Chat logs:
#
#   Session init:   "ccreq:XXXXXXXX.copilotmd | markdown"
#
#   Sub-request done (one per tool call / LLM round-trip):
#       "ccreq:XXXXXXXX.copilotmd | success | model | Xms | [panel/editAgent]"
#       "ccreq:XXXXXXXX.copilotmd | success | model | Xms | [searchSubagentTool]"
#
#   Auxiliary (title gen, progress, commit msg – NOT main generation):
#       "ccreq:XXXXXXXX.copilotmd | success | gpt-4o-mini ... | [title]"
#       "ccreq:XXXXXXXX.copilotmd | success | gpt-4o-mini ... | [progressMessages]"
#       "ccreq:XXXXXXXX.copilotmd | success | gpt-4o-mini ... | [copilotLanguageModelWrapper]"
#       "ccreq:XXXXXXXX.copilotmd | success | gpt-4o-mini ... | [gitCommitMessageGenerator]"
#
#   API message returned:
#       "[messagesAPI] message 0 returned. finish reason: [stop]"
#       "message 0 returned. finish reason: [stop]"
#
#   Definitive end of a multi-turn agent session:
#       "[ToolCallingLoop] Stop hook result: shouldContinue=false"
#
#   Request done (generic):
#       "request done: requestId: [UUID]"

# Main agent work (panel/editAgent, searchSubagentTool, summarize)
RE_AGENT_SUCCESS = re.compile(
    r"ccreq:[0-9a-f]+\.copilotmd \| success .+\| \[(?:panel/editAgent|searchSubagentTool|summarizeConversationHistory)"
)

# Auxiliary / lightweight requests (should NOT drive state)
RE_AUX_SUCCESS = re.compile(
    r"ccreq:[0-9a-f]+\.copilotmd \| success .+\| \[(?:title|progressMessages|copilotLanguageModelWrapper|gitCommitMessageGenerator)\]"
)

# Session markdown marker (appears once at start of a session)
RE_SESSION_MARKDOWN = re.compile(
    r"ccreq:[0-9a-f]+\.copilotmd \| markdown"
)

RE_MESSAGE_RETURNED = re.compile(
    r"\[messagesAPI\] message \d+ returned\. finish reason:"
)
RE_TOOL_LOOP_STOP = re.compile(
    r"\[ToolCallingLoop\] Stop hook result: shouldContinue=false"
)
RE_REQUEST_DONE = re.compile(
    r"request done: requestId:"
)
RE_TIMESTAMP = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)"
)


class LogEvent(enum.Enum):
    SESSION_START    = "SESSION_START"      # markdown session init
    AGENT_SUCCESS    = "AGENT_SUCCESS"      # main agent sub-request done
    AUX_SUCCESS      = "AUX_SUCCESS"        # auxiliary lightweight request
    MESSAGE_RETURNED = "MESSAGE_RETURNED"   # API message returned
    TOOL_LOOP_STOP   = "TOOL_LOOP_STOP"     # definitive end of agent turn
    REQUEST_DONE     = "REQUEST_DONE"       # generic request complete


def classify_log_line(line: str) -> Optional[LogEvent]:
    """Classify a log line into a lifecycle event, or None."""
    if RE_SESSION_MARKDOWN.search(line):
        return LogEvent.SESSION_START
    if RE_AGENT_SUCCESS.search(line):
        return LogEvent.AGENT_SUCCESS
    if RE_AUX_SUCCESS.search(line):
        return LogEvent.AUX_SUCCESS
    if RE_TOOL_LOOP_STOP.search(line):
        return LogEvent.TOOL_LOOP_STOP
    if RE_MESSAGE_RETURNED.search(line):
        return LogEvent.MESSAGE_RETURNED
    if RE_REQUEST_DONE.search(line):
        return LogEvent.REQUEST_DONE
    return None


def parse_timestamp(line: str) -> Optional[datetime]:
    """Extract timestamp from a log line."""
    m = RE_TIMESTAMP.match(line)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# Process monitor
# ---------------------------------------------------------------------------

class ProcessMonitor:
    """Monitors CPU usage of Copilot-related VS Code processes."""

    def __init__(self):
        self._processes: dict[int, psutil.Process] = {}
        self._last_refresh: float = 0
        self._refresh_interval: float = 10.0  # re-discover every 10s

    def refresh_processes(self) -> None:
        """(Re-)discover Copilot extension-host processes."""
        now = time.monotonic()
        if now - self._last_refresh < self._refresh_interval and self._processes:
            # Remove dead processes
            dead = [pid for pid, p in self._processes.items() if not p.is_running()]
            for pid in dead:
                del self._processes[pid]
            return

        self._last_refresh = now
        new_procs: dict[int, psutil.Process] = {}

        # Method 1: PIDs from lock files
        for pid in discover_copilot_pids():
            if pid not in new_procs:
                try:
                    new_procs[pid] = psutil.Process(pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

        # Method 2: Scan Code.exe processes for copilot-related ones
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = proc.info["name"] or ""
                if "code" not in name.lower():
                    continue
                cmdline = " ".join(proc.info["cmdline"] or []).lower()
                if any(kw in cmdline for kw in [
                    "github.copilot",
                    "copilot-typescript",
                    "copilot\\sdk",
                    "copilot/sdk",
                ]):
                    new_procs[proc.pid] = proc
            except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
                continue

        # Method 3: Extension host utility processes (children of main Code.exe)
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = proc.info["name"] or ""
                if "code" not in name.lower():
                    continue
                cmdline = " ".join(proc.info["cmdline"] or []).lower()
                if "extensionhost" in cmdline or "extension-host" in cmdline:
                    new_procs[proc.pid] = proc
            except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
                continue

        self._processes = new_procs

    def get_total_cpu(self) -> float:
        """Get combined CPU% of all tracked processes."""
        self.refresh_processes()
        total_cpu = 0.0
        dead_pids = []
        for pid, proc in self._processes.items():
            try:
                cpu = proc.cpu_percent(interval=0)
                total_cpu += cpu
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                dead_pids.append(pid)
        for pid in dead_pids:
            del self._processes[pid]
        return total_cpu

    def is_copilot_running(self) -> bool:
        """Return True if any Copilot process is found."""
        self.refresh_processes()
        return len(self._processes) > 0

    @property
    def tracked_pids(self) -> list[int]:
        return list(self._processes.keys())


# ---------------------------------------------------------------------------
# Log tailer
# ---------------------------------------------------------------------------

class LogTailer:
    """Tails the Copilot Chat log file for real-time events."""

    def __init__(self):
        self._path: Optional[Path] = None
        self._file = None
        self._position: int = 0
        self._last_check: float = 0
        self._check_interval: float = 30.0  # re-check log path every 30s

    def _ensure_log(self) -> bool:
        """Find / re-find the log file if needed."""
        now = time.monotonic()
        if self._path and self._path.exists() and (now - self._last_check < self._check_interval):
            return True

        self._last_check = now
        new_path = find_latest_copilot_log()

        if new_path is None:
            return False

        if new_path != self._path:
            # New log file – close old one and open new
            if self._file:
                try:
                    self._file.close()
                except OSError:
                    pass
            self._path = new_path
            try:
                self._file = open(self._path, "r", encoding="utf-8", errors="replace")
                # Seek to end so we only process new lines
                self._file.seek(0, 2)
                self._position = self._file.tell()
            except OSError:
                self._path = None
                return False

        return True

    def read_new_lines(self) -> list[str]:
        """Return any new lines appended since last read."""
        if not self._ensure_log():
            return []

        lines: list[str] = []
        try:
            # Check if file was truncated / rotated
            current_size = self._path.stat().st_size
            if current_size < self._position:
                self._file.seek(0)
                self._position = 0

            self._file.seek(self._position)
            raw = self._file.read()
            if raw:
                lines = raw.splitlines()
                self._position = self._file.tell()
        except OSError:
            pass

        return lines

    def close(self):
        if self._file:
            try:
                self._file.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class CopilotStateMachine:
    """
    Combines log events + CPU data to determine Copilot's state.

    Key insight: during an agent turn, Copilot fires multiple rapid
    'success | [panel/editAgent]' events (one per tool call). The turn
    ends definitively with '[ToolCallingLoop] Stop'.

    Transitions:
        IDLE  →  GENERATING    (agent success event = work in progress,
                                 or CPU spike without log events)
        GENERATING → AWAITING  (ToolCallingLoop stop = turn finished)
        GENERATING → AWAITING  (no new agent events for a gap period)
        AWAITING → IDLE        (awaiting-timeout with no new events)
        AWAITING → GENERATING  (new agent success event arrives)
        any → OFFLINE          (no Copilot process found)
    """

    # Seconds of silence between agent sub-requests before assuming done
    INTRA_TURN_GAP: float = 12.0

    def __init__(self):
        self._state = CopilotState.IDLE
        self._detail = ""
        self._last_agent_event: float = 0          # last panel/editAgent hit
        self._last_any_event: float = 0             # last event of any kind
        self._generating_since: Optional[float] = None
        self._awaiting_since: Optional[float] = None
        self._on_change: list[Callable[[StatusSnapshot], None]] = []

    @property
    def state(self) -> CopilotState:
        return self._state

    def on_state_change(self, callback: Callable[[StatusSnapshot], None]):
        """Register a callback for state changes (LED driver hook)."""
        self._on_change.append(callback)

    def _set_state(self, new_state: CopilotState, detail: str = ""):
        if new_state != self._state or detail != self._detail:
            self._state = new_state
            self._detail = detail
            snap = StatusSnapshot(state=new_state, detail=detail)
            for cb in self._on_change:
                try:
                    cb(snap)
                except Exception:
                    pass

    def process_log_event(self, event: LogEvent):
        """Handle a log lifecycle event."""
        now = time.monotonic()
        self._last_any_event = now

        if event == LogEvent.AGENT_SUCCESS:
            # A main agent sub-request completed → still generating
            # (more tool calls likely to follow)
            self._last_agent_event = now
            self._generating_since = self._generating_since or now
            self._awaiting_since = None
            self._set_state(CopilotState.GENERATING, "Agent active")

        elif event == LogEvent.TOOL_LOOP_STOP:
            # Definitive end of the agent's multi-turn session
            self._generating_since = None
            self._awaiting_since = now
            self._set_state(CopilotState.AWAITING, "Turn complete")

        elif event == LogEvent.MESSAGE_RETURNED:
            # An API message came back – part of ongoing work
            if self._state == CopilotState.IDLE:
                self._generating_since = now
                self._set_state(CopilotState.GENERATING, "API response")

        elif event == LogEvent.SESSION_START:
            # New session started
            self._generating_since = now
            self._awaiting_since = None
            self._set_state(CopilotState.GENERATING, "New session")

        # AUX_SUCCESS and REQUEST_DONE are deliberately ignored for
        # state transitions – they are lightweight background requests
        # (title generation, progress messages, commit messages).

    def process_cpu(self, cpu_pct: float, copilot_alive: bool):
        """Incorporate CPU reading into state decisions."""
        now = time.monotonic()

        if not copilot_alive:
            self._set_state(CopilotState.OFFLINE, "No Copilot process found")
            self._generating_since = None
            return

        # CPU can nudge us into GENERATING if logs haven't caught it
        if self._state == CopilotState.IDLE and cpu_pct > CPU_THRESHOLD:
            self._generating_since = now
            self._last_any_event = now
            self._set_state(CopilotState.GENERATING, f"CPU spike ({cpu_pct:.0f}%)")

        # If GENERATING came only from CPU (no log events supporting it)
        # and CPU dropped back down, transition to awaiting
        if self._state == CopilotState.GENERATING and cpu_pct < CPU_THRESHOLD:
            if self._generating_since and (now - self._last_any_event > 5.0):
                self._awaiting_since = now
                self._generating_since = None
                self._set_state(CopilotState.AWAITING, "Activity subsided")

    def tick(self):
        """Called every poll cycle to handle time-based transitions."""
        now = time.monotonic()

        # GENERATING → AWAITING if no new agent events for INTRA_TURN_GAP
        # (handles cases where ToolCallingLoop stop is not logged)
        if self._state == CopilotState.GENERATING and self._last_agent_event:
            if now - self._last_agent_event > self.INTRA_TURN_GAP:
                self._generating_since = None
                self._awaiting_since = now
                self._set_state(CopilotState.AWAITING, "Gap timeout")

        # AWAITING → IDLE after awaiting-timeout
        if self._state == CopilotState.AWAITING and self._awaiting_since:
            if now - self._awaiting_since > AWAITING_TIMEOUT:
                self._set_state(CopilotState.IDLE, "Ready")

        # Safety: if stuck in GENERATING for too long without any events
        if self._state == CopilotState.GENERATING and self._generating_since:
            if now - self._last_any_event > 120.0:
                self._generating_since = None
                self._set_state(CopilotState.IDLE, "Stale timeout")

    def snapshot(self) -> StatusSnapshot:
        return StatusSnapshot(state=self._state, detail=self._detail)


# ---------------------------------------------------------------------------
# Console display
# ---------------------------------------------------------------------------

STATE_ICONS = {
    CopilotState.GENERATING: ">>>",
    CopilotState.AWAITING:   "...",
    CopilotState.IDLE:       "[+]",
    CopilotState.OFFLINE:    "[X]",
}

STATE_ANSI_COLORS = {
    CopilotState.GENERATING: "\033[94m",   # bright blue
    CopilotState.AWAITING:   "\033[93m",   # bright yellow
    CopilotState.IDLE:       "\033[92m",   # bright green
    CopilotState.OFFLINE:    "\033[91m",   # bright red
}

RESET = "\033[0m"


def print_status(snap: StatusSnapshot, json_mode: bool = False):
    """Pretty-print or JSON-print the current status."""
    if json_mode:
        print(snap.to_json(), flush=True)
    else:
        icon = STATE_ICONS.get(snap.state, "?")
        color = STATE_ANSI_COLORS.get(snap.state, "")
        r, g, b = snap.color_rgb
        detail = f"  ({snap.detail})" if snap.detail else ""
        line = (
            f"{color}{icon}  {snap.state.value:<12}{RESET}"
            f"  RGB({r:3d},{g:3d},{b:3d}){detail}"
        )
        # Overwrite same line for a cleaner look
        print(f"\r{line}    ", end="", flush=True)


# ---------------------------------------------------------------------------
# Status-file writer
# ---------------------------------------------------------------------------

def write_status_file(path: Path, snap: StatusSnapshot):
    """Atomically write status to a JSON file (safe for external readers)."""
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(snap.to_json(), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def create_banner() -> str:
    return (
        "\n"
        "+------------------------------------------------------+\n"
        "|        GitHub Copilot Status Watcher  v1.0           |\n"
        "|                                                      |\n"
        "|   >>> GENERATING  = Copilot is thinking   (Blue)     |\n"
        "|   ... AWAITING    = Your turn              (Orange)   |\n"
        "|   [+] IDLE        = Ready & waiting        (Green)    |\n"
        "|   [X] OFFLINE     = Copilot not running    (Red)      |\n"
        "|                                                      |\n"
        "|   Press Ctrl+C to stop.                              |\n"
        "+------------------------------------------------------+\n"
    )


def _update_awaiting_timeout(value: float):
    global AWAITING_TIMEOUT
    AWAITING_TIMEOUT = value


def main():
    parser = argparse.ArgumentParser(description="Monitor GitHub Copilot status in VS Code")
    parser.add_argument(
        "--json", action="store_true",
        help="Output status as JSON lines (one per state change)"
    )
    parser.add_argument(
        "--status-file", type=str, default=None,
        help="Path to write current status as JSON (for external consumers)"
    )
    parser.add_argument(
        "--poll-interval", type=float, default=POLL_INTERVAL,
        help=f"Polling interval in seconds (default: {POLL_INTERVAL})"
    )
    parser.add_argument(
        "--awaiting-timeout", type=float, default=AWAITING_TIMEOUT,
        help=f"Seconds to stay in AWAITING before IDLE (default: {AWAITING_TIMEOUT})"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging"
    )

    # Hue options
    hue_group = parser.add_argument_group("Philips Hue")
    hue_group.add_argument(
        "--hue", action="store_true",
        help="Enable Philips Hue bulb control"
    )
    hue_group.add_argument(
        "--hue-bulb", type=str, default=DEFAULT_HUE_BULB,
        help=f"Name of the Hue bulb to control (default: {DEFAULT_HUE_BULB})"
    )
    hue_group.add_argument(
        "--hue-transition", type=int, default=HUE_TRANSITION_TIME,
        help=f"Transition time in deciseconds (default: {HUE_TRANSITION_TIME}, i.e. {HUE_TRANSITION_TIME * 100}ms)"
    )
    hue_group.add_argument(
        "--hue-off-on-exit", action="store_true",
        help="Turn the Hue bulb off when the watcher stops"
    )
    hue_group.add_argument(
        "--hue-brightness", type=int, default=None, metavar="PCT",
        help="Fixed brightness percentage (1-100). Default: auto from color"
    )
    hue_group.add_argument(
        "--hue-bridge-ip", type=str, default=None,
        help="Hue bridge IP address (auto-discovered if not set)"
    )

    args = parser.parse_args()

    # Reconfigure globals from args
    _update_awaiting_timeout(args.awaiting_timeout)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    log = logging.getLogger("copilot_watcher")

    if not args.json:
        print(create_banner())

    # --- Initialize components ---
    state_machine = CopilotStateMachine()
    process_monitor = ProcessMonitor()
    log_tailer = LogTailer()
    hue: Optional[HueController] = None

    status_file_path = Path(args.status_file) if args.status_file else None
    json_mode = args.json

    # --- Philips Hue setup ---
    if args.hue:
        hue = HueController(
            bulb_name=args.hue_bulb,
            transition=args.hue_transition,
            bridge_ip=args.hue_bridge_ip,
            brightness_pct=args.hue_brightness,
        )
        if not hue.connect():
            sys.exit(1)
        state_machine.on_state_change(hue.update)
        if not json_mode:
            print()  # blank line after Hue info

    # Callback: print every state change
    def on_change(snap: StatusSnapshot):
        print_status(snap, json_mode=json_mode)
        if status_file_path:
            write_status_file(status_file_path, snap)

    state_machine.on_state_change(on_change)

    # Initial CPU measurement (psutil needs a first call to calibrate)
    process_monitor.get_total_cpu()
    time.sleep(0.1)

    # Print initial state
    snap = state_machine.snapshot()
    print_status(snap, json_mode=json_mode)
    if status_file_path:
        write_status_file(status_file_path, snap)

    log.info("Watcher started. Polling every %.1fs", args.poll_interval)
    log.info("Lock dir: %s", COPILOT_LOCK_DIR)
    log.info("Logs dir: %s", VSCODE_LOGS_DIR)

    active_log = find_latest_copilot_log()
    if active_log:
        log.info("Tailing log: %s", active_log)
    else:
        log.warning("No Copilot Chat log found yet – relying on process monitoring")

    try:
        while True:
            # 1. Read new log lines
            new_lines = log_tailer.read_new_lines()
            for line in new_lines:
                event = classify_log_line(line)
                if event:
                    log.debug("Log event: %s | %s", event.name, line.strip()[:100])
                    state_machine.process_log_event(event)

            # 2. Check process CPU
            copilot_alive = process_monitor.is_copilot_running()
            cpu = process_monitor.get_total_cpu()
            log.debug("CPU: %.1f%% | PIDs: %s | Alive: %s",
                       cpu, process_monitor.tracked_pids, copilot_alive)
            state_machine.process_cpu(cpu, copilot_alive)

            # 3. Time-based transitions
            state_machine.tick()

            time.sleep(args.poll_interval)

    except KeyboardInterrupt:
        if not json_mode:
            print(f"\n\n{STATE_ANSI_COLORS[CopilotState.OFFLINE]}Watcher stopped.{RESET}")
    finally:
        log_tailer.close()
        if hue and args.hue_off_on_exit:
            hue.turn_off()
        if status_file_path and status_file_path.exists():
            snap = StatusSnapshot(state=CopilotState.OFFLINE, detail="Watcher stopped")
            write_status_file(status_file_path, snap)


# ---------------------------------------------------------------------------
# RGB → CIE xy conversion for Philips Hue
# ---------------------------------------------------------------------------

def rgb_to_xy(r: int, g: int, b: int) -> tuple[float, float]:
    """
    Convert RGB (0-255) to CIE 1931 xy chromaticity for Philips Hue.

    Uses the wide-gamut D65 matrix recommended by Philips.
    """
    # Normalize and apply gamma correction
    def gamma(v: float) -> float:
        v = v / 255.0
        return pow((v + 0.055) / 1.055, 2.4) if v > 0.04045 else v / 12.92

    red = gamma(r)
    green = gamma(g)
    blue = gamma(b)

    # Wide-gamut D65 conversion
    X = red * 0.664511 + green * 0.154324 + blue * 0.162028
    Y = red * 0.283881 + green * 0.668433 + blue * 0.047685
    Z = red * 0.000088 + green * 0.072310 + blue * 0.986039

    total = X + Y + Z
    if total == 0:
        return (0.3127, 0.3290)  # D65 white point

    x = X / total
    y = Y / total
    return (round(x, 4), round(y, 4))


def rgb_to_brightness(r: int, g: int, b: int) -> int:
    """Convert RGB to Hue brightness (1-254). Uses perceived luminance."""
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    bri = int((luminance / 255.0) * 253) + 1
    return max(1, min(254, bri))


# ---------------------------------------------------------------------------
# Philips Hue Controller
# ---------------------------------------------------------------------------

class HueController:
    """
    Controls a Philips Hue bulb based on Copilot state.

    Handles:
      - Auto-discovery of the Hue bridge on the local network
      - First-time pairing (press the bridge button when prompted)
      - Persistent config storage (~/.copilot_hue.conf)
      - Color + brightness changes with smooth transitions
    """

    def __init__(self, bulb_name: str = DEFAULT_HUE_BULB,
                 config_path: Path = HUE_CONFIG_PATH,
                 transition: int = HUE_TRANSITION_TIME,
                 bridge_ip: Optional[str] = None,
                 brightness_pct: Optional[int] = None):
        self._bulb_name = bulb_name
        self._config_path = config_path
        self._transition = transition
        self._bridge_ip = bridge_ip
        # Fixed brightness: 1-100% → Hue 1-254, or None for auto
        if brightness_pct is not None:
            brightness_pct = max(1, min(100, brightness_pct))
        self._brightness_pct = brightness_pct
        self._bridge = None
        self._light_id: Optional[int] = None
        self._last_state: Optional[CopilotState] = None
        self._log = logging.getLogger("hue")

    @staticmethod
    def discover_bridge_ip() -> Optional[str]:
        """
        Discover the Hue bridge IP on the local network.

        Uses discovery.meethue.com and filters to bridges
        reachable from this machine.
        """
        import http.client
        import socket

        print("  Discovering Hue bridge on network...")

        try:
            conn = http.client.HTTPSConnection("discovery.meethue.com", timeout=10)
            conn.request("GET", "/")
            resp = conn.getresponse()
            bridges = json.loads(resp.read().decode())
            conn.close()
        except Exception as e:
            print(f"  Discovery API failed: {e}")
            return None

        if not bridges:
            print("  No bridges found via discovery API.")
            return None

        # Find bridges reachable on the local network
        for b in bridges:
            ip = b.get("internalipaddress")
            if not ip:
                continue
            try:
                s = socket.create_connection((ip, 443), timeout=2)
                s.close()
                print(f"  Found bridge at {ip}")
                return ip
            except (OSError, socket.timeout):
                continue

        print("  No reachable bridge found on local network.")
        return None

    def connect(self) -> bool:
        """
        Connect to the Hue bridge and find the target bulb.

        Returns True if successful.  On first run, prompts user
        to press the bridge button for pairing.
        """
        try:
            from phue import Bridge, PhueRegistrationException
        except ImportError:
            print("ERROR: phue is required for Hue support.")
            print("       Install with:  pip install phue")
            return False

        config_str = str(self._config_path)

        # Determine bridge IP
        bridge_ip = self._bridge_ip

        # Check if config already has a stored bridge IP
        if not bridge_ip and self._config_path.exists():
            try:
                import configparser
                cfg = configparser.ConfigParser()
                cfg.read(str(self._config_path))
                # phue stores IP in config file
                stored_ip = None
                for section in cfg.sections():
                    if section not in ("DEFAULT",):
                        stored_ip = section
                        break
                if stored_ip:
                    bridge_ip = stored_ip
                    self._log.info("Using stored bridge IP: %s", bridge_ip)
            except Exception:
                pass

        # Auto-discover if no IP yet
        if not bridge_ip:
            bridge_ip = self.discover_bridge_ip()
            if not bridge_ip:
                print("ERROR: Could not discover Hue bridge.")
                print("       Use --hue-bridge-ip <IP> to specify manually.")
                return False

        # Attempt connection + pairing
        max_attempts = 30
        for attempt in range(1, max_attempts + 1):
            try:
                self._bridge = Bridge(
                    ip=bridge_ip,
                    config_file_path=config_str,
                )
                self._bridge.connect()
                break
            except PhueRegistrationException:
                if attempt == 1:
                    print("\n" + "=" * 55)
                    print("  PHILIPS HUE PAIRING")
                    print("  Press the button on your Hue Bridge now!")
                    print("  Waiting for pairing... (30 seconds)")
                    print("=" * 55)
                time.sleep(1)
                remaining = max_attempts - attempt
                print(f"  Waiting... ({remaining}s remaining)", end="\r")
            except Exception as e:
                self._log.error("Hue bridge connection failed: %s", e)
                print(f"ERROR: Could not connect to Hue bridge at {bridge_ip}: {e}")
                return False
        else:
            print("\nERROR: Pairing timed out. Run again and press the bridge button.")
            return False

        # Find the target bulb
        # phue returns the light id as a string, or False if not found
        try:
            light_id = self._bridge.get_light_id_by_name(self._bulb_name)
            if light_id is False or light_id is None:
                self._light_id = None
            else:
                self._light_id = int(light_id)
        except Exception:
            self._light_id = None

        if self._light_id is None:
            # List available lights to help the user
            try:
                lights = self._bridge.get_light_objects('name')
                available = list(lights.keys())
            except Exception:
                available = []

            print(f"\nERROR: Bulb '{self._bulb_name}' not found on the bridge.")
            if available:
                print("  Available lights:")
                for name in available:
                    print(f"    - {name}")
            print(f"\n  Use --hue-bulb \"<name>\" to specify a different bulb.")
            return False

        bridge_ip = self._bridge.ip
        print(f"  Hue bridge : {bridge_ip}")
        print(f"  Bulb       : {self._bulb_name} (id={self._light_id})")
        print(f"  Config     : {self._config_path}")

        # Ensure the light is on
        self._bridge.set_light(self._light_id, 'on', True)

        return True

    def set_color(self, r: int, g: int, b: int):
        """Set the bulb to an RGB color with a smooth transition."""
        if not self._bridge or self._light_id is None:
            return

        xy = rgb_to_xy(r, g, b)
        if self._brightness_pct is not None:
            bri = max(1, min(254, int(self._brightness_pct / 100.0 * 253) + 1))
        else:
            bri = rgb_to_brightness(r, g, b)

        try:
            self._bridge.set_light(self._light_id, {
                'on': True,
                'xy': list(xy),
                'bri': bri,
                'transitiontime': self._transition,
            })
        except Exception as e:
            self._log.warning("Hue set_light failed: %s", e)

    def turn_off(self):
        """Turn the bulb off (used on exit)."""
        if not self._bridge or self._light_id is None:
            return
        try:
            self._bridge.set_light(self._light_id, 'on', False)
        except Exception:
            pass

    def update(self, snap: StatusSnapshot):
        """
        Callback for state_machine.on_state_change().

        Only sends commands when the state actually changes
        to avoid flooding the bridge with duplicate requests.
        """
        if snap.state == self._last_state:
            return
        self._last_state = snap.state
        r, g, b = snap.color_rgb
        self.set_color(r, g, b)


# ---------------------------------------------------------------------------
# LED Integration stub  (future WS2812B support)
# ---------------------------------------------------------------------------

class LedController:
    """
    Stub for WS2812B LED light-tower control.

    To integrate later:
      1. Subclass this and implement `set_color()`
      2. Register an instance with `state_machine.on_state_change(led.update)`

    Example with rpi_ws281x (Raspberry Pi) or serial bridge:

        from rpi_ws281x import PixelStrip, Color

        class WS2812BController(LedController):
            def __init__(self, pin=18, count=8, brightness=50):
                self.strip = PixelStrip(count, pin, brightness=brightness)
                self.strip.begin()

            def set_color(self, r: int, g: int, b: int):
                color = Color(r, g, b)
                for i in range(self.strip.numPixels()):
                    self.strip.setPixelColor(i, color)
                self.strip.show()
    """

    def set_color(self, r: int, g: int, b: int):
        """Override this to send RGB to your LED hardware."""
        pass

    def update(self, snap: StatusSnapshot):
        """Callback compatible with state_machine.on_state_change()."""
        r, g, b = snap.color_rgb
        self.set_color(r, g, b)


if __name__ == "__main__":
    main()
