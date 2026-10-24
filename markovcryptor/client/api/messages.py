"""消息与会话窗口：状态、会话详情、发送、历史。

最核心的一组。/client/state 是界面轮询的主入口。
"""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ...core.errors import StegoError
from ...observability import event
from .. import config
from ..account import Account
from ..services import history as history_service
from ..services import estimate as estimate_service
from ..services import media as media_service
from ..services import picker as picker_service
from ..tasks import TaskManager
from .common import data_url, entry_payload, http_error
from .deps import get_account, get_watchdog, get_window_handle

def _handshake_state(account, conversation) -> dict:
    """What the UI needs to render the handshake gate.

    ``pending`` 是**对方发来、等用户确认**的请求。收方不再自动回复，所以
    这个字段决定了界面上要不要显示「接受 / 忽略」按钮。

    ``fingerprint`` / ``fingerprint_status`` 是 TOFU 的结果：
    ``first``（首次见面）、``same``（一致）、``changed``（**变过，要警告**）。
    首次见面没有可比对的旧值，所以不需要用户核对；变了才有意义。
    """
    terminal = conversation.terminal
    pending_public = terminal.pending_request
    state = {
        "pending": pending_public is not None,
        "fingerprint": "",
        "previous_fingerprint": "",
        "fingerprint_status": "",
    }
    if pending_public is not None:
        from ...core.identity import fingerprint as _fp
        state["fingerprint"] = _fp(pending_public)
        known = account.peers.known(conversation.peer) if account.peers else None
        if known:
            state["previous_fingerprint"] = known
            state["fingerprint_status"] = (
                "same" if known == state["fingerprint"] else "changed")
        else:
            state["fingerprint_status"] = "first"
    return state


router = APIRouter()

#: 一条 QQ 消息最多几张图。QQ 客户端通常允许 10 张，这里留一张余量 ——
#: 宁可少发一张，也不要让整条消息被拒。
IMAGE_LIMIT = 9

#: 文本信道的单条上限（字符数）。
#:
#: 文本隐写会膨胀：实测 800 汉字 → 约 1540 字符（1.92×），短文本更高。
#: QQ 单条文本有上限，超过会被截断或拒收，而截断会**直接毁掉密文**。
#: 2000 字符给 800 汉字留了余量，又不至于顶到 QQ 的边界。
TEXT_CHANNEL_LIMIT = 2000


def _check_covert_length(text: str, channel: str) -> None:
    """文本信道的长度闸门。

    只对文本信道生效：图片信道的容量由「9 张图的实际块数」决定，跟字符数
    没有直接关系（9 张 512 能装约 3700 汉字）。在这里用字符数卡图片信道
    反而会误伤。
    """
    if channel != "text":
        return
    if len(text) > TEXT_CHANNEL_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=(f"文本隐写最多 {TEXT_CHANNEL_LIMIT} 字符，"
                    f"当前 {len(text)} 字符。"
                    f"想发更长内容请改用图片信道（一条消息最多 {IMAGE_LIMIT} 张图）。"),
        )



@router.get("/client/state")
def state(request: Request, account: Account = Depends(get_account)) -> JSONResponse:
    """Everything the shell needs to draw itself."""
    return JSONResponse({
        "ok": True,
        "uin": account.uin,
        "ready": account.ready,
        "unread_total": account.conversations.unread_total(),
        "queue": {**account.queue.stats(), "items": media_service.queue_items(account)},
        "models": (account.models.status() if account.models is not None
                   else {"total": 0, "loaded": 0, "folder": "",
                         "names": [], "loading": False}),
        "text_channel": account.text_channel_usable,
        # 只有接管到原生窗口时最小化才有意义（Firefox 标签页跑不了）。
        "native_window": bool(getattr(request.app.state, "window_handle", None)),
        # 队列装得下握手吗？界面靠它在未握手时决定能否点"发起握手"。
        "handshake_capacity": account.covert_capacity(need_handshake=True),
        # 本机身份：界面顶部要显示真实昵称和头像，而不是"本机"两个字。
        "self": {
            "uin": account.uin,
            "name": account.directory.self_name,
            "nickname": account.directory.self_nickname,
            "avatar": account.directory.self_avatar,
            "online": account.online,
        },
        "friends": [
            {"uin": friend.uin, "name": friend.display_name,
             "nickname": friend.nickname, "avatar": friend.avatar}
            for friend in account.directory.ordered()
        ],
        "conversations": [
            {
                "peer": conversation.peer,
                "name": conversation.name,
                "unread": conversation.unread,
                "established": conversation.established,
                "state_label": conversation.state_label,
                # 对方发来、等用户确认的握手请求。界面靠它显示「接受/忽略」，
                # 并在最近列表上打一个点。
                "pending": conversation.terminal.pending_request is not None,
                "last_at": conversation.last_at,
                # 最近列表也要头像。好友列表里有，但会话的对端不一定是
                # 好友（陌生人也能发消息），所以这里按号码现拼一个兜底。
                "avatar": media_service.peer_avatar(account, conversation.peer),
            }
            for conversation in account.conversations.ordered()
        ],
        # 底部状态栏：队列/握手/收发的实时计数。
        "tasks": request.app.state.tasks.stats(),
        # 两个标签页的总数，界面直接显示"最近 (12) / 好友 (29)"。
        "counts": {
            "conversations": len(account.conversations),
            "friends": len(account.directory),
        },
    })


@router.get("/client/conversation/{peer}")
def conversation_detail(peer: str, history: int = 0, account: Account = Depends(get_account)) -> JSONResponse:
    """Open one conversation, optionally pulling recent QQ history first.

    History is fetched on open, not at startup: prefetching thirty friends
    would be slow and is more data than the user asked for.
    """
    conversation = account.conversations.ensure(peer)
    if history > 0:
        history_service.merge_history(account, conversation, peer, history)
    return JSONResponse({
        "ok": True,
        "peer": conversation.peer,
        "name": account.directory.name_of(peer),
        "established": conversation.established,
        "state_label": conversation.state_label,
        "handshake": _handshake_state(account, conversation),
        "entries": [entry_payload(e) for e in conversation.snapshot()],
    })


@router.post("/client/conversation/{peer}/open")
def open_conversation(peer: str, account: Account = Depends(get_account)) -> JSONResponse:
    """Mark the conversation as being read; clears its red dot."""
    conversation = account.conversations.ensure(peer)
    conversation.mark_open()
    return JSONResponse({"ok": True, "unread": conversation.unread})


@router.post("/client/conversation/{peer}/close")
def close_conversation(peer: str, account: Account = Depends(get_account)) -> JSONResponse:
    conversation = account.conversations.get(peer)
    if conversation is not None:
        conversation.mark_open(False)
    return JSONResponse({"ok": True})


@router.post("/client/send")
async def send(payload: dict, request: Request,
               account: Account = Depends(get_account)) -> JSONResponse:
    """Send plain text, pictures, or covert text, depending on the body.

    One endpoint with a ``mode`` rather than three endpoints, because the UI
    has one composer and the difference is a toggle, not a separate screen.

    **校验同步做，编码丢后台**：

    Markov 编码 0.6–0.75 秒、1600² 图嵌入 3.3 秒。以前这些都跑在事件循环里，
    发一张大图就让所有轮询和其他好友的消息停摆。现在：

    * 参数校验、长度检查立即返回（422 立刻能报，不用等 3 秒）
    * 真正的编码/嵌图提交到 ``TaskManager``，按会话串行、全局池并发
    * 前端拿 ``task_id`` 轮询进度，期间禁用这一条会话的发送按钮

    同一会话已有在途任务时返回 409 —— 消息顺序不能靠猜。
    """
    peer = str(payload.get("peer") or "").strip()
    if not peer:
        raise HTTPException(status_code=422, detail="缺少对端 QQ 号")
    mode = str(payload.get("mode") or "text")
    text_body = str(payload.get("text") or "")

    # 校验先做，放到 try 外面：HTTPException 是 Exception 的子类，
    # 写在 try 里会被下面的兜底 except 抓走，422 变成 502。
    if mode not in ("text", "covert", "handshake"):
        raise HTTPException(status_code=422, detail=f"未知模式：{mode}")

    # 明文可以带图：QQ 允许只发图、也允许图文同一条。
    images = payload.get("images") or []
    raw_images: tuple[bytes, ...] = ()
    if images:
        if not isinstance(images, list):
            raise HTTPException(status_code=422, detail="images 必须是数组")
        if len(images) > IMAGE_LIMIT:
            raise HTTPException(
                status_code=422,
                detail=f"一条消息最多 {IMAGE_LIMIT} 张图片，你选了 {len(images)} 张")
        raw_images = tuple(media_service.decode_data_url(str(item)) for item in images)
    channel = str(payload.get("channel") or "image")
    if channel not in ("image", "text"):
        raise HTTPException(status_code=422, detail=f"未知信道：{channel}")

    if mode == "text" and not text_body.strip() and not raw_images:
        raise HTTPException(status_code=422, detail="消息是空的")
    if mode == "covert":
        _check_covert_length(text_body, channel)

    manager: TaskManager = request.app.state.tasks
    active = manager.active_for(peer)
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail=f"这个会话正在发送（{active.note or active.state}），请稍候",
        )

    # 握手是轻量控制报文：不值得排进任务队列，直接做。
    if mode == "handshake":
        try:
            account.start_handshake(peer)
        except (StegoError, ValueError) as exc:
            event("client", "send_rejected", level=logging.INFO,
                  peer=peer, mode=mode, error=exc)
            raise http_error(exc) from None
        conversation = account.conversations.ensure(peer)
        return JSONResponse({
            "ok": True,
            "task_id": None,
            "state_label": conversation.state_label,
            "entries": [entry_payload(e) for e in conversation.snapshot()[-3:]],
        })

    def work(task) -> dict:
        """在后台线程里跑。这里可以慢，但不能碰 HTTP 对象。

        异常在这里**不能**直接抛给 FastAPI —— 已经离开请求上下文了。
        所以把 ``StegoError.http_status`` 抄进 ``task.error_status``，
        前端和测试才能分清「图太小(413)」「格式不支持(415)」「我们崩了(502)」。
        """
        if task.cancelled:
            return {}
        task.note = "正在编码…"
        task.progress = 0.15
        try:
            if mode == "text":
                if raw_images:
                    account.send_pictures(peer, raw_images, text=text_body or None)
                else:
                    account.send_text(peer, text_body)
            elif channel == "text":
                account.send_covert_text(peer, text_body)
            else:
                account.send_covert(peer, text_body)
        except (StegoError, ValueError) as exc:
            task.error_status = http_error(exc).status_code
            event("client", "send_rejected", level=logging.INFO,
                  peer=peer, mode=mode, error=exc)
            raise
        if task.cancelled:
            return {}
        task.progress = 0.9
        task.note = "正在发送…"
        conversation = account.conversations.ensure(peer)
        return {
            "state_label": conversation.state_label,
            "entries": [entry_payload(e) for e in conversation.snapshot()[-3:]],
        }

    # 文本信道走单线程串行队列（Markov 编解码不释放 GIL，多线程没意义）；
    # 图片信道走线程池（numpy/PIL 会释放 GIL，真并发）。
    kind = "text" if (mode == "covert" and channel == "text") else "image"
    task = manager.submit(peer, kind, work)
    if task is None:                            # 竞态：刚刚另一个请求抢到了
        raise HTTPException(status_code=409, detail="这个会话正在发送，请稍候")

    return JSONResponse({
        "ok": True,
        "task_id": task.id,
        "peer": peer,
        "state": task.state,
    })


@router.get("/client/history/{peer}")
def history(peer: str, count: int = 20, account: Account = Depends(get_account)) -> JSONResponse:
    """Fetch recent messages from QQ without merging into the timeline."""
    try:
        parsed = account.load_history(peer, count=count)
    except (StegoError, ValueError) as exc:
        # 非法 QQ 号是用户输入问题（409/422），不是服务器故障。
        event("client", "history_rejected", level=logging.INFO,
              peer=peer, error=exc)
        raise http_error(exc) from None
    except Exception as exc:                    # noqa: BLE001
        event("client", "history_failed", level=logging.WARNING,
              peer=peer, error=exc)
        raise http_error(exc, fallback=502) from None
    return JSONResponse({
        "ok": True,
        "messages": [
            {"text": item.text,
             "unsupported": list(item.unsupported),
             "images": [ref.summary or "" for ref in item.images]}
            for item in parsed
        ],
    })


# --------------------------------------------------------------- 任务

@router.get("/client/task/{task_id}")
def task_state(task_id: str, request: Request) -> JSONResponse:
    """轮询一个发送任务的进度。

    没有 SSE 不是偷懒：轮询在这里更简单也更可靠（连接可能被浏览器掐掉，
    而任务还在后台跑）。
    """
    manager: TaskManager = request.app.state.tasks
    task = manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    payload = task.as_dict()
    # 顺带把整个队列的统计发回去，省一次请求。
    payload["stats"] = manager.stats()
    return JSONResponse(payload)


@router.post("/client/task/{task_id}/cancel")
def task_cancel(task_id: str, request: Request) -> JSONResponse:
    """请求取消。

    没有抢占式取消：Python 线程无法安全中断。任务会在下一个检查点停下。
    已编码出来但还没发出去的图**归还队列**，不会白白消耗用户的图库。
    """
    manager: TaskManager = request.app.state.tasks
    task = manager.cancel(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    event("client", "send_cancelled", level=logging.INFO,
          task=task_id, peer=task.peer)
    return JSONResponse({"ok": True, "task": task.as_dict()})


@router.post("/client/estimate")
def estimate_message(payload: dict, request: Request,
                     account: Account = Depends(get_account)) -> JSONResponse:
    """这条消息会占几张图、能装多少字。

    前端在输入时（300ms 防抖）调用，用来显示「将会发送 4/9 张图片」并
    在装不下时提前禁用发送按钮 —— 而不是让用户点了才收到 413。

    逐张按待选顺序真实计算，不用平均容量：队列里可能混着 256×256 和
    1600×1600，平均值毫无意义。
    """
    text = str(payload.get("text") or "")
    channel = str(payload.get("channel") or "image")
    if channel not in ("image", "text"):
        raise HTTPException(status_code=422, detail=f"未知信道：{channel}")
    result = estimate_service.estimate(account, text=text, channel=channel)
    return JSONResponse(result)


@router.post("/client/handshake/{peer}/accept")
def handshake_accept(peer: str, account: Account = Depends(get_account)
                     ) -> JSONResponse:
    """用户点了「接受」：回复对方挂起的握手请求。

    收方**不会自动回复** —— 自动回复在 QQ 侧看就是机器行为，用户也失去
    核对指纹的机会，而且客户端挂着时任何人都能自动把连接建立起来。
    """
    conversation = account.conversations.get(peer)
    if conversation is None:
        raise HTTPException(status_code=404, detail="没有这个会话")
    if conversation.terminal.pending_request is None:
        raise HTTPException(status_code=409, detail="没有待确认的握手请求")

    sent = account.accept_handshake(peer)
    if not sent:
        raise HTTPException(
            status_code=409,
            detail=("队列里没有能装下握手的图片。"
                    "换一张大一点的图，或先在设置里指定图片文件夹。"),
        )
    return JSONResponse({
        "ok": True,
        "state_label": conversation.state_label,
        "entries": [entry_payload(e) for e in conversation.snapshot()[-3:]],
    })


@router.post("/client/handshake/{peer}/ignore")
def handshake_ignore(peer: str, account: Account = Depends(get_account)
                     ) -> JSONResponse:
    """用户点了「忽略」：丢掉这个请求，不建立会话。

    不记录指纹 —— 忽略的人不该被当成"已经信任过"。
    """
    conversation = account.conversations.get(peer)
    if conversation is None:
        raise HTTPException(status_code=404, detail="没有这个会话")
    conversation.terminal.pending_request = None
    event("client", "handshake_ignored", level=logging.INFO, peer=peer)
    return JSONResponse({"ok": True, "state_label": conversation.state_label})
