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
exit
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

# 用 pythonw 跑：它是 GUI 子系统程序，不会留一个黑框在那儿。
# 但 pythonw 没有 stdout，失败时用户什么都看不到 —— 所以启动**先探测**
# 能不能起来：用 python（有控制台）跑一个极短的预检，通过了再用 pythonw
# 正式跑。这样黑框只在真正需要看错误的时候出现。
$pyw = Join-Path $here "python\pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }

Write-Host "正在启动…（第一次会等 QQ 登录）" -ForegroundColor Cyan

# 预检：能不能 import、能不能找到 SnowLuma。只跑 2 秒，不碰网络。
& $py -X utf8 -c "import markovcryptor.client.run, sys; sys.exit(0)" 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host "包不完整：Python 依赖装不上。" -ForegroundColor Red
  & $py -X utf8 -c "import markovcryptor.client.run" 2>&1 | Select-Object -Last 8
  exit 1
}

# 正式启动：无控制台。退出码通过 Start-Process -Wait 拿回来。
$proc = Start-Process -FilePath $pyw -ArgumentList @(
  "-X", "utf8", "-m", "markovcryptor.client.run",
  "--snowluma", (Join-Path $here "snowluma")
) -PassThru -Wait -WindowStyle Hidden
$code = $proc.ExitCode

if ($code -ne 0) {
  # 失败原因必须看得见 —— 这时才值得占用一个控制台。
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


#: Environment variables that make a *different* Python's packages visible.
#: The build machine usually has these set (a user-level site-packages, a venv,
#: or a PYTHONPATH), and the bundled interpreter happily picks them up. That is
#: how a bundle can pass its own self-check on the build machine and then fail on
#: a clean one with "No module named 'cryptography'".
_LEAKY_ENV = ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PYTHONSTARTUP")


def _clean_env() -> dict:
    """Environment for running the bundled interpreter as if on a clean machine.

    ``-I`` (isolated) already ignores ``PYTHON*`` variables and the user site
    directory, but a caller's environment can still leak through ``PYTHONHOME``
    on some builds, so scrub it explicitly as well.
    """
    env = dict(os.environ)
    for name in _LEAKY_ENV:
        env.pop(name, None)
    # 用户级 site-packages 的根目录：不删掉的话，即使 -I 没生效也可能被读到。
    env["PYTHONNOUSERSITE"] = "1"
    return env


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
                                       stderr=subprocess.STDOUT,
                                       env=_clean_env())
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
    _log("  自检（隔离模式）")

    # 关键：**必须**用 -I 和干净环境跑。
    # 打包机上通常装有这些依赖（用户级 site-packages / venv），不隔离的话
    # 包内 python 会从宿主借包，自检通过、到干净机器上就 ModuleNotFoundError。
    # 实际就是这样漏出去的：虚拟机启动报 No module named 'cryptography'。
    probe = (
        "import sys, pathlib\n"
        "assert not any('Roaming' in p or 'AppData' in p for p in sys.path), "
        "f'隔离失败，仍能看到宿主路径: {sys.path}'\n"
        "import numpy, PIL, reedsolo, cryptography, fastapi, uvicorn, websockets\n"
        "import markovcryptor\n"
        "root = pathlib.Path(markovcryptor.__file__).parent\n"
        "assert 'site-packages' in str(root), f'库不在包内: {root}'\n"
        "ui = root / 'client' / 'webui' / 'index.html'\n"
        "assert ui.exists(), f'客户端界面缺失: {ui}'\n"
        "from markovcryptor.client.run import main, find_snowluma\n"
        "from markovcryptor.client.browser import find_default_browser\n"
        "from markovcryptor.client.launcher import discover_endpoint\n"
        "print('OK', ui.stat().st_size)\n"
    )
    result = subprocess.run([str(python), "-I", "-c", probe],
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace",
                            env=_clean_env())
    if result.returncode != 0 or "OK" not in result.stdout:
        print(result.stdout[-1500:])
        print(result.stderr[-1500:])
        raise SystemExit(
            "自检失败：包里的依赖不完整。\n"
            "最常见原因：wheels 目录缺包，或自检没有隔离、借用了宿主的包。")


#: 运行期用不到的包/目录。删之前必须确认没人 import：
#: - pip：只用来装东西，装完就没用了（12 MB）
#: - numpy 的 tests/f2py/distutils：测试与构建工具，运行时零引用（~4 MB）
#: - __pycache__：首次 import 会重建，但不重建也能用（省下解压时间更值）
_PRUNE_PACKAGES = ("pip", "setuptools", "wheel", "pkg_resources")
_PRUNE_SUBDIRS = (
    "numpy/tests", "numpy/random/tests", "numpy/f2py", "numpy/distutils",
    "numpy/ma/tests", "numpy/linalg/tests", "numpy/fft/tests",
    "numpy/polynomial/tests", "numpy/testing/tests",
    "PIL/tests",
)


def _prune_bundle(root: Path) -> None:
    """Drop what a shipped client never loads.

    Everything removed here is either a build tool (pip, f2py), a test suite, or
    a byte-code cache that Python regenerates on demand. None of it is imported
    at runtime, so removing it changes behaviour not at all and saves tens of
    megabytes.
    """
    site = root / "python" / "Lib" / "site-packages"
    saved = 0

    def _size(path: Path) -> int:
        if path.is_file():
            return path.stat().st_size
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())

    for name in _PRUNE_PACKAGES:
        target = site / name
        if target.exists():
            saved += _size(target)
            shutil.rmtree(target, ignore_errors=True)

    for relative in _PRUNE_SUBDIRS:
        target = site / relative
        if target.exists():
            saved += _size(target)
            shutil.rmtree(target, ignore_errors=True)

    # 散落的 __pycache__：删掉后 Python 会用源码重新编译，功能不受影响。
    for cache in list(root.rglob("__pycache__")):
        if "node_modules" in str(cache):
            continue
        saved += _size(cache)
        shutil.rmtree(cache, ignore_errors=True)

    # pip 留下的入口脚本，没有 pip 就是死文件。
    scripts = root / "python" / "Scripts"
    if scripts.exists():
        saved += _size(scripts)
        shutil.rmtree(scripts, ignore_errors=True)

    if saved:
        _log(f"  裁剪掉 {saved / 1048576:.0f} MB 运行期用不到的内容")


def _verify_site_packages(python: Path) -> None:
    """Assert the required distributions are physically inside the bundle.

    Importing is not enough -- that is exactly how the host's packages slipped
    through. This checks the directory, so a bundle that would fail on a clean
    machine fails here instead.
    """
    site = python.parent / "Lib" / "site-packages"
    required = ["cryptography", "numpy", "PIL", "reedsolo", "fastapi",
                "uvicorn", "websockets", "markovcryptor"]

    def present(name: str) -> bool:
        # 有的包是目录（numpy/），有的是单文件模块（reedsolo.py），
        # 还有的是编译扩展（foo.pyd）。只查目录会误报 reedsolo 缺失。
        if (site / name).is_dir():
            return True
        return any(site.glob(f"{name}.py")) or any(site.glob(f"{name}.*.pyd"))

    missing = [name for name in required if not present(name)]
    if missing:
        raise SystemExit(
            "打包失败：包里缺少这些依赖：" + ", ".join(missing) + "\n"
            f"检查 wheels 目录是否完整：{site}")
    _log(f"  包内依赖齐全（{len(required)} 项）")


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
    _scrub_personal_state(target)


#: SnowLuma 运行时会写这些东西，其中任何一样都不该跟着交付包发出去：
#: 里面有账号、访问令牌、密码哈希、聊天数据库和日志。
_PERSONAL_DIRS = ("config", "data", "logs", "cache", "tmp")


def _scrub_personal_state(snowluma_root: Path) -> None:
    """Remove everything SnowLuma wrote while running on the build machine.

    The build machine has a real QQ account hooked up, so ``snowluma/`` there
    contains:

    * ``config/onebot_<QQ>.json`` -- the account number and the OneBot **access
      token** in clear text
    * ``config/webui.json`` -- WebUI password hash and salt
    * ``data/<QQ>/*.db`` -- message history, reactions, identity
    * ``logs/*.log`` -- everything that was said

    None of that belongs in a package handed to someone else. It is also not
    recoverable from the package alone, so leaving it in is a silent leak rather
    than a convenience.

    ``config/`` is kept as an *empty directory* because SnowLuma expects it to
    exist and writes into it on first run.
    """
    for name in _PERSONAL_DIRS:
        folder = snowluma_root / name
        if not folder.exists():
            continue
        if name == "config":
            # 目录保留，内容清空。
            for item in folder.iterdir():
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            continue
        shutil.rmtree(folder)
    (snowluma_root / "config").mkdir(exist_ok=True)


def _verify_no_personal_state(root: Path) -> None:
    """Fail the build if any account-specific file survived.

    Runs over the finished package, not just SnowLuma, so a stray config dropped
    anywhere is caught too.
    """
    offenders: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = str(path.relative_to(root)).replace("\\", "/")
        lowered = relative.lower()
        if "/site-packages/" in lowered or "/node_modules/" in lowered:
            continue

        name = path.name.lower()
        if name.startswith("onebot_") and name.endswith(".json"):
            offenders.append(relative)
        elif lowered.endswith("snowluma/config/webui.json"):
            offenders.append(relative)
        elif lowered.endswith("snowluma/config/runtime.json"):
            # 这个只有端口和日志策略，但它是运行期产物，不该预置。
            offenders.append(relative)
        elif "/snowluma/data/" in lowered:
            offenders.append(relative)
        elif "/snowluma/logs/" in lowered:
            offenders.append(relative)
        elif name.endswith(".db") and "snowluma" in lowered:
            offenders.append(relative)

    if offenders:
        shown = "\n  ".join(offenders[:10])
        raise SystemExit(
            "打包失败：交付包里混进了账号相关的文件，必须先清掉：\n  "
            + shown
            + "\n（OneBot 令牌、WebUI 密码哈希、聊天数据库、日志都属于个人数据）"
        )


#: 文本模型不放包里：它们是拿用户自己的语料训练的，别人的模型对他也没用，
#: 而且模型文件往往来自有版权的书。这里只留一个说明，告诉用户怎么弄。
MODELS_README = """这个目录放文本模型（*.json）。

为什么是空的
------------
文本密文需要一方持有中文马尔可夫模型。模型是从语料训练出来的，体积几 MB，
而且往往来自有版权的书 —— 所以不随包分发，需要你自己准备。

没有模型会怎样
--------------
- 图片密文照常可用（握手和图片收发都不依赖模型）
- 文本密文不可用，界面上「模型 0/0」并提示「文本信道不可用」

怎么获得模型
------------
1. 自己训练：客户端里 齿轮 -> 文本模型 -> 选一个 UTF-8 中文语料文件 -> 开始训练
   （一部小说、文集都行，几 MB 通常几秒训完）
2. 用别处拿到的：把对方的 .json 文件复制进这个目录，然后点「重新扫描」

和对方要不要用同一个模型
------------------------
不用。接收端会把手里所有模型逐个试，认得出是哪个模型写的就解出来；
认不出就当普通文本静默忽略。所以双方各自用自己的模型也能通信。
"""


def _write_models_readme(folder: Path) -> None:
    """Create an empty models/ directory with an explanation."""
    folder.mkdir(exist_ok=True)
    (folder / "README.txt").write_text(MODELS_README, encoding="utf-8")


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
    _verify_site_packages(python_exe)
    _verify(python_exe)
    # 校验通过之后再裁：裁完还要再验一次，否则可能删掉真正在用的东西。
    _prune_bundle(target)
    _verify(python_exe)

    _log("[3/5] SnowLuma 与 node")
    _copy_snowluma(snowluma, target / "snowluma")
    node_dir = target / "node"
    node_dir.mkdir()
    shutil.copy2(_find_node(), node_dir / "node.exe")

    _log("[4/5] 启动脚本")
    _write_models_readme(target / "models")
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

    _log("[检查] 确认没有账号相关的残留")
    _verify_no_personal_state(target)

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
