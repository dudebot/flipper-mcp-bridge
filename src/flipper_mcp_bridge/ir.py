from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class IRButton:
    name: str
    type: str  # "parsed" or "raw"
    # parsed
    protocol: str | None = None
    address: str | None = None
    command: str | None = None
    # raw
    frequency: int | None = None
    duty_cycle: int | None = None
    data: list[int] = field(default_factory=list)


def parse_ir_file(text: str) -> list[IRButton]:
    """Parse a Flipper .ir file. Buttons are separated by '# ' comment lines."""
    buttons: list[IRButton] = []
    current: dict[str, str] = {}

    def flush() -> None:
        if not current or "name" not in current:
            return
        t = current.get("type", "parsed")
        btn = IRButton(name=current["name"], type=t)
        if t == "parsed":
            btn.protocol = current.get("protocol")
            btn.address = current.get("address")
            btn.command = current.get("command")
        else:  # raw
            freq = current.get("frequency")
            dc = current.get("duty_cycle")
            btn.frequency = int(freq) if freq else None
            btn.duty_cycle = int(dc) if dc else None
            data = current.get("data", "")
            btn.data = [int(x) for x in data.split() if x]
        buttons.append(btn)

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            flush()
            current = {}
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        current[key.strip()] = value.strip()
    flush()

    # Filter header pseudo-entries (Filetype, Version keys leak into first if no '#' precedes them).
    return [b for b in buttons if b.name and b.name not in ("Filetype", "Version")]


def button_summary(btn: IRButton) -> dict:
    d: dict = {"name": btn.name, "type": btn.type}
    if btn.type == "parsed":
        d.update(protocol=btn.protocol, address=btn.address, command=btn.command)
    else:
        d.update(frequency=btn.frequency, duty_cycle=btn.duty_cycle, samples=len(btn.data))
    return d
