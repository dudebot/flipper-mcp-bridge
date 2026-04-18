"""HTTP REST API for the Flipper bridge — built for Home Assistant's RESTful
Command / RESTful Switch platforms. Runs on a separate port from the MCP stdio
server; both can be used simultaneously since each opens its own FlipperCLI."""
from __future__ import annotations

import json

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .flipper import FlipperCLI, FlipperError, InvalidInputError
from .ir import button_summary, parse_ir_file

MAX_BODY_BYTES = 64 * 1024  # generous for .ir file content; rejects obvious abuse.


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


async def _body_json(request: Request) -> dict:
    # Cap the body read so a LAN caller can't send an endless stream.
    size = 0
    chunks = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise InvalidInputError(f"request body exceeds {MAX_BODY_BYTES} bytes")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise InvalidInputError(f"invalid JSON body: {e}")
    if not isinstance(data, dict):
        raise InvalidInputError("JSON body must be an object")
    return data


def _require_str(data: dict, field: str) -> str:
    if field not in data:
        raise InvalidInputError(f"missing required field: {field}")
    value = data[field]
    if not isinstance(value, str):
        raise InvalidInputError(f"field {field} must be a string, got {type(value).__name__}")
    return value


def _optional_float(data: dict, field: str, default: float, low: float, high: float) -> float:
    if field not in data or data[field] is None:
        return default
    v = data[field]
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise InvalidInputError(f"field {field} must be a number")
    v = float(v)
    if not (low <= v <= high):
        raise InvalidInputError(f"field {field} must be in [{low}, {high}]")
    return v


async def health(request: Request) -> JSONResponse:
    try:
        with FlipperCLI() as f:
            info = f.device_info()
        return JSONResponse({
            "ok": True,
            "hardware_name": info.get("hardware_name"),
            "firmware_version": info.get("firmware_version"),
            "firmware_origin_fork": info.get("firmware_origin_fork"),
        })
    except Exception as e:
        return _err(503, str(e))


async def device(request: Request) -> JSONResponse:
    with FlipperCLI() as f:
        return JSONResponse(f.device_info())


async def ir_files(request: Request) -> JSONResponse:
    directory = request.query_params.get("dir", "/ext/infrared")
    with FlipperCLI() as f:
        entries = f.storage_list(directory)
    return JSONResponse([
        {"name": e.name, "size": e.size, "path": f"{directory}/{e.name}"}
        for e in entries if e.kind == "F" and e.name.endswith(".ir")
    ])


async def ir_buttons(request: Request) -> JSONResponse:
    file = request.query_params.get("file")
    if not file:
        return _err(400, "missing ?file= query param")
    with FlipperCLI() as f:
        text = f.storage_read(file)
    return JSONResponse([button_summary(b) for b in parse_ir_file(text)])


async def ir_send_button(request: Request) -> JSONResponse:
    data = await _body_json(request)
    file = _require_str(data, "file")
    button = _require_str(data, "button")
    with FlipperCLI() as f:
        text = f.storage_read(file)
        buttons = parse_ir_file(text)
        match = next((b for b in buttons if b.name == button), None)
        if match is None:
            return _err(404, f"button {button!r} not found in {file}")
        if match.type == "parsed":
            assert match.protocol and match.address and match.command
            f.ir_tx_from_file_fields(match.protocol, match.address, match.command)
        else:
            assert match.frequency is not None and match.duty_cycle is not None
            f.ir_tx_raw(match.frequency, match.duty_cycle, match.data)
    return JSONResponse({"ok": True, "button": button})


async def ir_send_signal(request: Request) -> JSONResponse:
    data = await _body_json(request)
    protocol = _require_str(data, "protocol")
    address = _require_str(data, "address")
    command = _require_str(data, "command")
    with FlipperCLI() as f:
        f.ir_tx_direct(protocol, address, command)
    return JSONResponse({"ok": True, "protocol": protocol, "address": address, "command": command})


async def ir_universal_send(request: Request) -> JSONResponse:
    data = await _body_json(request)
    remote = _require_str(data, "remote")
    signal = _require_str(data, "signal")
    with FlipperCLI() as f:
        f.ir_universal_send(remote, signal)
    return JSONResponse({"ok": True, "remote": remote, "signal": signal})


async def ir_universal_list(request: Request) -> JSONResponse:
    remote = request.query_params.get("remote")
    with FlipperCLI() as f:
        if remote:
            return JSONResponse({"remote": remote, "signals": f.ir_universal_signals(remote)})
        return JSONResponse({"remotes": f.ir_universal_remotes()})


async def ir_delete_button(request: Request) -> JSONResponse:
    data = await _body_json(request)
    file = _require_str(data, "file")
    button = _require_str(data, "button")
    with FlipperCLI() as f:
        return JSONResponse(f.ir_delete_button(file, button))


async def ir_learn(request: Request) -> JSONResponse:
    data = await _body_json(request)
    file = _require_str(data, "file")
    button = _require_str(data, "button")
    timeout = _optional_float(data, "timeout_seconds", default=30.0, low=1.0, high=300.0)
    with FlipperCLI() as f:
        result = f.ir_learn_and_save(file, button, timeout=timeout)
    return JSONResponse({"ok": True, **result})


async def error_handler(request: Request, exc: Exception) -> JSONResponse:
    # InvalidInputError → 4xx; operational FlipperError → 503; anything else → 500.
    if isinstance(exc, InvalidInputError):
        return _err(400, str(exc))
    if isinstance(exc, FlipperError):
        return _err(503, str(exc))
    return _err(500, str(exc))


def create_app() -> Starlette:
    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/device", device, methods=["GET"]),
        Route("/ir/files", ir_files, methods=["GET"]),
        Route("/ir/buttons", ir_buttons, methods=["GET"]),
        Route("/ir/send-button", ir_send_button, methods=["POST"]),
        Route("/ir/send-signal", ir_send_signal, methods=["POST"]),
        Route("/ir/universal/send", ir_universal_send, methods=["POST"]),
        Route("/ir/universal/list", ir_universal_list, methods=["GET"]),
        Route("/ir/delete-button", ir_delete_button, methods=["POST"]),
        Route("/ir/learn", ir_learn, methods=["POST"]),
    ]
    return Starlette(
        routes=routes,
        exception_handlers={
            InvalidInputError: error_handler,
            FlipperError: error_handler,
            Exception: error_handler,
        },
    )


def run_http(host: str = "127.0.0.1", port: int = 8765) -> None:
    import sys
    import uvicorn
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"\n*** WARNING: binding HTTP API to {host}:{port} — this exposes IR send, "
            "IR learn, and .ir file delete endpoints to any reachable host on that "
            "interface with NO authentication. v1 has no auth layer by design; only do "
            "this on a trusted LAN.\n",
            file=sys.stderr,
        )
    uvicorn.run(create_app(), host=host, port=port, log_level="info")
