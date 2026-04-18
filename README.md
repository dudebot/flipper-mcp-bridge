# flipper-mcp-bridge

MCP server that exposes a USB-connected Flipper Zero as a set of tools for MCP clients (Claude Code, Home Assistant via compatible bridge, etc.). v0 focuses on IR: list/parse saved `.ir` files, replay buttons, capture new signals.

Tested against **Momentum firmware (mntm-008)**. Any recent Flipper fork with the same CLI (`ir tx`, `ir rx`, `storage *`, `loader *`) should work.

## v0 tools

| Tool | Purpose |
|---|---|
| `device_info` | Return the Flipper's `device_info` dict (firmware, hardware, radio) |
| `list_ir_files` | List `.ir` files under a directory on the SD card |
| `list_ir_buttons` | Parse a saved `.ir` file and return its buttons |
| `send_ir_button` | Transmit a named button from a saved `.ir` file |
| `send_ir_signal` | Transmit an ad-hoc parsed IR signal by MSB-first integer hex (e.g. `NECext DF02 EE11`) |
| `list_universal_remotes` | List built-in universal IR remotes available on the firmware (ac, tv, fans, ...) |
| `list_universal_signals` | List the signal names for a built-in universal remote |
| `send_universal_signal` | Transmit a named signal from a built-in universal remote |
| `learn_ir_button` | Put the Flipper in RX, capture the next remote press, append it to a `.ir` file |

## Setup (Windows host, WSL2)

### 1. Forward the Flipper USB into WSL

Install [usbipd-win](https://github.com/dorssel/usbipd-win) on Windows:

```powershell
winget install usbipd
```

Then (from Windows PowerShell):

```powershell
usbipd list                          # find the Flipper's BUSID
usbipd bind --busid <X-Y>            # one-time, admin PowerShell
usbipd attach --wsl --busid <X-Y>    # each replug / reboot
```

After `attach`, the Flipper shows up in WSL as `/dev/ttyACM0`.

### 2. Grant serial access in WSL

```bash
sudo usermod -aG dialout $USER
```

Then restart WSL so the group is active:

```powershell
wsl.exe --shutdown
```

Reopen your shell. `groups` should now include `dialout`.

### 3. Install dependencies

From the repo root:

```bash
uv sync
```

### 4. Smoke test

```bash
uv run python scripts/smoketest_readonly.py
```

You should see device info, a list of files under `/ext/infrared/`, and parsed contents of each `.ir` file.

## Running

MCP stdio (for Claude Code, Cursor, etc.):

```bash
uv run flipper-mcp-bridge
```

HTTP REST API (for Home Assistant, `curl`, scripts):

```bash
uv run flipper-mcp-bridge --http --port 8765
```

Endpoints:

| Method | Path | Body / Query |
|---|---|---|
| `GET` | `/health` | — |
| `GET` | `/device` | — |
| `GET` | `/ir/files` | `?dir=/ext/infrared` |
| `GET` | `/ir/buttons` | `?file=/ext/infrared/Remote.ir` |
| `POST` | `/ir/send-button` | `{"file": "...", "button": "..."}` |
| `POST` | `/ir/send-signal` | `{"protocol": "...", "address": "...", "command": "..."}` |
| `GET` | `/ir/universal/list` | `?remote=ac` (omit to list available remotes) |
| `POST` | `/ir/universal/send` | `{"remote": "ac", "signal": "OFF"}` |
| `POST` | `/ir/learn` | `{"file": "...", "button": "...", "timeout_seconds": 30}` |

## Home Assistant integration

### Deployment note: reachability from HA

HA needs to be able to HTTP to the bridge. Two easy setups work out of the box:

1. **Run the bridge on the same host as HA** (Pi/NUC/server with Flipper plugged in). HA hits `http://127.0.0.1:8765`. Simplest.
2. **Run the bridge on any always-on Linux host on the LAN**. Start it with `--host 0.0.0.0` (the CLI prints a warning — there's no auth in v1, so only do this on a trusted LAN). HA hits `http://HOST:8765`.

**WSL2 caveat**: WSL2 uses NAT — the WSL IP isn't reachable from other hosts on the LAN. Running the bridge inside WSL2 and expecting HA on a different device to reach it requires `netsh interface portproxy` port-forwarding on the Windows host, or running the bridge on the Windows host directly (Python + pyserial work fine on Windows).

### Configuration

Drop this into `configuration.yaml`:

```yaml
rest_command:
  flipper_humidifier_toggle:
    url: "http://FLIPPER_HOST:8765/ir/send-button"
    method: POST
    content_type: "application/json"
    payload: '{"file":"/ext/infrared/Remote.ir","button":"Humid"}'

  flipper_ac_off:
    url: "http://FLIPPER_HOST:8765/ir/universal/send"
    method: POST
    content_type: "application/json"
    payload: '{"remote":"ac","signal":"OFF"}'
```

Then in automations or scripts:

```yaml
action:
  - service: rest_command.flipper_humidifier_toggle
```

For a switch-like entity, use a RESTful switch pointed at the same `/ir/send-button` endpoint (state is maintained by HA since the Flipper itself doesn't expose device state).

## Port selection

The bridge picks a serial device in this priority order:

1. Explicit `port=` argument (library use only)
2. `FLIPPER_PORT` environment variable
3. **Auto-detect**: the first attached device whose USB manufacturer is "Flipper Devices Inc." (or VID:PID `0483:5740`)
4. Fallback: `/dev/ttyACM0`

So in the common case you don't need to set anything. If you've got multiple CDC devices and want to pin a specific one:

```bash
FLIPPER_PORT=/dev/ttyACM1 uv run flipper-mcp-bridge
```

Or add `env` to the `.mcp.json` server entry.

## Registering with Claude Code

The repo ships a `.mcp.json` at the root — Claude Code picks it up automatically when you start a session in this directory (you'll be prompted to trust it on first launch). If you'd rather register it explicitly:

```bash
claude mcp add flipper -- uv run --directory "$(pwd)" flipper-mcp-bridge
```

## Known limitations

- **Capture latency**: `learn_ir_button` takes a few seconds before the Flipper actually starts listening. Press the remote a beat after calling the tool, not immediately.
- **Transport**: CLI-over-serial only. Protobuf RPC is not wired yet. Fine for IR at human pace; may be revisited for throughput-sensitive flows.
- **Blocked by foreground apps**: if a non-CLI app owns the Flipper (e.g. the Nightstand Clock idle screen), IR TX is blocked. The bridge tries `loader close` once on conflict, but some apps can only be exited manually on the device.
- **Raw IR capture not supported**: unknown-protocol signals won't round-trip via `learn_ir_button` yet.
- **IR only**: Sub-GHz, NFC, RFID, GPIO, BadUSB — none of these are wired.
