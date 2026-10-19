"""QQ 信道实测工具：验证图片能否原样往返（P0）。

这是接 QQ 之前唯一必须先做的一件事：**像素域隐写依赖"发出去的图片
逐字节到达对端"**。QQ 服务端若重编码或降采样，DCT-QIM 载荷就全废了。

实测结论（2026-10，SnowLuma v1.14.22 + QQ 9.9.36）：

    原图 120332B md5=83f57a… → 取回 120332B md5=83f57a…  完全一致
    120²/256²/1024² 三图同发：均逐字节一致，同一条消息内，顺序不变

用法：

    # 先起 SnowLuma（每个 QQ 一份 onebot_<uin>.json，端口各不同）
    python tools/qq_probe.py --a-uin 111 --a-ws 9711 --a-token xxx \\
                             --b-uin 222 --b-ws 9721 --b-token yyy

    # 只想测单图往返
    python tools/qq_probe.py ... --only single
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import sys
import urllib.request

import numpy as np
from PIL import Image


def _png(size: int, seed: int) -> bytes:
    """噪声图：纯色会被图片信道判为"太光滑"，这里只要可解码的随机像素。"""
    array = np.random.default_rng(seed).integers(0, 256, (size, size, 3), dtype=np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(array, "RGB").save(buffer, "PNG")
    return buffer.getvalue()


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


async def _call(ws, action: str, params: dict, echo: str, timeout: float = 60):
    """发一个 OneBot 动作并等到同 echo 的回包。"""
    await ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        message = json.loads(raw)
        if message.get("echo") == echo:
            return message


async def _collect_images(ws, wanted: int, timeout: float) -> list[dict]:
    """等对端推来的图片段，收满 ``wanted`` 个或超时。"""
    found: list[dict] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while len(found) < wanted:
        left = deadline - asyncio.get_running_loop().time()
        if left <= 0:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=left)
        except asyncio.TimeoutError:
            break
        message = json.loads(raw)
        if message.get("post_type") != "message":
            continue
        for segment in message.get("message") or []:
            if segment.get("type") == "image":
                found.append(segment["data"])
    return found


async def _round_trip(args, specs: list[tuple[str, int]]) -> bool:
    """发 ``specs`` 若干张图，逐个比对取回的字节。返回是否全部一致。"""
    import websockets

    sent = [(name, _png(size, index + 1), size) for index, (name, size) in enumerate(specs)]
    print(f"\n=== 往返测试：{len(sent)} 张 ===")
    for name, blob, size in sent:
        print(f"  发出 {name:6s} {size:4d}² {len(blob):7d}B md5={hashlib.md5(blob).hexdigest()[:12]}")

    listener = websockets.connect(
        args.b_ws, extra_headers={"Authorization": f"Bearer {args.b_token}"})
    async with listener as b:
        await asyncio.sleep(1.5)          # 让对端先订阅上，免得推送漏在连接建立前
        collect = asyncio.create_task(_collect_images(b, len(sent), timeout=args.timeout))

        async with websockets.connect(
                args.a_ws, extra_headers={"Authorization": f"Bearer {args.a_token}"}) as a:
            message = [{"type": "image",
                        "data": {"file": "base64://" + base64.b64encode(blob).decode()}}
                       for _name, blob, _size in sent]
            reply = await _call(a, "send_private_msg", {
                "user_id": args.b_uin, "message": message}, echo="probe-send")
            print(f"  发送：{reply.get('status')} {reply.get('data')}")

        received = await collect

    print(f"\n  收到 {len(received)} 段（发出 {len(sent)}）")
    if len(received) != len(sent):
        print("  ✗ 段数不符：可能被合并、丢弃，或对端没在线")
        return False

    ok = True
    for index, (data, (name, blob, size)) in enumerate(zip(received, sent)):
        try:
            got = _fetch(data["url"])
        except Exception as error:                       # noqa: BLE001 - 探测工具
            print(f"  #{index} {name}: 下载失败 {type(error).__name__} {error}")
            ok = False
            continue
        image = Image.open(io.BytesIO(got))
        same = got == blob
        print(f"  #{index} {name:6s} {image.format} {image.size} {len(got):7d}B "
              f"md5={hashlib.md5(got).hexdigest()[:12]}  "
              f"{'✓ 逐字节一致' if same else '✗ 被改动'}")
        ok = ok and same and image.size == (size, size)
    return ok


def _report(results: dict[str, bool]) -> int:
    print("\n" + "=" * 62)
    for name, passed in results.items():
        print(f"  {'✓' if passed else '✗'} {name}")
    print("=" * 62)
    if all(results.values()):
        print("""
结论：QQ 原样往返图片，像素域隐写（DCT-QIM）在 QQ 上可用。
      下一步可以实现 OneBotNetwork，把 Terminal 接到 QQ。
""")
        return 0
    print("""
结论：图片被 QQ 改动了，像素域隐写不能直接跑在 QQ 上。
      备选：改用中文信道（纯文本，不依赖图片字节）。
""")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--a-uin", required=True, help="发送方 QQ 号（对方的 OneBot 端口在 b-*）")
    parser.add_argument("--a-ws", required=True, help="发送方 OneBot WS 地址，如 ws://127.0.0.1:9711/")
    parser.add_argument("--a-token", required=True, help="发送方 OneBot accessToken")
    parser.add_argument("--b-uin", required=True, type=int, help="接收方 QQ 号")
    parser.add_argument("--b-ws", required=True, help="接收方 OneBot WS 地址")
    parser.add_argument("--b-token", required=True, help="接收方 OneBot accessToken")
    parser.add_argument("--only", choices=("single", "multi", "big", "all"), default="all",
                        help="只跑某一项；默认全跑")
    parser.add_argument("--timeout", type=float, default=60.0, help="等对端推送的秒数")
    args = parser.parse_args(argv)

    results: dict[str, bool] = {}
    try:
        if args.only in ("all", "single"):
            results["单图 200² 原样往返"] = asyncio.run(_round_trip(args, [("单图", 200)]))
        if args.only in ("all", "multi"):
            results["三图同发：独立、顺序、字节一致"] = asyncio.run(
                _round_trip(args, [("小图", 120), ("中图", 256), ("大图", 512)]))
        if args.only in ("all", "big"):
            results["1024² 大图不被降采样"] = asyncio.run(
                _round_trip(args, [("大图", 1024)]))
    except Exception as error:                            # noqa: BLE001 - 探测工具
        print(f"\n探测中断：{type(error).__name__}: {error}", file=sys.stderr)
        if not results:
            return 2
    return _report(results)


if __name__ == "__main__":
    raise SystemExit(main())
