"""Fire a named button from an .ir file on the Flipper. USAGE:
    sg dialout -c 'uv run python scripts/fire.py /ext/infrared/Remote.ir Humid'
"""
from __future__ import annotations

import sys

from flipper_mcp_bridge.flipper import FlipperCLI
from flipper_mcp_bridge.ir import parse_ir_file


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    file, button = sys.argv[1], sys.argv[2]
    with FlipperCLI() as f:
        text = f.storage_read(file)
        buttons = parse_ir_file(text)
        match = next((b for b in buttons if b.name == button), None)
        if match is None:
            print(f"button {button!r} not found. Available: {[b.name for b in buttons]}")
            sys.exit(1)
        if match.type == "parsed":
            print(f"TX parsed: {match.protocol} {match.address} {match.command}")
            out = f.ir_tx_from_file_fields(match.protocol, match.address, match.command)
        else:
            print(f"TX raw: {match.frequency} Hz, {len(match.data)} samples")
            out = f.ir_tx_raw(match.frequency, match.duty_cycle, match.data)
        print("response:", repr(out))


if __name__ == "__main__":
    main()
