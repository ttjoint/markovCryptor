"""Real-browser smoke test for the web UI.

Why this exists separately from ``tests/test_webui.py``: the unit tests prove
the page is served and that its schema handling is sound, but only a browser can
prove the JavaScript actually runs -- a typo in a selector or a rejected promise
leaves a blank tab that no structural test can see.

Chrome/Edge is driven over the DevTools protocol with a raw WebSocket, so this
needs no extra package (no selenium, no playwright). It also never touches a
browser the user already has open: a dedicated ``--user-data-dir`` and a
dedicated debugging port are used, and cleanup matches on that port only.

    python -m tools.ui_smoke
    python -m tools.ui_smoke --keep     # leave the browser open to inspect
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def find_browser() -> str | None:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


class Cdp:
    """Minimal DevTools client: just enough to evaluate JavaScript."""

    def __init__(self, ws_url: str) -> None:
        import websockets.sync.client as ws          # noqa: PLC0415

        self._ws = ws.connect(ws_url, max_size=None)
        self._id = 0

    def call(self, method: str, **params) -> dict:
        self._id += 1
        self._ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            message = json.loads(self._ws.recv())
            if message.get("id") == self._id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    def eval(self, expression: str):
        result = self.call("Runtime.evaluate", expression=expression,
                           awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"].get("text", "JS 异常"))
        return result["result"].get("value")

    def wait_for(self, expression: str, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.eval(expression):
                    return True
            except RuntimeError:
                pass
            time.sleep(0.1)
        return False

    def close(self) -> None:
        self._ws.close()


def _ws_url(debug_port: int) -> str:
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/list") as r:
                targets = json.load(r)
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return target["webSocketDebuggerUrl"]
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError("浏览器没有在超时内暴露调试端口")


def _kill(process: subprocess.Popen, profile: Path, debug_port: int) -> None:
    """Stop only the browser this script started.

    Matched on the profile directory and debug port -- never by image name, so a
    user's own browser windows are untouched.
    """
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    if sys.platform != "win32":
        return
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' or Name='msedge.exe'\" | "
        f"Where-Object {{ $_.CommandLine -match '--remote-debugging-port={debug_port}' "
        f"-or $_.CommandLine -match '{str(profile).replace(chr(92), chr(92) * 2)}' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", script],
                   capture_output=True)


def check(condition: bool, label: str, failures: list[str]) -> None:
    print(("  ok   " if condition else "  FAIL ") + label)
    if not condition:
        failures.append(label)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用真实浏览器验证前端")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--show", dest="headless", action="store_false",
                        help="显示浏览器窗口")
    parser.add_argument("--keep", action="store_true", help="结束后不关闭浏览器")
    parser.add_argument("--screenshot", metavar="PATH",
                        help="把终端页截一张 PNG 存到指定路径，用于人工看布局")
    args = parser.parse_args(argv)

    browser = find_browser()
    if browser is None:
        print("没有找到 Chrome/Edge，跳过浏览器冒烟。", file=sys.stderr)
        return 2

    # Import here so the tool still fails clearly if the deps are missing.
    import uvicorn

    from markovcryptor.engine import Engine
    from markovcryptor.model import MarkovModel
    from markovcryptor.web import create_app

    sys.path.insert(0, str(ROOT / "tests"))
    import fixtures

    api_port, debug_port = free_port(), free_port()
    profile = Path(tempfile.mkdtemp(prefix="mkx1-ui-"))

    model = MarkovModel.train(fixtures.corpus_text(), vocabulary_size=512)
    app = create_app(engine=Engine(model=model))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port,
                                           log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        print("API 未能启动。", file=sys.stderr)
        return 2

    command = [
        browser, f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--disable-gpu",
    ]
    if args.headless:
        command.append("--headless=new")
    command.append(f"http://127.0.0.1:{api_port}/")

    process = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    failures: list[str] = []
    try:
        client = Cdp(_ws_url(debug_port))
        try:
            # 页面里的任何 JS 异常都记下来：前端的失败往往只表现为"没反应"，
            # 没有这条通道就只能猜。
            client.call("Runtime.enable")
            client.eval("window.__errs = [];"
                        "window.addEventListener('error', e => window.__errs.push("
                        "String(e.message) + ' @' + (e.filename || '') + ':' + e.lineno));"
                        "window.addEventListener('unhandledrejection', e => "
                        "window.__errs.push('reject: ' + String(e.reason)));")
            print("浏览器冒烟：")
            check(client.wait_for("!!document.querySelector('#channel option')"),
                  "页面加载并填充了信道下拉框", failures)

            channels = client.eval(
                "[...document.querySelectorAll('#channel option')].map(o => o.value)"
            )
            check(channels == ["image", "gif", "chinese"],
                  f"三个信道都在（实际 {channels}）", failures)

            # The form is generated from the schema, so the image channel must
            # have produced its four inputs without any front-end knowledge.
            client.eval("(() => { const s = document.querySelector('#channel');"
                        " s.value = 'image'; s.onchange(); })()")
            inputs = client.eval(
                "[...document.querySelectorAll('#options [data-option-name]')]"
                ".map(i => i.dataset.optionName).sort()"
            )
            check(inputs == ["include_jpeg", "jpeg_quality", "step", "upscale"],
                  f"图片信道的表单由 schema 生成（实际 {inputs}）", failures)

            client.eval("(() => { const s = document.querySelector('#channel');"
                        " s.value = 'gif'; s.onchange(); })()")
            gif_inputs = client.eval(
                "[...document.querySelectorAll('#options [data-option-name]')]"
                ".map(i => i.dataset.optionName)"
            )
            check("step" in gif_inputs and "frame_index" in gif_inputs,
                  f"GIF 信道表单正确（实际 {gif_inputs}）", failures)

            collected = client.eval(
                "(() => { const s = document.querySelector('#channel');"
                " s.value = 'image'; s.onchange();"
                " const step = document.querySelector('[data-option-name=step]');"
                " step.value = '14';"
                " const up = document.querySelector('[data-option-name=upscale]');"
                " up.checked = false;"
                " return JSON.stringify([...document.querySelectorAll("
                "'#options [data-option-name]')].reduce((acc, i) => {"
                "  if (i.dataset.optionKind === 'boolean') acc[i.dataset.optionName] = i.checked;"
                "  else if (i.value !== '') acc[i.dataset.optionName] = i.dataset.optionKind === 'integer'"
                "    ? parseInt(i.value, 10) : parseFloat(i.value);"
                "  return acc; }, {})); })()"
            )
            parsed = json.loads(collected)
            check(parsed.get("step") == 14 and parsed.get("upscale") is False,
                  f"表单取值正确（实际 {parsed}）", failures)

            # Exercise the real embed path from the page's own fetch code.
            payload_b64 = base64.b64encode(b"ui-smoke-payload").decode()
            cover_b64 = base64.b64encode((Path(fixtures.FIXTURES) / "photo.png")
                                         .read_bytes()).decode()
            status = client.eval(f"""(async () => {{
                const toFile = (b64, name, type) => {{
                  const bin = atob(b64); const bytes = new Uint8Array(bin.length);
                  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
                  return new File([bytes], name, {{type}});
                }};
                const form = new FormData();
                form.append('payload_file', toFile('{payload_b64}', 'p.bin', 'application/octet-stream'));
                form.append('cover_file', toFile('{cover_b64}', 'c.png', 'image/png'));
                const r = await fetch('/embed?channel=image&kind=image&key=k', {{method: 'POST', body: form}});
                return r.status;
            }})()""")
            check(status == 200, f"页面同源 POST /embed 成功（实际 {status}）", failures)

            client.eval("document.querySelector('[data-tab=batch]').click()")
            check(client.eval("!document.querySelector('#tab-batch').hidden"),
                  "批处理页签可切换", failures)

            client.eval("document.querySelector('[data-tab=channels]').click()")
            detail = client.eval("document.querySelector('#channel-detail').textContent")
            check("DCT" in detail or "隐写" in detail,
                  "信道详情页有内容", failures)

            # --- 终端页：两个客户端 × 双窗 + 中间网络连线 ---
            client.eval("document.querySelector('[data-tab=terminal]').click()")
            check(client.eval("!document.querySelector('#tab-terminal').hidden"),
                  "终端页签可切换", failures)
            for me in ("a", "b"):
                check(client.eval(f"document.querySelector('#{me}-plain') !== null && "
                                  f"document.querySelector('#{me}-covert') !== null"),
                      f"{me} 侧明文窗与密文窗都存在", failures)
                check(not client.eval(f"document.querySelector('#{me}-plain-text').disabled"),
                      f"{me} 明文输入框随时可用", failures)
                check(client.eval(f"document.querySelector('#{me}-covert-text').disabled"),
                      f"{me} 未握手时密文输入框不可用", failures)
            check(client.eval("document.querySelector('#wire') !== null"),
                  "两客户端之间有网络连线", failures)

            def queue_push(me, name):
                client.eval(f"""(() => {{
                    const bin = atob('{cover_b64}');
                    const bytes = new Uint8Array(bin.length);
                    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
                    const file = new File([bytes], '{name}', {{type: 'image/png'}});
                    const input = document.querySelector('#{me}-files');
                    const dt = new DataTransfer(); dt.items.add(file);
                    input.files = dt.files;
                    input.dispatchEvent(new Event('change'));
                }})()""")
                time.sleep(0.4)

            # 两侧各自加图：队列必须互不影响。
            queue_push("a", "a1.png")
            queue_push("a", "a2.png")
            queue_push("b", "b1.png")
            check(client.eval("document.querySelectorAll('#a-queue .qitem').length") == 2,
                  "alice 队列有 2 张", failures)
            check(client.eval("document.querySelectorAll('#b-queue .qitem').length") == 1,
                  "bob 队列有 1 张（互不影响）", failures)

            # 明文附件发送前应有预览。
            client.eval(f"""(() => {{
                const bin = atob('{cover_b64}');
                const bytes = new Uint8Array(bin.length);
                for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
                const file = new File([bytes], 'attach.png', {{type: 'image/png'}});
                const input = document.querySelector('#a-plain-files');
                const dt = new DataTransfer(); dt.items.add(file);
                input.files = dt.files;
                input.dispatchEvent(new Event('change'));
            }})()""")
            time.sleep(0.5)
            check(client.eval("document.querySelectorAll('#a-plain-preview .thumb2').length") == 1,
                  "明文附件有预览缩略图", failures)

            # 明文发送：不消耗图片队列，两侧窗口都应显示这句话。
            client.eval("""(() => {
                document.querySelector('#a-plain-text').value = '普通消息';
                document.querySelector('#a-plain-send').click();
            })()""")
            time.sleep(2.0)
            check(client.eval("document.querySelectorAll('#a-queue .qitem').length") == 2,
                  "明文发送不消耗图片队列", failures)
            print("  JS 错误：", client.eval("JSON.stringify(window.__errs || [])"))
            check(client.eval("document.querySelector('#a-plain').textContent")
                  .find("普通消息") >= 0, "发送方明文窗看到自己发的消息", failures)
            check(client.eval("document.querySelector('#b-plain').textContent")
                  .find("普通消息") >= 0, "接收方明文窗看到对方发的消息", failures)

            # alice 握手：从她的队首取一张；bob 的队列不受影响。
            client.eval("document.querySelector('#a-handshake').click()")
            time.sleep(4.0)
            check(client.eval("document.querySelectorAll('#a-queue .qitem').length") == 1,
                  "握手从 alice 队首取走一张", failures)
            check(client.eval("document.querySelectorAll('#b-queue .qitem').length") == 1,
                  "握手不动 bob 的队列", failures)
            check(not client.eval("document.querySelector('#a-covert-text').disabled"),
                  "alice 握手后密文输入框解锁", failures)
            check(not client.eval("document.querySelector('#b-covert-text').disabled"),
                  "bob 侧也被动解锁", failures)
            check(client.eval("document.querySelector('#a-covert').textContent")
                  .find("密钥协商") >= 0, "alice 密文窗显示协商完成提示", failures)
            check(client.eval("document.querySelector('#b-covert').textContent")
                  .find("密钥协商") >= 0, "bob 密文窗显示协商完成提示", failures)

            # 文本混淆：不耗图，两侧密文窗都看得到。
            client.eval("""(() => {
                document.querySelector('#a-covert-text').value = '文字机密';
                document.querySelector('#a-send-text').click();
            })()""")
            time.sleep(3.5)
            check(client.eval("document.querySelectorAll('#a-queue .qitem').length") == 1,
                  "文本混淆不消耗图片", failures)
            check(client.eval("document.querySelector('#a-covert').textContent")
                  .find("文字机密") >= 0, "发送方密文窗看到自己发的密文", failures)
            check(client.eval("document.querySelector('#b-covert').textContent")
                  .find("文字机密") >= 0, "接收方密文窗收到密文", failures)

            # 图片混淆：短密文只需一张，队列里的另一张必须留着。
            client.eval("""(() => {
                document.querySelector('#a-covert-text').value = '图片机密';
                document.querySelector('#a-send-image').click();
            })()""")
            time.sleep(4.0)
            check(client.eval("document.querySelectorAll('#a-queue .qitem').length") == 0,
                  "图片混淆消耗了队列里最后一张", failures)
            check(client.eval("document.querySelectorAll('#a-sent .qitem').length") == 2,
                  "只消耗够用的张数（不是把整队全发出去）", failures)

            # 自己发的图要显示成图，而不是"[图片]"占位。
            check(client.eval(
                "[...document.querySelectorAll('#a-plain img.thumb')]"
                ".every(i => i.naturalWidth > 0)"),
                  "自己发的图片在聊天框里正常显示", failures)
            check("张图片］" not in client.eval(
                      "document.querySelector('#a-plain').textContent"),
                  "自己发的不再退化成占位文字", failures)

            # 密文发送的明文窗只显示公开内容，不夹带要隐藏的那句。
            check("图片机密" not in client.eval(
                      "document.querySelector('#a-plain').textContent"),
                  "密文不出现在明文窗", failures)
            check("图片机密" not in client.eval(
                      "document.querySelector('#b-plain').textContent"),
                  "对方的明文窗也不夹带密文", failures)

            # 每条消息带时刻。
            check(bool(client.eval(
                "(() => { const w = document.querySelector('#a-plain .who');"
                " return !!w && /[0-9]{1,2}:[0-9]{2}/.test(w.textContent); })()")),
                  "消息带时间显示", failures)
            check(client.eval("document.querySelector('#a-send-image').disabled"),
                  "队列空后图片混淆按钮重新禁用", failures)
            check(client.eval("document.querySelector('#a-q-status').textContent")
                  .find("不可用") >= 0, "队列空时明确提示隐写不可用", failures)
            check(client.eval("document.querySelector('#b-covert').textContent")
                  .find("图片机密") >= 0, "接收方收到图片密文", failures)
            check(client.eval("document.querySelectorAll('#a-sent .qitem').length") == 2,
                  "alice 已发送栏归档 2 张（握手 + 密文）", failures)

            if args.screenshot:
                # 大屏用户的实际窗口很宽，截图前把视口拉到终端页需要的尺寸。
                client.call("Emulation.setDeviceMetricsOverride",
                            width=1700, height=1400, deviceScaleFactor=1,
                            mobile=False)
                time.sleep(0.6)
                shot = client.call("Page.captureScreenshot", format="png")
                Path(args.screenshot).write_bytes(
                    base64.b64decode(shot["data"]))
                print(f"  截图已保存：{args.screenshot}")
        finally:
            client.close()
    finally:
        if args.keep:
            print(f"\n浏览器仍在运行（调试端口 {debug_port}，用户目录 {profile}）")
        else:
            _kill(process, profile, debug_port)
        server.should_exit = True
        thread.join(timeout=10)

    if failures:
        print(f"\n{len(failures)} 项失败。", file=sys.stderr)
        return 1
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
