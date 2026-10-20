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
import logging
from dataclasses import dataclass, field
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from .channels import Carrier, CarrierKind
from . import covers as covers_pkg
from .core.errors import InvalidInput, StegoError
from .engine import Config, Engine
from .observability import METRICS, event as log_event
from . import jobs

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
    #: OneBot endpoints, one per QQ account, as ``{"<uin>": "ws://...;<token>"}``.
    #: Empty means the local two-window演练. A non-empty mapping puts the very
    #: same terminals on real QQ -- the ``Network`` seam is the only thing that
    #: changes (see docs/QQ_TRANSPORT.md §3).
    onebot: dict[str, str] = field(default_factory=dict)
    #: 单账号模式下的对端 QQ 号。本机只登录一个号时，终端靠它知道该发给谁，
    #: 从而能主动发起握手，而不是干等对方先开口。双账号模式下留空即可。
    peer: str = ""
    #: Shared secret for the *stego* layer. Both sides must agree or the frames
    #: do not authenticate and every picture looks like an ordinary photo.
    psk: str = "pbx1"


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
    # ``CarrierKind.of`` owns the message (legal values, then what arrived), so
    # the CLI, the web API and a plain library call all explain it the same way.
    try:
        return CarrierKind.of(text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


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

    app.state.jobs = {}

    # One terminal pair per process: the conversation feature is a demonstration
    # of the protocol, not a multiuser chat server.
    from .web_terminal import TerminalHub, TextChannelAdapter, wire_terminal_routes

    # Text covert sending rides the Chinese channel, which only exists once a
    # model is loaded. Without one the hub simply gets no adapter and the UI
    # greys that button out instead of failing at send time.
    text_channel = None
    try:
        text_channel = TextChannelAdapter(app.state.engine.channel("chinese"))
    except StegoError:
        text_channel = None

    if settings.onebot:
        hub, transport = _onebot_hub(settings, text_channel)
        app.state.onebot_transport = transport
    else:
        # 本地演练：两个窗口共用一条内存线，一定送达。
        hub = TerminalHub(text_channel=text_channel, psk=settings.psk)
    app.state.terminal_hub = hub
    wire_terminal_routes(app, hub, max_upload_bytes=settings.max_upload_bytes)

    @app.exception_handler(StegoError)
    async def _handle_stego_error(request: Request, exc: StegoError) -> JSONResponse:
        # Every failed request is counted under its stable code, so "what is
        # breaking" is answerable from /metrics without reading logs.
        METRICS.incr("requests_failed", code=exc.code)
        log_event("web", "request_failed", level=logging.WARNING,
                  path=request.url.path, code=exc.code, message=str(exc))
        return _json_error(exc)

    @app.middleware("http")
    async def _observe(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = (time.perf_counter() - start) * 1000
        if request.url.path == "/metrics":
            # Watching must not change what is watched: a scraper polling this
            # endpoint would otherwise dominate its own counters.
            return response
        route = request.scope.get("route")
        # Labelled by route template, not raw path: /jobs/<uuid> would otherwise
        # create one counter per job and make the metric unreadable.
        label = getattr(route, "path", request.url.path)
        METRICS.observe("request_ms", elapsed, route=label)
        METRICS.incr("requests", route=label, status=str(response.status_code))
        log_event("web", "request", path=request.url.path,
                  method=request.method, status=response.status_code,
                  elapsed_ms=round(elapsed, 1))
        return response

    # ------------------------------------------------------------------- the UI
    #
    # Served from the same origin as the API on purpose: a separate dev server
    # would need CORS, and CORS on a service with no auth means any page the
    # user visits could drive it. One origin, no CORS, one process to start.

    ui_dir = Path(__file__).parent / "webui"

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(ui_dir / "index.html", media_type="text/html")

    # ------------------------------------------------------------------- metrics

    @app.get("/metrics")
    def metrics() -> dict[str, Any]:
        """Counters and mean latency since start.  Plain JSON, not Prometheus.

        No Prometheus text format: exposing that would invite a scraper to
        depend on this local-only, unauthenticated service, and nothing about
        the format helps a single-machine operator.  JSON also composes with
        ``/health`` in the same client code.
        """
        return {"ok": True, **METRICS.snapshot()}

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

    # ------------------------------------------------------------------- jobs
    #
    # A batch can run for hours, so it cannot be a request body that the caller
    # waits on. Submit returns an id, progress is polled (and streamed), and the
    # job lives in memory for the life of the process. Deliberately no worker
    # queue or persistence: this is a single-user local service, and a job that
    # needs to survive a restart is what --resume on the CLI is for.

    @app.post("/jobs", status_code=202)
    async def submit_job(
        channel: str = Query(...),
        input_dir: str = Query(..., description="服务端可读的输入文件夹"),
        output_dir: str = Query(..., description="服务端可写的输出文件夹"),
        action: str = Query("embed", pattern="^(embed|extract)$"),
        kind: str = Query("image"),
        key: str = Query(""),
        options: str | None = Query(None),
        payload_file: UploadFile | None = None,
        limit: int | None = Query(None, ge=1),
        workers: int = Query(4, ge=1, le=64),
        on_error: str = Query("skip", pattern="^(skip|abort|retry)$"),
        resume: str | None = Query(None, description="断点 JSONL 路径"),
    ) -> dict[str, Any]:
        """Start a batch over a folder. Returns immediately with a job id.

        Paths are resolved on the *server*, which is why this endpoint takes
        folders rather than 500 uploads: the caller and the service are on the
        same machine by construction (loopback-only), so pointing at a directory
        is both simpler and avoids copying gigabytes through HTTP.
        """
        source = Path(input_dir)
        if not source.is_dir():
            raise InvalidInput(f"输入不是一个文件夹：{source}")
        carrier_kind = _carrier_kind(kind)
        files = jobs.discover(source, carrier_kind, limit=limit)
        if not files:
            raise InvalidInput(f"{source} 下没有 {kind} 类型的文件")
        payload = b""
        if action == "embed":
            if payload_file is None:
                raise InvalidInput("批量嵌入需要上传 payload_file")
            payload = await _read_upload(payload_file, settings.max_upload_bytes)

        suffix = {("embed", "animation"): ".gif", ("embed", "text"): ".txt",
                  ("embed", "image"): ".png"}.get((action, kind), ".bin")
        items = jobs.plan_targets(files, Path(output_dir), suffix=suffix)
        spec = jobs.JobSpec(
            channel_id=channel, items=items, key=key.encode(),
            options=_parse_options(options) or {}, action=action,
            payload=payload, on_error=on_error, max_workers=workers,
            kind=carrier_kind,
        )
        runner = jobs.BatchRunner(app.state.engine, Path(resume) if resume else None)
        job_id = uuid.uuid4().hex[:12]
        status = jobs.JobStatus(job_id=job_id, state="running", total=len(items))
        app.state.jobs[job_id] = _RunningJob(runner=runner, status=status)

        def _work() -> None:
            try:
                final = runner.run(spec, job_id=job_id,
                                   on_progress=lambda s: _remember(app, job_id, s))
                _remember(app, job_id, final)
            except Exception as exc:                       # noqa: BLE001
                _remember_failed(app, job_id, exc)

        threading.Thread(target=_work, daemon=True, name=f"job-{job_id}").start()
        return {"job_id": job_id, "total": len(items), "state": "running"}

    @app.get("/jobs")
    def list_jobs() -> list[dict[str, Any]]:
        return [entry.status_summary() for entry in app.state.jobs.values()]

    @app.get("/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        entry = app.state.jobs.get(job_id)
        if entry is None:
            raise InvalidInput(f"没有这个作业：{job_id}")
        return entry.status_summary(with_results=True)

    @app.post("/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        entry = app.state.jobs.get(job_id)
        if entry is None:
            raise InvalidInput(f"没有这个作业：{job_id}")
        entry.runner.cancel()
        return {"job_id": job_id, "state": "cancelling"}

    @app.get("/jobs/{job_id}/report")
    def job_report(job_id: str, failures_only: bool = False) -> dict[str, Any]:
        entry = app.state.jobs.get(job_id)
        if entry is None:
            raise InvalidInput(f"没有这个作业：{job_id}")
        results = entry.status.results
        if failures_only:
            results = [r for r in results if not r.ok]
        return {"job_id": job_id, "results": [r.as_dict() for r in results]}

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


def _onebot_hub(settings: ApiConfig, text_channel):
    """Put the terminal hub on real QQ.

    Each entry is ``{"<uin>": "ws://host:port/;<token>"}`` -- the token after a
    semicolon, because a dict of two-value tuples cannot be expressed on a
    command line without inventing a config file for two strings.

    The two local terminals keep their names ``alice`` / ``bob``. Every route,
    the history, and the whole web UI address them by that name; renaming them to
    UINs would mean touching all three for no gain. The UIN is where it belongs:
    on the wire, inside each ``OneBotNetwork``.

    Covers are generated here. An automatic handshake reply must travel in a
    picture this side owns (``Terminal._auto_respond``), and in QQ mode there is
    no browser queue to take one from -- without a fallback the peer's request
    would sit in ``pending_request`` forever with nothing on screen to explain it.
    """
    from .terminal.onebot import OneBotNetwork, RoutedNetwork
    from .web_terminal import TerminalHub

    if not settings.onebot:
        raise InvalidInput(
            "没有给任何 OneBot 账号",
            hints=("用 --onebot <uin>=<url> 标出本机登录的 QQ",),
        )
    if len(settings.onebot) > 2:
        raise InvalidInput(
            f"最多两个账号，收到 {len(settings.onebot)} 个：{sorted(settings.onebot)}",
            hints=("本机一个号 + 远端一个号就够；同机双号才需要两个",),
        )

    # 位置决定身份：第一个是 alice，第二个是 bob（如果有）。顺序来自命令行
    # 顺序，所以 --onebot 写了两次的顺序就是谁坐左边。
    #
    # 一个账号也是完整的一侧：本机只登录了一个 QQ 才是常态，对端在另一台
    # 机器上。这时只有一个本地终端，对端地址由 terminal 从入站消息里学到，
    # 或者由 --peer 预置 —— 不需要为了"凑两个"而多挂一个空终端。
    sides = list(settings.onebot.items())
    names = tuple(["alice", "bob"][: len(sides)])
    routes: dict[str, OneBotNetwork] = {}
    uins: dict[str, str] = {}
    for name, (uin, spec) in zip(names, sides):
        url, _, token = str(spec).partition(";")
        routes[name] = OneBotNetwork(url.strip(), str(uin), token=token.strip())
        uins[name] = str(uin)
    network = RoutedNetwork(routes)
    # 底图用包里自带的真实照片，不现场造图。
    # 造出来的噪声图在 QQ 看就是"程序生成的东西"，正是风控会挑的形态；
    # 用户随手会发的那种图才不惹眼。见 covers.py 的说明。
    shared_covers = covers_pkg.load_or_none(2)
    if not shared_covers:
        raise InvalidInput(
            "包里没有自带底图，接 QQ 无法自动回应握手",
            hints=("重新安装本包（covers/*.jpg 应当随包安装）",),
        )
    cover_pool = {name: list(shared_covers) for name in names}
    hub = TerminalHub(text_channel=text_channel, network=network,
                      names=names, fallback_covers=cover_pool, psk=settings.psk)
    #: 让 UI 能显示"alice 其实是哪个 QQ 号"，而不只是位置名。
    #: 只放本机账号 —— 把对端也塞进来会让 name_of() 把对方的 QQ 号翻译成
    #: 内部占位名，聊天记录里就看不到真实号码了。
    hub.uins = uins
    #: 单账号模式的对端号，发送时用它当收件地址。
    hub.peer_uin = str(settings.peer) if settings.peer else ""
    network.start()
    return hub, network


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


@dataclass
class _RunningJob:
    """One in-flight or finished job, kept in ``app.state.jobs``."""

    runner: Any
    status: Any
    lock: Any = field(default_factory=threading.Lock)

    def status_summary(self, *, with_results: bool = False) -> dict[str, Any]:
        with self.lock:
            status = self.status
            data = {
                "job_id": status.job_id,
                "state": status.state,
                "total": status.total,
                "succeeded": status.succeeded,
                "failed": status.failed,
                "skipped": status.skipped,
                "current": status.current,
                "progress": round(status.progress, 4),
                "elapsed": round(status.elapsed, 3),
            }
        if with_results:
            data["results"] = [r.as_dict() for r in status.results if not r.ok]
        return data


def _remember(app: FastAPI, job_id: str, status: Any) -> None:
    entry = app.state.jobs.get(job_id)
    if entry is None:
        return
    with entry.lock:
        entry.status = status


def _remember_failed(app: FastAPI, job_id: str, exc: BaseException) -> None:
    entry = app.state.jobs.get(job_id)
    if entry is None:
        return
    with entry.lock:
        entry.status.state = "failed"
        code, message, reasons, hints = jobs.classify(exc)
        entry.status.results.append(jobs.ItemResult(
            source="<job>", target="", ok=False, error=message,
            error_code=code, reasons=reasons, hints=hints,
        ))
        entry.status.failed += 1


__all__ = [
    "ApiConfig", "DEFAULT_HOST", "DEFAULT_PORT",
    "MAX_UPLOAD_BYTES", "create_app", "run_server",
]
