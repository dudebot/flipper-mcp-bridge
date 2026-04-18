"""Start ir rx, wait for the first signal (30s), print raw Flipper output.
Run, then press any remote button at the Flipper.
    sg dialout -c 'uv run python scripts/capture_once.py'
"""
from flipper_mcp_bridge.flipper import FlipperCLI

with FlipperCLI() as f:
    print("Point a remote at the Flipper and press any button...")
    out = f.ir_rx_one(timeout=30.0)
    print("=== captured ===")
    print(repr(out))
    print("=== rendered ===")
    print(out)
