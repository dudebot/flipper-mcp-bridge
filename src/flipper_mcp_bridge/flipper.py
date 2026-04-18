from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

import serial
from serial.tools import list_ports

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


def _normalize_int_hex(value: str, n_bytes: int) -> str:
    """Normalize a user-supplied MSB-first hex string ('0xDF02', 'df02', 'DF 02')
    to the exact width `ir tx` expects. Unlike _shrink_hex this does NOT byte-swap."""
    v = value.strip().lower().replace(" ", "")
    if v.startswith("0x"):
        v = v[2:]
    if not v or any(c not in "0123456789abcdef" for c in v):
        raise ValueError(f"invalid hex value: {value!r}")
    want = n_bytes * 2
    if len(v) > want:
        raise ValueError(f"value too large for {n_bytes}-byte field: {value!r}")
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
    pass


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
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        self._ser = serial.Serial(self.port, self.baudrate, timeout=0.1)
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
        """Send a CLI command, return the response text (echo + prompt stripped)."""
        assert self._ser is not None, "call open() first"
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
        out = self.command(f"storage stat {path}", timeout=2.0)
        # Success looks like "File, size: N" or "Directory"; failure includes "error"/"not exist".
        return "error" not in out.lower() and "not exist" not in out.lower()

    def storage_read(self, path: str) -> str:
        """Return file contents as text. Strips the 'Size: N' header Flipper prepends."""
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
            raise FlipperError(f"unknown protocol {protocol!r}; known: {sorted(PROTOCOL_WIDTHS)}")
        addr_bytes, cmd_bytes = PROTOCOL_WIDTHS[protocol]
        addr = _shrink_hex(address, addr_bytes)
        cmd = _shrink_hex(command, cmd_bytes)
        return self._tx_with_recovery(f"ir tx {protocol} {addr} {cmd}")

    def ir_tx_direct(self, protocol: str, address_int_hex: str, command_int_hex: str) -> str:
        """Transmit a parsed IR signal from MSB-first integer hex (as `ir rx` reports).
        No byte-swapping. Use for ad-hoc sends where you have the integer value."""
        if protocol not in PROTOCOL_WIDTHS:
            raise FlipperError(f"unknown protocol {protocol!r}; known: {sorted(PROTOCOL_WIDTHS)}")
        addr_bytes, cmd_bytes = PROTOCOL_WIDTHS[protocol]
        addr = _normalize_int_hex(address_int_hex, addr_bytes)
        cmd = _normalize_int_hex(command_int_hex, cmd_bytes)
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

    def ir_tx_raw(self, frequency: int, duty_cycle: int, samples: list[int]) -> str:
        if not (10000 <= frequency <= 56000):
            raise ValueError("frequency out of range (10000..56000)")
        if not (0 <= duty_cycle <= 100):
            raise ValueError("duty_cycle out of range (0..100)")
        if len(samples) > 512:
            raise ValueError("max 512 samples")
        sample_str = " ".join(str(s) for s in samples)
        return self._tx_with_recovery(f"ir tx RAW F:{frequency} DC:{duty_cycle} {sample_str}")
