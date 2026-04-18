"""Smoke-test the read-only parts of the bridge against a connected Flipper.

Does NOT transmit IR. Run via:
    sg dialout -c 'uv run python scripts/smoketest_readonly.py'
"""
from __future__ import annotations

import json

from flipper_mcp_bridge.flipper import FlipperCLI
from flipper_mcp_bridge.ir import button_summary, parse_ir_file


def main() -> None:
    with FlipperCLI() as f:
        info = f.device_info()
        print("=== device_info (excerpt) ===")
        for k in ("hardware_name", "firmware_origin_fork", "firmware_version", "firmware_build_date"):
            print(f"  {k}: {info.get(k)}")

        print("\n=== storage list /ext/infrared ===")
        entries = f.storage_list("/ext/infrared")
        for e in entries:
            print(f"  [{e.kind}] {e.name} ({e.size}b)" if e.size else f"  [{e.kind}] {e.name}")

        ir_files = [e for e in entries if e.kind == "F" and e.name.endswith(".ir")]
        for e in ir_files:
            path = f"/ext/infrared/{e.name}"
            print(f"\n=== parse {path} ===")
            text = f.storage_read(path)
            buttons = parse_ir_file(text)
            print(json.dumps([button_summary(b) for b in buttons], indent=2))


if __name__ == "__main__":
    main()
