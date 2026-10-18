"""Web API: the `Engine` over HTTP.

Scope and security model, stated up front because they drive every choice here:

*This is a **local, single-machine** service.* It binds ``127.0.0.1`` only and
carries **no authentication**. That is a deliberate decision, not an oversight:
the caller is another program on the same desktop, and a token would only add a
failure mode (where do I get it? why did it rotate?) without adding security --
anything that can reach a loopback port can already read the key from the process.

Consequences we accept and document:

* Requests are trusted. No rate limiting, no CSRF handling, no request signing.
* The only defence that matters is **not leaving loopback**. ``run_server``
  refuses a non-loopback host unless ``allow_remote=True`` is passed explicitly,
  and the CLI has no flag for that, so accidental exposure needs a code change.
* Upload size is still capped. Not for security, but because a 500 MB image
  would be silently decoded into gigabytes of pixels and kill the process.

The default port is **8731**: high, unassigned, and nothing else on a typical
machine wants it. Ports like 8000/8080 are commonly grabbed by dev servers,
which would turn "start the API" into "hunt for a free port".
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from .channels import Carrier, CarrierKind
from .core.errors import InvalidInput, StegoError
from .engine import Config, Engine

#: Loopback-only default port. Chosen to be memorable (`MKX1` is not, but 8731
#: is not claimed by anything common) and above the usual dev-server range.
DEFAULT_PORT = 8731
DEFAULT_HOST = "127.0.0.1"

#: Encoded images are much smaller than this; a bigger upload is an accident.
MAX_UPLOAD_BYTES = 64 * 1024 * 1024


@dataclass
class ApiConfig:
    """Everything configurable about the service."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    model_dir: Path | None = None
    max_upload_bytes: int = MAX_UPLOAD_BYTES


def _json_error(error: StegoError) -> JSONResponse:
    """The one place a project error becomes an HTTP body.

    ``code`` is what programs branch on; ``reasons``/``hints`` are what a human
    reads. Flattening them into a single string here would throw away the
    distinction the whole ``core.errors`` hierarchy exists to preserve.
    """
    return JSONResponse(
        status_code=error.http_status,
        content={
            "code": error.code,
            "message": str(error),
            "reasons": list(error.reasons),
            "hints": list(error.hints),
        },
    )


def _carrier_kind(text: str) -> CarrierKind:
    try:
        return CarrierKind(text)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"kind 必须是 {[k.value for k in CarrierKind]} 之一，收到 {text!r}",
        ) from None


def create_app(config: ApiConfig | None = None, *, engine: Engine | None = None) -> FastAPI:
    """Build the ASGI app. ``engine`` is injectable so tests skip model training."""
    settings = config or ApiConfig()
    app = FastAPI(
        title="markovCryptor",
        description="多信道隐写加解密（本机服务，无鉴权，仅监听回环地址）",
        version="1.0",
    )

    # One engine for the process: it owns the model cache, and rebuilding it per
    # request would throw that cache away on every call.
    app.state.engine = engine or Engine(
        Config(model_dir=settings.model_dir) if settings.model_dir else Config()
    )
    app.state.config = settings

    @app.exception_handler(StegoError)
    async def _handle_stego_error(_request: Request, exc: StegoError) -> JSONResponse:
        return _json_error(exc)

    # ------------------------------------------------------------------ metadata

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Liveness plus what the service can currently do."""
        engine_ = app.state.engine
        return {
            "ok": True,
            "channels": [c.id for c in engine_.channels()],
            "models": len(engine_.models()),
        }

    @app.get("/channels")
    def channels() -> list[dict[str, Any]]:
        """Every channel plus its ``options_schema``, for form generation."""
        return app.state.engine.describe_channels()

    # -------------------------------------------------------------------- probe

    @app.post("/probe")
    async def probe(
        channel: str = Query(..., description="信道 id，见 GET /channels"),
        payload_bytes: int = Query(..., ge=0, description="计划装入的载荷字节数"),
        kind: str = Query("image", description="载体类型：image / animation / text"),
        file: UploadFile | None = None,
        text: str | None = None,
    ) -> dict[str, Any]:
        """Ask whether a carrier can hold a payload, without embedding anything.

        Accepts either an uploaded file or inline ``text`` (the Chinese channel
        takes no carrier at all, so neither is required there).
        """
        carrier = await _read_carrier(kind, file, text, settings.max_upload_bytes)
        feas = app.state.engine.probe(channel, carrier, payload_bytes)
        return {
            "ok": feas.ok,
            "capacity_bytes": feas.capacity_bytes,
            "required_bytes": feas.required_bytes,
            "utilization": round(feas.utilization, 4),
            "reasons": list(feas.reasons),
            "hints": list(feas.hints),
            "cost": feas.cost,
        }

    # ------------------------------------------------------------------ embed

    @app.post("/embed")
    async def embed(
        channel: str = Query(...),
        key: str = Query("", description="口令；留空表示不加密（仅中文信道支持）"),
        kind: str = Query("image"),
        payload_file: UploadFile | None = None,
        cover_file: UploadFile | None = None,
        text: str | None = None,
        options: str | None = Query(None, description="JSON 字符串形式的信道参数"),
    ) -> Response:
        """Hide a payload in a carrier and return the carrier as a binary body.

        Two uploads, kept separate on purpose: ``payload_file`` is the secret,
        ``cover_file`` is what hides it. Overloading one field for both would
        make the Chinese channel (which has no cover) expressible only by
        convention. Response headers carry the report, because the body is
        binary and the caller still needs to know the effective options -- e.g.
        the QIM step that actually worked after auto-escalation.
        """
        payload = await _read_upload_required(payload_file, settings.max_upload_bytes)
        carrier = await _read_carrier(kind, cover_file, text, settings.max_upload_bytes)
        parsed = _parse_options(options)
        env = app.state.engine.embed(channel, carrier, payload, key.encode(), parsed)

        body = env.primary.encode("utf-8") if isinstance(env.primary, str) else env.primary
        return Response(
            content=body,
            media_type=_media_for(channel),
            headers=_report_headers(env.report),
        )

    # ------------------------------------------------------------------ extract

    @app.post("/extract")
    async def extract(
        channel: str = Query(...),
        key: str = Query(""),
        kind: str = Query("image"),
        file: UploadFile | None = None,
        text: str | None = None,
        options: str | None = Query(None),
    ) -> Response:
        """Recover the hidden payload and return it as a binary body."""
        carrier = await _read_carrier(kind, file, text, settings.max_upload_bytes)
        parsed = _parse_options(options)
        payload = app.state.engine.extract(channel, carrier, key.encode(), parsed)
        return Response(
            content=payload,
            media_type="application/octet-stream",
            headers={"X-Payload-Bytes": str(len(payload))},
        )

    # ------------------------------------------------------------------- helper

    return app


async def _read_upload_required(file: UploadFile | None, limit: int) -> bytes:
    if file is None:
        raise InvalidInput(
            "缺少载荷文件 payload_file",
            hints=("载荷是要隐藏的内容，用 -F payload_file=@secret.bin 上传",),
        )
    return await _read_upload(file, limit)


async def _read_upload(file: UploadFile, limit: int) -> bytes:
    """Read an upload with a hard cap, so a huge file cannot exhaust memory."""
    data = await file.read()
    if len(data) > limit:
        raise InvalidInput(
            f"上传文件 {len(data) // 1024 // 1024} MB 超过上限 {limit // 1024 // 1024} MB",
            hints=("先压缩或裁剪文件，或调大 ApiConfig.max_upload_bytes",),
        )
    return data


async def _read_carrier(kind: str, file: UploadFile | None, text: str | None,
                        limit: int = MAX_UPLOAD_BYTES) -> Carrier:
    """Build a lazy carrier from either an upload or inline text."""
    carrier_kind = _carrier_kind(kind)
    if carrier_kind is CarrierKind.TEXT:
        # Either inline ``text`` or an uploaded file may carry the cover text.
        # Uploading is the normal case for extract (the cover is a document the
        # caller already has) while inline text is what the Chinese channel's
        # *embed* passes when it generates one -- so both must work, and
        # ignoring the upload made every round-trip through the API fail.
        if text is not None:
            return Carrier.of_text(text, label="request-text")
        if file is not None:
            raw = await _read_upload(file, limit)
            try:
                body = raw.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise InvalidInput(
                    f"载体文本不是 UTF-8：{exc}",
                    hints=("请以 UTF-8 上传伪装文本，或先用其它编码转存",),
                ) from exc
            return Carrier.of_text(body, label=file.filename or "upload")
        return Carrier.of_text("", label="request-text")
    if file is None:
        raise InvalidInput(
            f"信道类型 {kind} 需要上传载体文件 file",
            hints=("中文信道改用 kind=text 并直接传 text",),
        )
    data = await _read_upload(file, limit)
    return Carrier.of_bytes(data, kind=carrier_kind,
                            media_type=file.content_type, label=file.filename or "")


def _parse_options(raw: str | None) -> dict[str, Any] | None:
    """Options arrive as a JSON string in a query parameter.

    A body field would be cleaner, but this endpoint is already multipart; a
    query string keeps the whole surface callable with `curl -F` alone.
    """
    if not raw:
        return None
    import json

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidInput(f"options 不是合法 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise InvalidInput(f"options 必须是 JSON 对象，收到 {type(parsed).__name__}")
    return parsed


def _media_for(channel: str) -> str:
    if channel == "chinese":
        return "text/plain; charset=utf-8"
    if channel == "gif":
        return "image/gif"
    return "image/png"


def _report_headers(report) -> dict[str, str]:
    """Expose the embed report via headers, since the body is binary.

    HTTP header values are latin-1. Option values are arbitrary user data -- a
    Chinese password, a filename -- so anything non-ASCII is dropped rather than
    allowed to raise ``UnicodeEncodeError`` halfway through building the
    response. The full report stays available in ``X-Report`` (ASCII-safe JSON
    with non-ASCII escaped), which is what a caller should parse anyway.
    """
    headers = {
        "X-Payload-Bytes": str(report.payload_bytes),
        "X-Capacity-Bytes": str(report.capacity_bytes),
        "X-Utilization": f"{report.utilization:.4f}",
    }
    for key, value in report.options_used.items():
        text = str(value)
        if text.isascii() and chr(10) not in text:
            headers[f"X-Option-{key.replace('_', '-')}"] = text
    import json

    headers["X-Report"] = json.dumps(
        {
            "channel": report.channel,
            "payload_bytes": report.payload_bytes,
            "capacity_bytes": report.capacity_bytes,
            "utilization": round(report.utilization, 4),
            "options_used": {k: str(v) for k, v in report.options_used.items()},
        },
        ensure_ascii=True,
    )
    return headers


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def run_server(config: ApiConfig | None = None, *, allow_remote: bool = False) -> None:
    """Start uvicorn. Refuses to listen beyond loopback unless told twice.

    The check lives here rather than in ``main`` so that any caller -- a script,
    a future desktop wrapper -- inherits it.
    """
    import uvicorn

    settings = config or ApiConfig()
    if not _is_loopback(settings.host) and not allow_remote:
        raise InvalidInput(
            f"拒绝在 {settings.host} 上监听：本服务无鉴权，只应绑定回环地址",
            hints=(f"改用 --host {DEFAULT_HOST}，或显式传入 allow_remote=True",),
        )
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port,
                log_level="info")


__all__ = [
    "ApiConfig", "DEFAULT_HOST", "DEFAULT_PORT",
    "MAX_UPLOAD_BYTES", "create_app", "run_server",
]
