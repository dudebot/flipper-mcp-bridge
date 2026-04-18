from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .flipper import FlipperCLI, FlipperError
from .ir import button_summary, parse_ir_file

mcp = FastMCP("flipper-mcp-bridge")


def _flipper() -> FlipperCLI:
    # FlipperCLI() resolves port via explicit arg → FLIPPER_PORT env → USB autodetect → default.
    return FlipperCLI()


@mcp.tool()
def device_info() -> dict[str, str]:
    """Return the Flipper's device_info dict (firmware, hardware, radio, etc.)."""
    with _flipper() as f:
        return f.device_info()


@mcp.tool()
def list_ir_files(directory: str = "/ext/infrared") -> list[dict]:
    """List .ir files under a directory on the Flipper's SD card (non-recursive)."""
    with _flipper() as f:
        entries = f.storage_list(directory)
    return [
        {"name": e.name, "size": e.size, "path": f"{directory}/{e.name}"}
        for e in entries
        if e.kind == "F" and e.name.endswith(".ir")
    ]


@mcp.tool()
def list_ir_buttons(file: str) -> list[dict]:
    """Parse a saved .ir file on the Flipper and return its buttons."""
    with _flipper() as f:
        text = f.storage_read(file)
    return [button_summary(b) for b in parse_ir_file(text)]


@mcp.tool()
def send_ir_button(file: str, button: str) -> dict:
    """Transmit a named button from a saved .ir file on the Flipper. Raises on CLI error."""
    with _flipper() as f:
        text = f.storage_read(file)
        buttons = parse_ir_file(text)
        match = next((b for b in buttons if b.name == button), None)
        if match is None:
            names = [b.name for b in buttons]
            raise FlipperError(f"button {button!r} not found in {file}; available: {names}")
        if match.type == "parsed":
            assert match.protocol and match.address and match.command
            f.ir_tx_from_file_fields(match.protocol, match.address, match.command)
            return {"ok": True, "button": button, "mode": "parsed"}
        assert match.frequency is not None and match.duty_cycle is not None
        f.ir_tx_raw(match.frequency, match.duty_cycle, match.data)
        return {"ok": True, "button": button, "mode": "raw"}


@mcp.tool()
def send_ir_signal(protocol: str, address: str, command: str) -> dict:
    """Transmit an ad-hoc parsed IR signal. `address` and `command` are MSB-first integer
    hex as reported by `ir rx` (e.g. protocol='NECext', address='DF02', command='EE11').
    Optional '0x' prefix is fine. Raises on CLI error."""
    with _flipper() as f:
        f.ir_tx_direct(protocol, address, command)
    return {"ok": True, "protocol": protocol, "address": address, "command": command}


@mcp.tool()
def learn_ir_button(file: str, button: str, timeout_seconds: float = 30.0) -> dict:
    """Put Flipper in IR RX, wait for a single remote press (up to timeout_seconds), then
    append the captured signal as a named button to the given .ir file. Creates the file
    if it doesn't exist. The user must press a physical remote at the Flipper during the
    capture window."""
    with _flipper() as f:
        return f.ir_learn_and_save(file, button, timeout=timeout_seconds)


def main() -> None:
    mcp.run()
