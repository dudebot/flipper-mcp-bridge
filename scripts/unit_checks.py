"""Pure-function sanity checks — no Flipper needed. Run before committing.
    uv run python scripts/unit_checks.py
"""
from flipper_mcp_bridge.flipper import (
    _int_hex_to_file_bytes,
    _normalize_int_hex,
    _reject_cli_unsafe,
    _shrink_hex,
    FlipperError,
    InvalidInputError,
)
from flipper_mcp_bridge.ir import parse_ir_file


failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def check_raises(fn, exc_cls, msg):
    try:
        fn()
    except exc_cls:
        return
    except Exception as e:
        failures.append(f"{msg}: wrong exception {type(e).__name__}: {e}")
        return
    failures.append(f"{msg}: did not raise")


# _shrink_hex: NECext 16-bit, byte-swap, validates zero-padded tail.
check(_shrink_hex("02 DF 00 00", 2) == "DF02", "shrink NECext")
check(_shrink_hex("04 00 00 00", 1) == "04", "shrink NEC")
check_raises(lambda: _shrink_hex("02 DF 01 00", 2), ValueError, "shrink rejects non-zero tail")
check_raises(lambda: _shrink_hex("02", 2), ValueError, "shrink rejects too-short")

# _int_hex_to_file_bytes: DF02 → "02 DF 00 00"
check(_int_hex_to_file_bytes("DF02", 4) == "02 DF 00 00", "int2file DF02")
check(_int_hex_to_file_bytes("0xDF02", 4) == "02 DF 00 00", "int2file 0x prefix")
check(_int_hex_to_file_bytes("4", 4) == "04 00 00 00", "int2file small")
check_raises(lambda: _int_hex_to_file_bytes("", 4), ValueError, "int2file rejects empty")
check_raises(lambda: _int_hex_to_file_bytes("zz", 4), ValueError, "int2file rejects non-hex")
check_raises(lambda: _int_hex_to_file_bytes("FFFFFFFFFF", 4), ValueError, "int2file rejects oversize")

# _normalize_int_hex: left-pads to exact width, rejects junk.
check(_normalize_int_hex("DF02", 2) == "DF02", "normalize 2-byte pass-through")
check(_normalize_int_hex("4", 1) == "04", "normalize left-pad 1-byte")
check(_normalize_int_hex("0x04", 1) == "04", "normalize 0x prefix")
check_raises(lambda: _normalize_int_hex("", 2), InvalidInputError, "normalize rejects empty")
check_raises(lambda: _normalize_int_hex("12345", 2), InvalidInputError, "normalize rejects oversize")

# _reject_cli_unsafe: CRLF injection guard.
check(_reject_cli_unsafe("/ext/infrared/Remote.ir", "path") == "/ext/infrared/Remote.ir", "cli-safe ok")
check_raises(lambda: _reject_cli_unsafe("good\rhack", "x"), InvalidInputError, "rejects CR")
check_raises(lambda: _reject_cli_unsafe("good\nhack", "x"), InvalidInputError, "rejects LF")
check_raises(lambda: _reject_cli_unsafe("good\x00hack", "x"), InvalidInputError, "rejects NUL")
check_raises(lambda: _reject_cli_unsafe(123, "x"), InvalidInputError, "rejects non-string")
check_raises(lambda: _reject_cli_unsafe("", "x"), InvalidInputError, "rejects empty")

# InvalidInputError is-a FlipperError so existing catch-FlipperError paths still work.
check(issubclass(InvalidInputError, FlipperError), "InvalidInputError subclasses FlipperError")

# parse_ir_file: empty, header-only, multi-button.
check(parse_ir_file("") == [], "parse empty → empty list")
check(parse_ir_file("Filetype: IR signals file\nVersion: 1\n") == [], "parse header-only → empty list")
multi = """Filetype: IR signals file
Version: 1
#
name: A
type: parsed
protocol: NEC
address: 04 00 00 00
command: 01 00 00 00
#
name: B
type: parsed
protocol: NEC
address: 04 00 00 00
command: 02 00 00 00
"""
parsed = parse_ir_file(multi)
check(len(parsed) == 2, f"parse 2-button count, got {len(parsed)}")
check(parsed[0].name == "A" and parsed[1].name == "B", "parse 2-button order")


if failures:
    print("FAILURES:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("OK — all pure-function checks passed")
