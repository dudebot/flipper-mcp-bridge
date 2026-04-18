"""HTTP REST API for the Flipper bridge — built for Home Assistant's RESTful
Command / RESTful Switch platforms. Runs on a separate port from the MCP stdio
server; both can be used simultaneously since each opens its own FlipperCLI."""
from __future__ import annotations

import json

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .flipper import FlipperCLI, FlipperError
from .ir import button_summary, parse_ir_file


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


async def _body_json(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise FlipperError(f"invalid JSON body: {e}")
    if not isinstance(data, dict):
        raise FlipperError("JSON body must be an object")
    return data


def _require(data: dict, *fields: str) -> tuple:
    missing = [f for f in fields if f not in data]
    if missing:
        raise FlipperError(f"missing required fields: {missing}")
    return tuple(data[f] for f in fields)


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
    try:
        data = await _body_json(request)
        file, button = _require(data, "file", "button")
    except FlipperError as e:
        return _err(400, str(e))
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
    try:
        data = await _body_json(request)
        protocol, address, command = _require(data, "protocol", "address", "command")
    except FlipperError as e:
        return _err(400, str(e))
    with FlipperCLI() as f:
        f.ir_tx_direct(protocol, address, command)
    return JSONResponse({"ok": True, "protocol": protocol, "address": address, "command": command})


async def ir_universal_send(request: Request) -> JSONResponse:
    try:
        data = await _body_json(request)
        remote, signal = _require(data, "remote", "signal")
    except FlipperError as e:
        return _err(400, str(e))
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
    try:
        data = await _body_json(request)
        file, button = _require(data, "file", "button")
    except FlipperError as e:
        return _err(400, str(e))
    with FlipperCLI() as f:
        return JSONResponse(f.ir_delete_button(file, button))


async def ir_learn(request: Request) -> JSONResponse:
    try:
        data = await _body_json(request)
        file, button = _require(data, "file", "button")
    except FlipperError as e:
        return _err(400, str(e))
    timeout = float(data.get("timeout_seconds", 30.0))
    with FlipperCLI() as f:
        result = f.ir_learn_and_save(file, button, timeout=timeout)
    return JSONResponse({"ok": True, **result})


async def error_handler(request: Request, exc: Exception) -> JSONResponse:
    status = 400 if isinstance(exc, FlipperError) else 500
    return _err(status, str(exc))


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
            FlipperError: error_handler,
            Exception: error_handler,
        },
    )


def run_http(host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn
    uvicorn.run(create_app(), host=host, port=port, log_level="info")
