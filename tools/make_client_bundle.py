"""Build a zero-install client bundle around a bundled Python runtime.

The point is a machine with **no Python and no Node**: the user unzips, logs into
QQ, double-clicks. Everything else is shipped.

Layout produced::

    qqclient-<version>-<stamp>/
      python/            embeddable CPython + every dependency
      snowluma/          the OneBot bridge (bundled node)
      node/node.exe      runtime for SnowLuma
      start.bat          what the user double-clicks
      start.ps1          the real logic
      config.json        settings (cover folders etc.), created on first run

Why embeddable Python rather than a .NET bootstrapper that pip-installs on first
run: the target machine may be offline, and a first run that downloads 60MB of
wheels is indistinguishable from a hang. Shipping the runtime costs ~90MB and
removes an entire class of "it didn't work" reports.

Two traps this script exists to avoid:

- ``WindowsApps\\python.exe`` is a Microsoft Store *stub* that opens the Store.
  The launcher must call the bundled interpreter by absolute path.
- The embeddable distribution disables ``site`` by default, so ``pip`` and
  ``Lib\\site-packages`` do not work until ``python3xx._pth`` is rewritten.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

#: Pinned so builds are reproducible; bump deliberately, not by drift.
PYTHON_VERSION = "3.12.6"
PYTHON_URL = (f"https://www.python.org/ftp/python/{PYTHON_VERSION}/"
              f"python-{PYTHON_VERSION}-embed-amd64.zip")

#: Runtime dependencies. Same list the wheel declares, spelled out because the
#: embeddable interpreter has no resolver of its own until pip is bootstrapped.
RUNTIME_PACKAGES = [
    "numpy", "pillow", "reedsolo", "cryptography",
    "fastapi", "uvicorn", "python-multipart", "websockets",
]

START_BAT = r"""@echo off
chcp 65001 >nul
title markovCryptor 客户端
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 (
  echo.
  echo 启动失败。上面那几行说明了原因。
  pause
)
"""

START_PS1 = r"""# 一键启动客户端。由 tools/make_client_bundle.py 生成。
# 编码设置失败必须忽略：cmd 里 chcp 65001 之后再设 OutputEncoding，
# PS 5.1 有时抛"句柄无效"；若 ErrorActionPreference 是 Stop 会整脚本静默退出。
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# 必须用包内解释器的**绝对路径**：C:\...\WindowsApps\python.exe 是微软商店
# 的占位程序，裸调 python 会弹商店或直接失败。
$py = Join-Path $here "python\python.exe"
if (-not (Test-Path $py)) {
  Write-Host "包不完整：找不到 $py" -ForegroundColor Red
  Write-Host "请重新解压整个压缩包，不要只拷一部分。" -ForegroundColor Yellow
  exit 1
}

$node = Join-Path $here "node\node.exe"
if (-not (Test-Path $node)) {
  Write-Host "包不完整：找不到 $node" -ForegroundColor Red
  exit 1
}
$env:PATH = (Join-Path $here "node") + ";" + $env:PATH

$logFile = Join-Path $env:APPDATA "markovcryptor\client.log"
Write-Host "正在启动…（第一次会等 QQ 登录）" -ForegroundColor Cyan
& $py -X utf8 -m markovcryptor.client.run --snowluma (Join-Path $here "snowluma")
$code = $LASTEXITCODE
if ($code -ne 0) {
  # 失败原因必须看得见。pythonw 没有控制台时，日志文件是唯一的线索。
  Write-Host ""
  Write-Host "启动失败（退出码 $code）。" -ForegroundColor Red
  if (Test-Path $logFile) {
    Write-Host "日志：$logFile" -ForegroundColor Yellow
    Write-Host "最后几行：" -ForegroundColor Yellow
    Get-Content $logFile -Tail 12 | ForEach-Object { Write-Host "  $_" }
  } else {
    Write-Host "没有日志文件（$logFile）。多半是包不完整。" -ForegroundColor Yellow
  }
}
exit $code
"""


def _log(msg: str) -> None:
    print(msg, flush=True)


def _download(url: str, target: Path) -> None:
    """Fetch ``url`` to ``target``, reusing a previous download."""
    if target.exists() and target.stat().st_size > 1_000_000:
        _log(f"  复用已下载的 {target.name}")
        return
    _log(f"  下载 {url}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response, \
            open(target, "wb") as out:
        shutil.copyfileobj(response, out)


def _prepare_python(target: Path, cache: Path) -> None:
    """Unpack embeddable CPython and make ``site``/``pip`` work."""
    zip_path = cache / f"python-{PYTHON_VERSION}-embed-amd64.zip"
    _download(PYTHON_URL, zip_path)

    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(target)

    # embeddable 默认不启用 site，于是 pip 和 Lib\site-packages 都不工作。
    # 重写 ._pth 打开它，并显式加一行 site-packages。
    pth = next(target.glob("python3*._pth"))
    pth.write_text(
        "python312.zip\n.\nLib\\site-packages\n\nimport site\n",
        encoding="utf-8")

    # 先把 Lib\site-packages 建出来。embeddable 包里没有 Lib 目录，pip 首次
    # 写入时要在刚解压的目录里现建 —— 某些机器上（杀软或索引服务正在扫描）
    # 会撞成 WinError 5。先建好，把这一步从「安装时」挪到「解压后」。
    (target / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)

    _wait_executable(target / "python.exe")

    _log("  安装 pip")
    get_pip = cache / "get-pip.py"
    if not get_pip.exists():
        _download("https://bootstrap.pypa.io/get-pip.py", get_pip)
    _run([str(target / "python.exe"), str(get_pip),
          "--no-warn-script-location"], "pip")


def _wait_executable(exe: Path, *, timeout: float = 30.0) -> None:
    """Wait until ``exe`` can actually be launched.

    A freshly extracted executable is briefly unlaunchable: antivirus and the
    search indexer open new files the moment they appear, and Windows refuses
    ``CreateProcess`` with ``WinError 5`` while that scan holds them. This is a
    race, not a permission problem -- the same file runs fine a second later.

    Waiting for the *first* launch here means every later ``pip`` call is on a
    file that has already been opened once, which is why the failure always
    landed on the first pip invocation and never the second.
    """
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            code = subprocess.call([str(exe), "-c", "pass"],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
            if code == 0:
                return
        except OSError as exc:
            last = exc
        time.sleep(1.0)
    raise SystemExit(f"打包失败：{exe} 一直无法启动（{last}）；"
                     "多半是杀毒软件锁住了新解压的文件")


def _run(cmd: list[str], what: str) -> None:
    """Run a build step, retrying the Windows "file is busy" race.

    Right after ``extractall`` the fresh ``python.exe`` can be momentarily
    unlaunchable with ``WinError 5`` -- antivirus and the search indexer open new
    executables the instant they appear. It is a race, not a permission problem:
    the same command succeeds a second later. Retrying a few times turns a
    confusing build failure into a pause.
    """
    # 用临时文件接输出，不用 capture_output=True。
    # embeddable 的 pip 在输出被重定向到管道时会抛 WinError 5（它要写控制台
    # 缓冲区）；同一命令把输出接到文件就正常。这是 embeddable 版的怪癖，
    # 不是权限问题 —— 手动跑永远成功，只有被管道接走时才失败。
    # 日志文件放在当前目录而不是 %TEMP%：某些环境里 %TEMP% 在别的工作卷上，
    # 给子进程继承一个跨卷的句柄会失败。名字用 ASCII，避免中文路径问题。
    log = Path.cwd() / "build-step.log"
    last: Exception | None = None
    for attempt in range(4):
        try:
            with open(log, "wb") as out:
                code = subprocess.call(cmd, stdout=out,
                                       stderr=subprocess.STDOUT)
        except (PermissionError, OSError) as exc:   # WinError 5, transient
            last = exc
            time.sleep(1.0 + attempt)
            continue
        if code != 0:
            _log(f"  {what} 失败：")
            try:
                print(log.read_text(encoding="utf-8", errors="replace")[-1800:])
            except OSError:
                pass
            raise SystemExit(f"{what} 失败")
        return
    import traceback
    import traceback
    raise SystemExit(
        f"{what} 失败：{last}"
        + "".join(traceback.format_exception(
            type(last), last, last.__traceback__)))

def _install_deps(python: Path, wheels: Path, markovcryptor_wheel: Path) -> None:
    """Install dependencies, then this package.

    Dependencies first: installing our wheel first would make pip try to resolve
    them from the network, which fails on an offline build machine.
    """
    _log("  安装依赖（离线 wheel）")
    _run([str(python), "-m", "pip", "install", "--quiet",
          "--no-warn-script-location", "--no-index",
          "--find-links", str(wheels), *RUNTIME_PACKAGES], "依赖安装")

    _log("  安装 markovcryptor")
    _run([str(python), "-m", "pip", "install", "--quiet",
          "--no-warn-script-location", "--no-index",
          "--find-links", str(wheels), "--no-deps", str(markovcryptor_wheel)],
         "本库安装")


def _verify(python: Path) -> None:
    """Prove the bundle actually works before zipping 200MB.

    Checks the two things that fail silently: the client UI file (missing ->
    blank window) and the browser probe (missing -> no window at all).
    """
    _log("  自检")
    probe = (
        "import pathlib, markovcryptor\n"
        "root = pathlib.Path(markovcryptor.__file__).parent\n"
        "ui = root / 'client' / 'webui' / 'index.html'\n"
        "assert ui.exists(), f'客户端界面缺失: {ui}'\n"
        "from markovcryptor.client.run import main, find_snowluma\n"
        "from markovcryptor.client.browser import find_default_browser\n"
        "from markovcryptor.client.launcher import discover_endpoint\n"
        "print('OK', ui.stat().st_size)\n"
    )
    result = subprocess.run([str(python), "-c", probe], capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or "OK" not in result.stdout:
        print(result.stdout[-1200:])
        print(result.stderr[-1200:])
        raise SystemExit("自检失败：装出来的包不完整")


def _copy_snowluma(source: Path, target: Path) -> None:
    """Copy the SnowLuma files the bridge needs."""
    files = ["index.mjs", "package.json", "check-node-version.cjs",
             "launcher.bat", "EULA.md", "PRIVACY.md"]
    dirs = ["native", "client"]
    target.mkdir(parents=True, exist_ok=True)
    for name in files:
        item = source / name
        if item.exists():
            shutil.copy2(item, target / name)
    # 散落的 hash 命名 JS 分块，按后缀挑，不要写死文件名。
    for chunk in source.glob("*.js"):
        shutil.copy2(chunk, target / chunk.name)
    for name in dirs:
        item = source / name
        if item.is_dir():
            shutil.copytree(item, target / name, dirs_exist_ok=True)
    # config/ 留空目录：SnowLuma 自己往里写 onebot_<QQ号>.json。
    (target / "config").mkdir(exist_ok=True)


def _find_node() -> Path:
    """Locate a node.exe to bundle."""
    for candidate in (Path(r"D:\nodejs\node.exe"),
                      Path(r"C:\Program Files\nodejs\node.exe")):
        if candidate.exists():
            return candidate
    found = shutil.which("node")
    if not found:
        raise SystemExit("找不到 node.exe，无法打包")
    return Path(found)


def build(*, out: Path, snowluma: Path, wheels: Path,
          markovcryptor_wheel: Path, cache: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M")
    version = _read_version()
    target = out / f"qqclient-{version}-{stamp}"
    _log(f"打包到 {target}")

    _log("[1/5] Python 运行时")
    _prepare_python(target / "python", cache)

    _log("[2/5] 依赖与本库")
    # 必须传 python.exe，不是 python 目录 —— 传目录时 str(python) 得到的是
    # 文件夹路径，CreateProcess 报 WinError 5（"拒绝访问"），看着像权限问题。
    python_exe = target / "python" / "python.exe"
    _install_deps(python_exe, wheels, markovcryptor_wheel)
    _verify(python_exe)

    _log("[3/5] SnowLuma 与 node")
    _copy_snowluma(snowluma, target / "snowluma")
    node_dir = target / "node"
    node_dir.mkdir()
    shutil.copy2(_find_node(), node_dir / "node.exe")

    _log("[4/5] 启动脚本")
    (target / "start.bat").write_bytes(START_BAT.replace("\n", "\r\n").encode("utf-8"))
    (target / "start.ps1").write_bytes(
        b"\xef\xbb\xbf" + START_PS1.replace("\n", "\r\n").encode("utf-8"))

    _log("[5/5] 清单")
    manifest = {
        "format": "markovcryptor-client-bundle-v1",
        "version": version,
        "built_at": stamp,
        "python": PYTHON_VERSION,
        "packages": RUNTIME_PACKAGES,
        "entry": "start.bat",
        "notes": [
            "用户数据不落盘：好友、历史、图片队列退出即销毁",
            "config.json 只存图片文件夹路径等偏好，不含聊天内容",
        ],
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    _log(f"完成：{target}  ({size / 1048576:.0f} MB)")
    return target


def _read_version() -> str:
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version"):
            return line.split('"')[1]
    return "0.0.0"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DIST)
    parser.add_argument("--snowluma", type=Path, required=True,
                        help="已解压的 SnowLuma 目录")
    parser.add_argument("--wheels", type=Path, required=True,
                        help="离线 wheel 目录（含依赖与本库）")
    parser.add_argument("--wheel", type=Path, required=True,
                        help="markovcryptor 的 wheel 文件")
    parser.add_argument("--cache", type=Path, default=Path(r"D:\_Temp\bundle_cache"),
                        help="下载缓存目录")
    args = parser.parse_args(argv)

    # 全部转绝对路径：子进程（尤其是 embeddable python）的工作目录不是这里，
    # 相对路径会让 pip 找不到 wheel，或报 WinError 5 权限错误。
    build(out=args.out.resolve(), snowluma=args.snowluma.resolve(),
          wheels=args.wheels.resolve(), markovcryptor_wheel=args.wheel.resolve(),
          cache=args.cache.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
