# huepilot

> Control your Philips Hue lights (or any RGB LED) based on GitHub Copilot's activity state in VS Code.

huepilot watches VS Code in real time and drives a physical light to show you — and anyone nearby — exactly what Copilot is doing, without you having to look at the screen.

---

## States

| State | Meaning | Default color |
|---|---|---|
| `GENERATING` | Copilot is actively producing a response | Blue |
| `AWAITING` | Response just finished, waiting for your input | Dim blue |
| `IDLE` | Copilot is ready, nothing happening | Green |
| `OFFLINE` | VS Code or Copilot is not running | Red |

---

## How it works

Detection is multi-layered for reliability:

1. **Log monitoring** — Tails the Copilot Chat log file for request lifecycle events (`ccreq`, `ToolCallingLoop`, `messagesAPI`, etc.)
2. **Process monitor** — Tracks CPU usage of Copilot extension-host processes via `psutil`
3. **Lock-file discovery** — Reads `~/.copilot/ide/*.lock` to find active Copilot instance PIDs automatically

All three signals feed a single state machine that debounces transitions and avoids false positives from background VS Code activity.

---

## Platform support

| Platform | VS Code log path |
|---|---|
| Windows | `%APPDATA%\Code\logs` |
| macOS | `~/Library/Application Support/Code/logs` |
| Linux | `~/.config/Code/logs` (or `$XDG_CONFIG_HOME/Code/logs`) |

---

## Requirements

- Python 3.9 or later
- [psutil](https://pypi.org/project/psutil/) — process and CPU monitoring
- [phue](https://pypi.org/project/phue/) — Philips Hue bridge control *(only needed for `--hue`)*

---

## Installation

```bash
git clone https://github.com/MaprovaDevelopment/huepilot.git
cd huepilot
pip install -r requirements.txt
```

Or install dependencies manually:

```bash
pip install psutil phue
```

---

## Usage

```bash
# Console only (no light control)
python main.py

# Enable Philips Hue control (uses auto-discovery)
python main.py --hue

# Specify which bulb to control
python main.py --hue --hue-bulb "Living Room"

# Specify the bridge IP manually
python main.py --hue --hue-bridge-ip 192.168.1.10

# Output state as JSON lines (one per change, useful for scripting)
python main.py --json

# Write current state to a JSON file for external consumers
python main.py --status-file status.json

# Adjust how long to stay in AWAITING before going IDLE
python main.py --awaiting-timeout 30

# Set a fixed brightness (1–100%)
python main.py --hue --hue-brightness 20

# Turn the bulb off when the watcher stops
python main.py --hue --hue-off-on-exit

# Debug logging
python main.py --verbose
```

### All options

| Flag | Default | Description |
|---|---|---|
| `--hue` | off | Enable Philips Hue bulb control |
| `--hue-bulb NAME` | `My Hue Light` | Name of the bulb to control |
| `--hue-bridge-ip IP` | auto-discovered | Hue bridge IP address |
| `--hue-transition N` | `5` | Transition time in deciseconds (e.g. `5` = 500 ms) |
| `--hue-brightness PCT` | auto | Fixed brightness, 1–100%. Default derives brightness from color |
| `--hue-off-on-exit` | off | Turn the bulb off when the watcher stops |
| `--json` | off | Print state changes as JSON lines |
| `--status-file PATH` | none | Write current state to a JSON file |
| `--poll-interval SECS` | `0.5` | How often to poll logs and CPU |
| `--awaiting-timeout SECS` | `45` | Seconds to stay in AWAITING before returning to IDLE |
| `--verbose` / `-v` | off | Enable debug logging |

---

## First-time Hue setup

1. Make sure your Hue bridge and your PC are on the same network.
2. Run the watcher with `--hue`:
   ```bash
   python main.py --hue
   ```
3. When prompted, **press the button on your Hue bridge**.
4. Pairing completes and credentials are saved to `~/.copilot_hue.conf` — no need to press the button again.

huepilot uses [discovery.meethue.com](https://discovery.meethue.com) to find your bridge automatically. If that fails, pass `--hue-bridge-ip <IP>` instead.

---

## JSON output

With `--json`, each state change prints a single JSON line:

```json
{"state": "GENERATING", "timestamp": "2026-03-01T10:00:00.123456+00:00", "detail": "Agent active", "color_rgb": [0, 80, 255]}
{"state": "AWAITING", "timestamp": "2026-03-01T10:00:15.789012+00:00", "detail": "Turn complete", "color_rgb": [0, 40, 128]}
{"state": "IDLE", "timestamp": "2026-03-01T10:01:00.000000+00:00", "detail": "Ready", "color_rgb": [0, 90, 0]}
```

Combine with `--status-file` to let other tools and dashboards poll the current state from disk.

---

## LED integration stub

The `LedController` class in `main.py` is a ready-to-subclass stub for WS2812B (NeoPixel) or any other RGB LED hardware.

Example for a Raspberry Pi with the `rpi_ws281x` library:

```python
from rpi_ws281x import PixelStrip, Color
from main import LedController, CopilotStateMachine

class WS2812BController(LedController):
    def __init__(self, pin=18, count=8, brightness=50):
        self.strip = PixelStrip(count, pin, brightness=brightness)
        self.strip.begin()

    def set_color(self, r: int, g: int, b: int):
        color = Color(r, g, b)
        for i in range(self.strip.numPixels()):
            self.strip.setPixelColor(i, color)
        self.strip.show()

# Register the LED controller
state_machine = CopilotStateMachine()
state_machine.on_state_change(WS2812BController().update)
```

---

## License

[MIT](LICENSE) — © 2026 Hans Thoft Rasmussen

