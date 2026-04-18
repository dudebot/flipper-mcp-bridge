"""Capture one IR signal and save it as a named button in an .ir file.
    sg dialout -c 'uv run python scripts/learn.py /ext/infrared/Test.ir MyButton'
"""
from __future__ import annotations

import json
import sys

from flipper_mcp_bridge.flipper import FlipperCLI


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    file, button = sys.argv[1], sys.argv[2]
    with FlipperCLI() as f:
        print(f"Point a remote at the Flipper and press the '{button}' button...")
        result = f.ir_learn_and_save(file, button, timeout=30.0)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
