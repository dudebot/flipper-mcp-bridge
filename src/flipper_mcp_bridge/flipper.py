from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass

import serial
from serial.tools import list_ports

# Single-process lock: all CLI sessions serialize through this so concurrent MCP tool
# calls, HTTP requests, and scripts can't interleave on one Flipper's serial port.
_SESSION_LOCK = threading.Lock()


def _reject_cli_unsafe(value: object, field: str, max_len: int = 512) -> str:
    """Validate a user-supplied string before it's interpolated into a Flipper CLI command.
    Rejects non-strings, control characters (CR/LF/NUL would let a caller inject extra
    commands over the line-based CLI), and unreasonable lengths."""
    if not isinstance(value, str):
        raise InvalidInputError(f"{field}: must be a string, got {type(value).__name__}")
    if not value:
        raise InvalidInputError(f"{field}: must not be empty")
    if len(value) > max_len:
        raise InvalidInputError(f"{field}: too long (max {max_len} chars)")
    for c in value:
        if ord(c) < 0x20 or c == "\x7f":
            raise InvalidInputError(f"{field}: contains control character")
    return value

IR_RX_LINE_RE = re.compile(r"(\w+),\s*A:0x([0-9A-Fa-f]+),\s*C:0x([0-9A-Fa-f]+)")
# Anchored form: matches only a fully-transmitted signal line (ends with newline),
# with an optional " R" repeat marker. Used to avoid committing on a mid-line chunk.
IR_RX_COMPLETE_RE = re.compile(
    r"^(\w+),\s*A:0x([0-9A-Fa-f]+),\s*C:0x([0-9A-Fa-f]+)(?:\s+R)?\s*\r?\n",
    re.MULTILINE,
)


def detect_flipper_port() -> str | None:
    """Return the serial device of the first attached Flipper Zero, or None."""
    for p in list_ports.comports():
        if p.manufacturer and "Flipper" in p.manufacturer:
            return p.device
        # Fallback: match by USB VID:PID (STMicro-assigned range Flipper uses).
        if p.vid == 0x0483 and p.pid == 0x5740:
            return p.device
    return None


def resolve_port(explicit: str | None = None) -> str:
    """Pick a serial port in priority order: explicit arg → FLIPPER_PORT env → auto-detect → default."""
    if explicit:
        return explicit
    env_port = os.environ.get("FLIPPER_PORT")
    if env_port:
        return env_port
    detected = detect_flipper_port()
    if detected:
        return detected
    return DEFAULT_PORT

PROMPT = b">: "
DEFAULT_PORT = "/dev/ttyACM0"
DEFAULT_BAUD = 115200

# (address_bytes, command_bytes) per IR protocol. .ir files store these zero-padded
# to 4 bytes regardless; the `ir tx` CLI wants the exact width for the protocol.
PROTOCOL_WIDTHS: dict[str, tuple[int, int]] = {
    "NEC":       (1, 1),
    "NECext":    (2, 2),
    "NEC42":     (2, 1),
    "NEC42ext":  (3, 1),
    "Samsung32": (2, 1),
    "RC5":       (1, 1),
    "RC5X":      (1, 1),
    "RC6":       (1, 1),
    "SIRC":      (1, 1),
    "SIRC15":    (1, 1),
    "SIRC20":    (2, 1),
    "Kaseikyo":  (3, 1),
    "RCA":       (1, 1),
    "Pioneer":   (2, 2),
}


def _shrink_hex(value: str, n_bytes: int) -> str:
    """Convert a .ir file hex field ('02 DF 00 00') to the `ir tx` CLI format.
    .ir files store bytes LSB-first; the CLI wants the integer as MSB-first hex,
    so we take the first n_bytes bytes and reverse them. Raises if bytes beyond
    the protocol width are non-zero (would otherwise be silently dropped)."""
    compact = value.replace(" ", "")
    want = n_bytes * 2
    if len(compact) < want:
        raise ValueError(f"need {n_bytes} bytes, got {len(compact)//2}: {value!r}")
    head = compact[:want]
    tail = compact[want:]
    if tail and any(c != "0" for c in tail):
        raise ValueError(f"value has non-zero bytes beyond protocol width: {value!r}")
    return "".join(head[i:i+2] for i in range(want - 2, -1, -2))


def _normalize_int_hex(value: str, n_bytes: int, field: str = "value") -> str:
    """Normalize a user-supplied MSB-first hex string ('0xDF02', 'df02', 'DF 02')
    to the exact width `ir tx` expects. Unlike _shrink_hex this does NOT byte-swap."""
    if not isinstance(value, str):
        raise InvalidInputError(f"{field}: must be a string")
    v = value.strip().lower().replace(" ", "")
    if v.startswith("0x"):
        v = v[2:]
    if not v or any(c not in "0123456789abcdef" for c in v):
        raise InvalidInputError(f"{field}: invalid hex value: {value!r}")
    want = n_bytes * 2
    if len(v) > want:
        raise InvalidInputError(f"{field}: value too large for {n_bytes}-byte field: {value!r}")
    return v.zfill(want).upper()


def _int_hex_to_file_bytes(int_hex: str, total_bytes: int = 4) -> str:
    """Convert integer hex ('DF02' or '0xDF02') to the .ir file byte field
    ('02 DF 00 00'): little-endian bytes, zero-padded to total_bytes, space-separated."""
    cleaned = int_hex.strip().lower().removeprefix("0x")
    if not cleaned or any(c not in "0123456789abcdef" for c in cleaned):
        raise ValueError(f"invalid hex value: {int_hex!r}")
    value = int(cleaned, 16)
    try:
        data = value.to_bytes(total_bytes, "little")
    except OverflowError as e:
        raise ValueError(f"value {int_hex!r} exceeds {total_bytes}-byte field width") from e
    return " ".join(f"{b:02X}" for b in data)


class FlipperError(RuntimeError):
    """Operational failure (device, FS, serial). Maps to 5xx over HTTP."""


class InvalidInputError(FlipperError):
    """Client-supplied input was invalid (missing field, wrong type, control chars, etc.).
    Maps to 4xx over HTTP so callers can distinguish their bugs from device issues."""


@dataclass
class StorageEntry:
    name: str
    kind: str  # "F" or "D"
    size: int | None


class FlipperCLI:
    def __init__(self, port: str | None = None, baudrate: int = DEFAULT_BAUD, timeout: float = 3.0):
        self.port = resolve_port(port)
        self.baudrate = baudrate
        self.timeout = timeout
        self._ser: serial.Serial | None = None

    def __enter__(self) -> "FlipperCLI":
        # Serialize all sessions through a process-level lock so concurrent callers
        # (HTTP requests, MCP tool calls, scripts) don't interleave on the serial port.
        _SESSION_LOCK.acquire()
        try:
            self.open()
        except Exception:
            # Close any partially-opened port so the exclusive fd isn't stranded
            # (would otherwise wedge the device until process exit).
            self.close()
            _SESSION_LOCK.release()
            raise
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.close()
        finally:
            _SESSION_LOCK.release()

    def open(self) -> None:
        # exclusive=True gets an OS-level advisory lock so a second PROCESS opening the
        # same tty also fails fast instead of silently cross-talking.
        self._ser = serial.Serial(self.port, self.baudrate, timeout=0.1, exclusive=True)
        # Drain any banner / pending output by waiting for quiescence (no bytes for ~200ms).
        # Cheaper and more robust than matching the prompt against the banner's ASCII art.
        quiet_deadline = time.monotonic() + 0.2
        hard_deadline = time.monotonic() + 2.0
        while time.monotonic() < hard_deadline:
            chunk = self._ser.read(4096)
            if chunk:
                quiet_deadline = time.monotonic() + 0.2
            elif time.monotonic() >= quiet_deadline:
                return
            else:
                time.sleep(0.02)

    def close(self) -> None:
        if self._ser is not None:
            self._ser.close()
            self._ser = None

    def _read_until_prompt(self, timeout: float | None = None) -> bytes:
        assert self._ser is not None
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = self._ser.read(4096)
            if chunk:
                buf += chunk
                # Flipper may emit BEL (\x07) or extra CR/LF after the prompt; tolerate that.
                if buf.rstrip(b"\x07\r\n\t ").endswith(b">:"):
                    return bytes(buf)
            else:
                time.sleep(0.02)
        raise FlipperError(f"timeout waiting for prompt; got: {bytes(buf)!r}")

    def command(self, cmd: str, timeout: float | None = None) -> str:
        """Send a CLI command, return the response text (echo + prompt stripped).
        Rejects embedded CR/LF/NUL so a caller can't smuggle in extra CLI commands."""
        assert self._ser is not None, "call open() first"
        if any(c in cmd for c in ("\r", "\n", "\x00")):
            raise FlipperError(f"CLI command contains control characters: {cmd!r}")
        self._ser.reset_input_buffer()
        self._ser.write((cmd + "\r\n").encode("utf-8"))
        raw = self._read_until_prompt(timeout=timeout)
        text = raw.decode("utf-8", errors="replace")
        # Strip only the trailing prompt — anchored at end, with optional CR/LF before and
        # BEL/whitespace after. rfind(">: ") would have eaten a literal ">: " inside output.
        text = re.sub(r"(?:\r?\n)?>: [\x07\r\n\t ]*\Z", "", text)
        # Flipper echoes the command on its own line; drop the first matching echo.
        lines = text.split("\r\n")
        if lines and lines[0].strip() == cmd.strip():
            lines = lines[1:]
        while lines and lines[-1].strip() == "":
            lines = lines[:-1]
        return "\n".join(lines)

    # --- High-level wrappers ---

    def device_info(self) -> dict[str, str]:
        out = self.command("device_info")
        info: dict[str, str] = {}
        for line in out.splitlines():
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            info[k.strip()] = v.strip()
        return info

    def storage_list(self, path: str) -> list[StorageEntry]:
        _reject_cli_unsafe(path, "path")
        out = self.command(f"storage list {path}")
        entries: list[StorageEntry] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            # Formats seen:
            #   [F] name.ext 620b
            #   [D] dirname
            if line.startswith("[D]"):
                entries.append(StorageEntry(name=line[3:].strip(), kind="D", size=None))
            elif line.startswith("[F]"):
                rest = line[3:].strip()
                # split trailing size token like "620b"
                parts = rest.rsplit(" ", 1)
                if len(parts) == 2 and parts[1].endswith("b") and parts[1][:-1].isdigit():
                    entries.append(StorageEntry(name=parts[0], kind="F", size=int(parts[1][:-1])))
                else:
                    entries.append(StorageEntry(name=rest, kind="F", size=None))
        return entries

    def storage_exists(self, path: str) -> bool:
        _reject_cli_unsafe(path, "path")
        out = self.command(f"storage stat {path}", timeout=2.0)
        # Success looks like "File, size: N" or "Directory"; failure includes "error"/"not exist".
        return "error" not in out.lower() and "not exist" not in out.lower()

    def storage_remove(self, path: str) -> None:
        _reject_cli_unsafe(path, "path")
        out = self.command(f"storage remove {path}", timeout=3.0).strip()
        if out and "error" in out.lower():
            raise FlipperError(f"storage remove failed: {out}")

    def storage_read(self, path: str) -> str:
        """Return file contents as text. Strips the 'Size: N' header Flipper prepends."""
        _reject_cli_unsafe(path, "path")
        out = self.command(f"storage read {path}", timeout=5.0)
        lines = out.split("\n")
        # First line is "Size: NNN"
        if lines and lines[0].startswith("Size:"):
            lines = lines[1:]
        return "\n".join(lines)

    def _tx_with_recovery(self, cli_command: str, timeout: float = 5.0) -> str:
        """Run an IR tx CLI command; if another app owns the peripheral, close it and retry once.
        Successful `ir tx` emits no output — any non-empty response is treated as an error."""
        output = self.command(cli_command, timeout=timeout)
        if "Other application is running" in output:
            close_output = self.command("loader close", timeout=2.0)
            if "has to be closed manually" in close_output:
                running = self.command("loader info", timeout=1.0).strip()
                raise FlipperError(
                    f"IR TX blocked: {running or 'an app is running'}. "
                    "This app must be closed on the device (press Back)."
                )
            output = self.command(cli_command, timeout=timeout)
        stripped = output.strip()
        if stripped:
            raise FlipperError(f"IR TX failed: {stripped}")
        return output

    def ir_tx_from_file_fields(self, protocol: str, address: str, command: str) -> str:
        """Transmit a parsed IR signal from .ir file fields (LSB-first zero-padded bytes).
        Converts them to the MSB-first integer hex the CLI expects."""
        if protocol not in PROTOCOL_WIDTHS:
            raise InvalidInputError(
                f"unknown protocol {protocol!r}; known: {sorted(PROTOCOL_WIDTHS)}"
            )
        addr_bytes, cmd_bytes = PROTOCOL_WIDTHS[protocol]
        try:
            addr = _shrink_hex(address, addr_bytes)
            cmd = _shrink_hex(command, cmd_bytes)
        except ValueError as e:
            raise InvalidInputError(str(e)) from e
        return self._tx_with_recovery(f"ir tx {protocol} {addr} {cmd}")

    def ir_tx_direct(self, protocol: str, address_int_hex: str, command_int_hex: str) -> str:
        """Transmit a parsed IR signal from MSB-first integer hex (as `ir rx` reports).
        No byte-swapping. Use for ad-hoc sends where you have the integer value."""
        if protocol not in PROTOCOL_WIDTHS:
            raise InvalidInputError(
                f"unknown protocol {protocol!r}; known: {sorted(PROTOCOL_WIDTHS)}"
            )
        addr_bytes, cmd_bytes = PROTOCOL_WIDTHS[protocol]
        addr = _normalize_int_hex(address_int_hex, addr_bytes, field="address")
        cmd = _normalize_int_hex(command_int_hex, cmd_bytes, field="command")
        return self._tx_with_recovery(f"ir tx {protocol} {addr} {cmd}")

    def ir_rx_one(self, timeout: float = 30.0) -> str:
        """Start `ir rx`, wait for one fully-transmitted signal line, abort, return the line.
        Commits only on a complete (newline-terminated) match of IR_RX_COMPLETE_RE so a
        partial chunk can't be mistaken for a full capture."""
        assert self._ser is not None
        self._ser.reset_input_buffer()
        self._ser.write(b"ir rx\r\n")
        deadline = time.monotonic() + timeout
        buf = bytearray()
        captured: str | None = None
        early_error: str | None = None
        try:
            while time.monotonic() < deadline:
                chunk = self._ser.read(4096)
                if chunk:
                    buf += chunk
                    text = buf.decode("utf-8", errors="replace")
                    # If the prompt returns before the rx header, the command failed (e.g. IR
                    # peripheral busy, loader blocked). Surface that rather than timing out.
                    if "Press Ctrl+C to abort" not in text and ">: " in text:
                        early_error = re.sub(
                            r"(?:\r?\n)?>: [\x07\r\n\t ]*\Z", "", text
                        ).replace("ir rx", "", 1).strip()
                        break
                    header_idx = text.find("Press Ctrl+C to abort")
                    if header_idx < 0:
                        continue
                    search_from = header_idx + len("Press Ctrl+C to abort")
                    m = IR_RX_COMPLETE_RE.search(text, search_from)
                    if m:
                        captured = m.group(0).rstrip("\r\n")
                        break
                else:
                    time.sleep(0.02)
        finally:
            # Only send Ctrl+C if we actually entered rx mode (not on early-error path).
            if early_error is None:
                self._ser.write(b"\x03")
                self._read_until_prompt(timeout=1.5)
        if early_error is not None:
            raise FlipperError(f"ir rx failed: {early_error or 'unknown error'}")
        if captured is None:
            raise FlipperError(f"no IR signal received within {timeout}s")
        return captured

    def storage_append(self, path: str, content: str) -> None:
        """Append text content to a file on the SD card using `storage write` (Ctrl+C to stop).
        If the write command errors before entering input mode (e.g. bad path), raises rather
        than silently discarding the error and sending content into the void."""
        _reject_cli_unsafe(path, "path")
        assert self._ser is not None
        self._ser.reset_input_buffer()
        self._ser.write(f"storage write {path}\r\n".encode("utf-8"))
        # If the command errors, Flipper emits the error then returns to prompt immediately.
        # If it entered input mode, no prompt arrives until we send Ctrl+C later. So: read
        # briefly; if a prompt appears, that's an error response.
        time.sleep(0.2)
        pre = self._ser.read(4096).decode("utf-8", errors="replace")
        if ">: " in pre:
            # Strip echo and prompt, what remains is the error message.
            err = pre.replace(f"storage write {path}", "", 1)
            err = re.sub(r"(?:\r?\n)?>: [\x07\r\n\t ]*\Z", "", err).strip()
            raise FlipperError(f"storage write failed: {err or 'unknown error'}")
        # In input mode. Send content, flush, then Ctrl+C to commit.
        if not content.endswith("\n"):
            content += "\n"
        self._ser.write(content.encode("utf-8"))
        self._ser.flush()
        time.sleep(0.15)
        self._ser.write(b"\x03")
        raw = self._read_until_prompt(timeout=3.0)
        post = raw.decode("utf-8", errors="replace")
        post = re.sub(r"(?:\r?\n)?>: [\x07\r\n\t ]*\Z", "", post).strip()
        # Surface known FS error markers that can appear at commit time (e.g. full disk).
        low = post.lower()
        if any(tok in low for tok in ("error", "fail", "full", "denied", "not exist")):
            raise FlipperError(f"storage write commit failed: {post}")

    def ir_learn_and_save(
        self, file: str, button_name: str, timeout: float = 30.0
    ) -> dict:
        """Capture one IR signal and append it as a named button to an .ir file.
        Creates the file with the standard header if it doesn't exist yet."""
        _reject_cli_unsafe(file, "file")
        # Button name is written into the .ir file (`name: {x}`) — newlines would inject
        # extra lines and forge fake buttons; also a colon would break the key:value format.
        _reject_cli_unsafe(button_name, "button", max_len=128)
        if ":" in button_name:
            raise InvalidInputError("button: must not contain ':'")
        captured = self.ir_rx_one(timeout=timeout)
        m = IR_RX_LINE_RE.match(captured)
        if not m:
            raise FlipperError(f"could not parse RX line: {captured!r}")
        proto, addr_int_hex, cmd_int_hex = m.group(1), m.group(2), m.group(3)
        addr_file = _int_hex_to_file_bytes(addr_int_hex, 4)
        cmd_file = _int_hex_to_file_bytes(cmd_int_hex, 4)

        if not self.storage_exists(file):
            header = "Filetype: IR signals file\nVersion: 1\n"
            self.storage_append(file, header)

        entry = (
            f"#\n"
            f"name: {button_name}\n"
            f"type: parsed\n"
            f"protocol: {proto}\n"
            f"address: {addr_file}\n"
            f"command: {cmd_file}\n"
        )
        self.storage_append(file, entry)
        return {
            "button": button_name,
            "protocol": proto,
            "address": addr_file,
            "command": cmd_file,
            "raw_capture": captured,
        }

    def _storage_remove_if_exists(self, path: str) -> None:
        try:
            self.storage_remove(path)
        except FlipperError:
            pass

    def ir_delete_button(self, file: str, button_name: str) -> dict:
        """Remove a named button from an .ir file. Backup → rewrite → cleanup. If the
        rewrite fails at any step, restore the original from the backup so the user's
        file is never lost."""
        from .ir import parse_ir_file  # local import to avoid a module cycle

        _reject_cli_unsafe(file, "file")
        _reject_cli_unsafe(button_name, "button", max_len=128)
        text = self.storage_read(file)
        buttons = parse_ir_file(text)
        if not any(b.name == button_name for b in buttons):
            raise FlipperError(f"button {button_name!r} not found in {file}")
        kept = [b for b in buttons if b.name != button_name]
        # Rebuild the file in the canonical format.
        parts = ["Filetype: IR signals file\nVersion: 1\n"]
        for b in kept:
            if b.type == "parsed":
                parts.append(
                    f"#\nname: {b.name}\ntype: parsed\n"
                    f"protocol: {b.protocol}\naddress: {b.address}\ncommand: {b.command}\n"
                )
            else:
                data = " ".join(str(x) for x in b.data)
                parts.append(
                    f"#\nname: {b.name}\ntype: raw\n"
                    f"frequency: {b.frequency}\nduty_cycle: {b.duty_cycle}\ndata: {data}\n"
                )
        new_content = "".join(parts)

        backup = file + ".bak"
        self._storage_remove_if_exists(backup)
        copy_out = self.command(f"storage copy {file} {backup}", timeout=5.0).strip()
        if copy_out and "error" in copy_out.lower():
            raise FlipperError(f"backup copy failed: {copy_out}")
        try:
            self.storage_remove(file)
            self.storage_append(file, new_content)
        except Exception as delete_exc:
            # Roll back: wipe any partial rewrite, then restore from backup. If the
            # restore itself blows up (disconnect, timeout, FS error), surface the
            # backup path so the user can recover manually.
            self._storage_remove_if_exists(file)
            try:
                restore_out = self.command(f"storage rename {backup} {file}", timeout=3.0).strip()
                if restore_out and "error" in restore_out.lower():
                    raise FlipperError(restore_out)
            except Exception as restore_exc:
                raise FlipperError(
                    f"delete failed ({delete_exc}) AND restore failed ({restore_exc}). "
                    f"Your original file is preserved at {backup} on the SD card — "
                    f"rename it back manually."
                ) from delete_exc
            raise
        self._storage_remove_if_exists(backup)
        return {"file": file, "deleted": button_name, "remaining": [b.name for b in kept]}

    def ir_universal_remotes(self) -> list[str]:
        """Return the built-in universal remote names the firmware exposes (ac, tv, ...)."""
        out = self.command("ir help")
        for line in out.splitlines():
            if "Available universal remotes:" in line:
                _, _, rest = line.partition(":")
                return [r.strip() for r in rest.split() if r.strip()]
        return []

    def ir_universal_signals(self, remote: str) -> list[str]:
        """Return the signal names known for a universal remote (e.g. 'POWER', 'VOL+').
        Raises FlipperError if the firmware response looks like an error/usage dump rather
        than a signal list."""
        _reject_cli_unsafe(remote, "remote", max_len=64)
        out = self.command(f"ir universal list {remote}", timeout=3.0)
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        # Error/usage output from the firmware usually contains one of these — treat as
        # "parser couldn't understand the response" rather than returning garbage as signals.
        joined_low = "\n".join(lines).lower()
        if any(tok in joined_low for tok in ("wrong arguments", "usage:", "available universal remotes")):
            raise FlipperError(f"ir universal list {remote!r} failed: {out.strip()}")
        # Firmware prepends a header line like "Valid signals:" — drop header-ish lines.
        return [ln for ln in lines if not ln.endswith(":")]

    def ir_universal_send(self, remote: str, signal: str) -> str:
        """Transmit a named signal from a built-in universal remote."""
        _reject_cli_unsafe(remote, "remote", max_len=64)
        _reject_cli_unsafe(signal, "signal", max_len=128)
        return self._tx_with_recovery(f"ir universal {remote} {signal}")

    def ir_tx_raw(self, frequency: int, duty_cycle: int, samples: list[int]) -> str:
        if not (10000 <= frequency <= 56000):
            raise ValueError("frequency out of range (10000..56000)")
        if not (0 <= duty_cycle <= 100):
            raise ValueError("duty_cycle out of range (0..100)")
        if len(samples) > 512:
            raise ValueError("max 512 samples")
        sample_str = " ".join(str(s) for s in samples)
        return self._tx_with_recovery(f"ir tx RAW F:{frequency} DC:{duty_cycle} {sample_str}")
