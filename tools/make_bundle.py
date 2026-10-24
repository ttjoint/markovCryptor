"""打一个可拷到干净 Windows 机器上离线安装的包。

给"本机 + 虚拟机各挂一个 QQ"这种真实连通性测试用：guest 里通常没网、
没装 Python 依赖，甚至可能没装 Python。

产物（``dist/qqtest-<版本>-<时间>/``）

    wheels/              本库 + 全部依赖的 wheel（离线 pip 装）
    snowluma/            SnowLuma 运行所需文件（可选，--with-snowluma）
    install.ps1          guest 端一键安装
    run-snowluma.ps1     起 SnowLuma 并等 OneBot 端口
    probe.ps1            发一张图，验 OneBot 通不通
    chat.ps1             两个 QQ 号互联：开终端 UI，握手 + 密文都走真 QQ

包里 `wheels/` 内的 markovcryptor wheel 自带 `covers/*.jpg`（两张由本项目
`data/` 真实测试照片缩成的 JPEG），供 QQ 模式下自动握手回包当底图。
不要换成程序生成的渐变噪声图：QQ 风控会盯上那种形态。
    CHECKLIST.md         手工步骤与预期结果
    manifest.json        版本、md5、端口分配

用法：

    python tools/make_bundle.py
    python tools/make_bundle.py --with-snowluma D:/Projects/SnowLuma-v1.14.22-win-x64-lite
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

#: guest 上要装的东西。``websockets`` 是 OneBot 客户端要用的，其它是本库与 Web。
REQUIREMENTS = [
    "numpy>=1.26",
    "pillow>=10.0",
    "reedsolo>=1.7",
    "cryptography>=42.0",
    "fastapi>=0.110",
    "uvicorn[standard]>=0.27",
    "python-multipart>=0.0.9",
    "websockets>=12.0",
]

#: 要覆盖的 Python 版本 × 架构。用户机器上装的是哪个我们控制不了 ——
#: 32 位 Python 在 64 位 Windows 上很常见，而它的 wheel 名字是 win32。
#: 组合不多，全下一遍，pip 自己挑。
PYTHON_TARGETS = ["3.11", "3.12", "3.13", "3.14", "3.15"]
PLATFORMS = ["win_amd64", "win32"]

#: guest 只需要这些文件就能跑 SnowLuma（其余是 WebUI 前端、日志、可选原生模块）。
SNOWLUMA_FILES = [
    "index.mjs", "package.json", "package-lock.json",
    "check-node-version.cjs", "launcher.bat",
    "config-ClrCrmAk.js", "logger-Bm8oJxzs.js", "server-D--Qu9qq.js",
    "utils-tSVKpzEf.js", "EULA.md", "PRIVACY.md",
]
SNOWLUMA_DIRS = ["native", "client"]


def _version() -> str:
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version"):
            return line.split('"')[1]
    return "0"


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(target: Path, version: str, platform: str,
              requirements: list[str]) -> bool:
    """下一组 wheel。成功（或已有完整缓存）返回 True。"""
    result = subprocess.run(
        [sys.executable, "-m", "pip", "download", "--only-binary=:all:",
         "--platform", platform, "--python-version", version,
         "--implementation", "cp", "--dest", str(target), *requirements],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode == 0:
        return True
    tail = (result.stderr.strip().splitlines() or ["?"])[-1]
    print(f"    cp{version}-{platform} 失败：{tail[:80]}")
    return False


def _build_wheels(target: Path) -> None:
    print("  构建本库 wheel…")
    # 编码必须显式给：pip 在中文系统上会输出 UTF-8，而 subprocess 默认
    # 按 GBK 解，遇到 0xaa 之类就是 UnicodeDecodeError。
    result = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(target), str(ROOT)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print(result.stdout[-800:])
        print(result.stderr[-800:])
        raise SystemExit("构建本库 wheel 失败")
    for version in PYTHON_TARGETS:
        for platform in PLATFORMS:
            print(f"  下载依赖 wheel（cp{version.replace('.', '')}-{platform}）…")
            if _download(target, version, platform, REQUIREMENTS):
                continue
            # uvicorn[standard] 会拖进 httptools / watchfiles 这些 C 扩展，
            # 新版本 Python 上常常还没编好。退一步用纯 Python 的 uvicorn：
            # 功能照常，只是慢一点，对连通性测试完全够。
            plain = [r for r in REQUIREMENTS if not r.startswith("uvicorn")]
            plain.append("uvicorn>=0.27")
            if _download(target, version, platform, plain):
                print(f"    cp{version}-{platform} 用纯 Python uvicorn（无 [standard]）")
            else:
                print(f"    cp{version}-{platform} 完全下不了，跳过")


#: SnowLuma 运行时写的东西，其中任何一样都不该跟着交付包发出去。
#:
#: **事故**：0.3.0 的测试包里带着 `config/onebot_<QQ>.json`，里面有真实
#: 的 OneBot accessToken —— 拿到它就能完全控制构建机上那个 QQ 号：
#: 收发任意消息、读全部好友和群。另外还带着聊天数据库和明文日志。
#:
#: 这不是"便利"问题，是静默泄漏：从包里看不出它是谁的，但它是真的能用。
_PERSONAL_DIRS = ("config", "data", "logs", "cache", "tmp")


def _scrub_personal_state(snowluma_root: Path) -> None:
    """删除 SnowLuma 在构建机上运行后留下的个人信息。

    保留 ``config/`` 空目录本身：SnowLuma 没有它就拒绝启动。
    """
    for name in _PERSONAL_DIRS:
        folder = snowluma_root / name
        if not folder.exists():
            continue
        if name == "config":
            for item in folder.iterdir():
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
            continue
        shutil.rmtree(folder, ignore_errors=True)


def _copy_snowluma(source: Path, target: Path) -> None:
    """拷出 SnowLuma 运行所需的文件。

    ``config/`` 必须带 —— SnowLuma 没有它就拒绝启动，而 ``qq-init``
    也是往那里写 OneBot 配置。
    """
    if not (source / "index.mjs").is_file():
        raise SystemExit(f"{source} 里没有 index.mjs，不是 SnowLuma 的 release 目录")

    target.mkdir(parents=True, exist_ok=True)
    for name in SNOWLUMA_FILES:
        item = source / name
        if item.is_file():
            shutil.copy2(item, target / name)
    for name in SNOWLUMA_DIRS:
        item = source / name
        if item.is_dir():
            shutil.copytree(item, target / name, dirs_exist_ok=True)

    (target / "config").mkdir(exist_ok=True)
    _scrub_personal_state(target)
    missing = [n for n in ("index.mjs", "native", "config")
               if not (target / n).exists()]
    if missing:
        print(f"  注意：SnowLuma 里缺 {missing}，guest 上可能起不来")


START_PS1 = r'''# 一键启动。由 tools/make_bundle.py 生成。
# 做四件事：查依赖、准备配置、后台起 SnowLuma、报出 probe 要的参数。
# 全程不需要开网页：协议同意、hook QQ、WebUI 密码都由环境变量给定。
param(
  [long]$Uin = 0,
  [int]$WsPort = 0,
  [int]$HttpPort = 0,
  [int]$TimeoutSec = 120
)
# 控制台编码：失败必须忽略。cmd 里 chcp 65001 之后再设 OutputEncoding，
# PS 5.1 有时会抛"句柄无效"；若 ErrorActionPreference 已是 Stop，
# 整个脚本会静默退出 —— 屏幕上什么都不剩，看起来像没运行。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot

$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
if (-not $py) {
  Write-Host "没有找到 Python。先装 Python 3.11+（勾 Add to PATH）。" -ForegroundColor Red
  exit 1
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
  Write-Host "没有找到 Node（SnowLuma 要 18+）。" -ForegroundColor Red
  exit 1
}
$snow = Join-Path $here "snowluma"
if (-not (Test-Path (Join-Path $snow "index.mjs"))) {
  Write-Host "找不到 $snow\index.mjs —— 这个包没带 SnowLuma。" -ForegroundColor Red
  exit 1
}

Write-Host "=== 一键启动 ===" -ForegroundColor Cyan

# [1/3] 依赖。装过就跳过，别每次都等。
Write-Host "`n[1/3] 检查依赖…" -ForegroundColor Yellow
& $py -c "import markovcryptor, fastapi, uvicorn, websockets, numpy, PIL, reedsolo, cryptography" 2>$null
if ($LASTEXITCODE -ne 0) {
  & $py -m pip install --no-index --find-links "$here\wheels" --upgrade `
    numpy pillow reedsolo cryptography `
    fastapi uvicorn python-multipart websockets markovcryptor
  if ($LASTEXITCODE -ne 0) { Write-Host "  依赖安装失败。" -ForegroundColor Red; exit 1 }
}
Write-Host "  已就绪"

# [2/3] 配置。指定了 UIN 且还没有配置就建一份；否则沿用现有的。
Write-Host "`n[2/3] 准备配置…" -ForegroundColor Yellow
$cfgDir = Join-Path $snow "config"
$existing = @()
if (Test-Path $cfgDir) {
  $existing = @(Get-ChildItem $cfgDir -Filter "onebot_*.json" | Sort-Object Name)
}
if ($Uin -ne 0 -and -not ($existing.Name -contains "onebot_$Uin.json")) {
  $cliArgs = @("qq-init", "--snowluma", $snow, "--uin", "$Uin")
  if ($WsPort -ne 0) { $cliArgs += @("--ws-port", "$WsPort") }
  if ($HttpPort -ne 0) { $cliArgs += @("--http-port", "$HttpPort") }
  & $py -m markovcryptor.cli @cliArgs
  if ($LASTEXITCODE -ne 0) { Write-Host "  写配置失败。" -ForegroundColor Red; exit 1 }
  $existing = @(Get-ChildItem $cfgDir -Filter "onebot_*.json" | Sort-Object Name)
}
if ($existing.Count -eq 0) {
  # 全新设备就是这种情况，而且它是**正常**的：onebot_<QQ号>.json 由
  # SnowLuma 在 hook 到已登录 QQ 之后自己写，文件名就是那个号。所以这里
  # 不能要求用户先报号码 —— 让他报了，也只是把这个号写进配置让 SnowLuma
  # 去匹配，纯属多余的一步。直接起服务，起来之后再读它写出来的东西。
  Write-Host "  还没有配置 —— 交给 SnowLuma 自动识别本机已登录的 QQ"
}
if ($existing.Count -gt 1) {
  # 多个号共用一个 SnowLuma 是支持的，但一键启动只能挑一个。
  Write-Host "  有多份配置，请用 -Uin 指定：" -ForegroundColor Yellow
  $existing | ForEach-Object { Write-Host ("    " + $_.Name) }
  exit 1
}
$wantPort = 0
$target = $null
if ($existing.Count -eq 1) {
  $cfgFile = $existing[0].FullName
  $target = Get-Content -Raw $cfgFile | ConvertFrom-Json
  $wantPort = [int]@($target.networks.wsServers)[0].port
  Write-Host ("  使用 " + $existing[0].Name)
}

# [3/3] 起服务。run-snowluma 自带无人值守环境变量，并会在端口就绪后返回。
Write-Host "`n[3/3] 启动 SnowLuma（自动 hook QQ、自动同意协议）…" -ForegroundColor Yellow
& (Join-Path $here "run-snowluma.ps1") -Snowluma $snow -Uin $Uin -TimeoutSec $TimeoutSec
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# SnowLuma 起来了，配置这时候才一定有（首次运行是它刚写出来的）。
$existing = @()
if (Test-Path $cfgDir) {
  $existing = @(Get-ChildItem $cfgDir -Filter "onebot_*.json" | Sort-Object Name)
}
if ($existing.Count -ge 1) {
  $target = Get-Content -Raw $existing[0].FullName | ConvertFrom-Json
  $wantPort = [int]@($target.networks.wsServers)[0].port
  $detected = $existing[0].BaseName -replace "^onebot_", ""
} else {
  $detected = "?"
}

Write-Host "`n=== 就绪 ===" -ForegroundColor Green
Write-Host ("  本机 QQ   : " + $detected)
Write-Host ("  OneBot WS : 127.0.0.1:" + $wantPort)
if ($target) {
  Write-Host ("  Token     : " + @($target.networks.wsServers)[0].accessToken)
}
Write-Host ("  WebUI     : http://127.0.0.1:5099/  (admin / mkx-webui-1)")
Write-Host "`n  发一张图验证（参数只有对端 QQ 号）：" -ForegroundColor Cyan
Write-Host "    .\probe.bat <对端QQ号>"
Write-Host "`n  两个号都起来之后，开始真正的互联（握手 + 密文都走 QQ）：" -ForegroundColor Cyan
Write-Host "    .\chat.bat"
Write-Host ""
exit 0
'''

INSTALL_BAT = r"""@echo off
REM Double-click entry. Win10 ships with ExecutionPolicy=Restricted, so
REM running a .ps1 directly fails with SecurityError. -ExecutionPolicy Bypass
REM only affects this one process; nothing on the machine is changed.
setlocal
REM Chinese console defaults to codepage 936; force UTF-8 so the
REM script's Chinese output is readable.
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul || where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python not found. Install Python 3.11+ and tick "Add to PATH".
  echo.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
"""


START_BAT = r"""@echo off
REM One-click: check deps, write config, start SnowLuma, wait for OneBot.
REM Usage: start.bat            (reuse existing config)
REM        start.bat 1741473279 (first run: create config for this UIN)
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul || where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python not found. Install Python 3.11+ first.
  echo.
  pause
  exit /b 1
)
set UINOPT=
if not "%~1"=="" set UINOPT=-Uin "%~1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %UINOPT%
if errorlevel 1 (
  echo.
  echo   Startup failed. See snowluma\logs\qqtest.log
  pause
  exit /b 1
)
echo.
pause
"""


RUN_BAT = r"""@echo off
REM Start SnowLuma. Pass -WsPort 9721 on the guest machine.
setlocal
REM Chinese console defaults to codepage 936; force UTF-8 so the
REM script's Chinese output is readable.
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-snowluma.ps1" %*
echo.
pause
"""


PROBE_BAT = r"""@echo off
REM Usage: probe.bat mkx-<UIN> 9721 <peer QQ number>
setlocal
REM Chinese console defaults to codepage 936; force UTF-8 so the
REM script's Chinese output is readable.
chcp 65001 >nul
cd /d "%~dp0"
REM Port and token are read from snowluma\config\onebot_<UIN>.json,
REM because SnowLuma picks them randomly on first run.
if "%~1"=="" (
  echo Usage: probe.bat PEER_QQ
  echo Example: probe.bat 1741473279
  echo Optional: probe.bat PEER_QQ UIN     ^(pick config explicitly^)
  pause
  exit /b 1
)
set UINOPT=
if not "%~2"=="" set UINOPT=-Uin "%~2"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0probe.ps1" -Peer "%~1" %UINOPT%
REM .bat must stay pure ASCII: cmd decodes it with the OEM codepage
REM (936 on a Chinese Windows), so Chinese here turns into garbage.
REM But it still has to SAY something when probe fails. Before this,
REM a silently-dying script left only "Press any key" on screen and
REM looked like probe did nothing at all.
if errorlevel 1 (
  echo.
  echo   probe FAILED ^(exit code %ERRORLEVEL%^). The output above says why.
  echo   If you still see nothing, capture the full log with:
  echo     powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0probe.ps1" -Peer %~1 ^> probe.log 2^>^&1
)
echo.
pause
"""


INSTALL_PS1 = r"""# 在 guest 上运行：离线安装依赖并自检。
# 控制台编码：失败必须忽略。cmd 里 chcp 65001 之后再设 OutputEncoding，
# PS 5.1 有时会抛"句柄无效"；若 ErrorActionPreference 已是 Stop，
# 整个脚本会静默退出 —— 屏幕上什么都不剩，看起来像没运行。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
# 这个文件由 tools/make_bundle.py 生成。
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
Write-Host "=== markovcryptor QQ 连通性测试包 ===" -ForegroundColor Cyan

# 优先 py 启动器：它总能找到真正装好的 Python。直接找 python 有时会
# 撞上 Microsoft Store 的占位符（运行后跳应用商店，不可用）。
# 这也解释了"install 能跑但 python 不在 PATH"——那种机器上只有 py。
$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
if (-not $py) {
  Write-Host "没有找到 Python。请先装 Python 3.11+（勾选 Add to PATH），再重跑本脚本。" -ForegroundColor Red
  Write-Host "离线机器可用 wheels/ 目录里的 wheel；Python 安装包需另行准备。"
  exit 1
}
Write-Host ("Python: " + (& $py --version))

# 先说清楚位数和版本 —— 装不上时绝大多数是这个原因，别让人去猜。
$info = & $py -c "import sys,platform,struct; print('%d.%d|%d|%s' % (sys.version_info[0], sys.version_info[1], struct.calcsize('P')*8, platform.machine()))"
$parts = $info -split '\|'
$ver = $parts[0]
$bits = $parts[1]
Write-Host ("        版本 {0} / {1} 位 / {2}" -f $ver, $bits, $parts[2])

$major, $minor = $ver -split '\.'
if ([int]$major -lt 3 -or ([int]$major -eq 3 -and [int]$minor -lt 11)) {
  Write-Host "`nPython 版本太低：本库要 3.11+，现在是 $ver。" -ForegroundColor Red
  Write-Host "包里带了 cp311–cp315 的 wheel（含 32 位），重装一个 3.11+ 的 Python 即可。"
  exit 2
}

Write-Host "`n[1/3] 离线安装依赖…" -ForegroundColor Yellow
# 逐项显式安装，而不是靠 "markovcryptor[web]" 的依赖解析：
# 那样会强制要求 uvicorn[standard]，而它的 C 扩展在很新的 Python 上
# 可能还没编好，整个安装就会失败。这里 uvicorn 只用纯 Python 版。
# 本包版本号固定（0.3.0），新包和旧包长得一样。pip 看到"已装 0.3.0"就
# 会跳过，于是 guest 上一直跑的是**第一次装的那份**代码 —— 表现为
# `chat.bat` 报 "unrecognized arguments: --onebot"，看着像脚本 bug，
# 其实是库没更新。--force-reinstall 保证每次都换成包里这份。
# 依赖跟着一起重装是它的副作用，离线 wheels 目录里都有，不额外联网。
& $py -m pip install --no-index --find-links "$here\wheels" --upgrade `
  --force-reinstall --no-deps `
  numpy pillow reedsolo cryptography `
  fastapi uvicorn python-multipart websockets
& $py -m pip install --no-index --find-links "$here\wheels" `
  --upgrade --force-reinstall --no-deps markovcryptor
if ($LASTEXITCODE -ne 0) {
  Write-Host "`n依赖安装失败。" -ForegroundColor Red
  Write-Host "本机 Python 是 $ver / $bits 位。包里有哪些 wheel：" -ForegroundColor Yellow
  Get-ChildItem (Join-Path $here "wheels") -Filter "numpy-*.whl" |
    ForEach-Object { "  " + $_.Name }
  Write-Host "如果你的组合不在上面，换一个 Python（建议 3.12 64 位）重试。"
  exit 1
}

Write-Host "`n[2/3] 检查能否启动…" -ForegroundColor Yellow
& $py -c "import markovcryptor, fastapi, uvicorn, websockets, numpy, PIL, reedsolo, cryptography, pathlib; ui = pathlib.Path(markovcryptor.__file__).parent / 'webui' / 'index.html'; assert ui.is_file(), 'WebUI 缺失'; print('imports ok, webui', ui.stat().st_size, 'bytes')"
if ($LASTEXITCODE -ne 0) { Write-Host "导入失败" -ForegroundColor Red; exit 1 }

$snow = Join-Path $here "snowluma"
if (Test-Path (Join-Path $snow "index.mjs")) {
  Write-Host "`n[3/3] SnowLuma 已就位：$snow" -ForegroundColor Yellow
  $node = Get-Command node -ErrorAction SilentlyContinue
  if (-not $node) {
    Write-Host "  警告：没有找到 node。SnowLuma 需要 Node 18+。" -ForegroundColor Yellow
  } else {
    Write-Host ("  node " + (& node --version))
  }
  Write-Host "  下一步（把 <你的UIN> 换成 guest 上登录的 QQ 号）："
  Write-Host "    python -m markovcryptor.cli qq-init --snowluma `"$snow`" --uin <你的UIN> --ws-port 9721 --http-port 9722"
  Write-Host "    .\run-snowluma.ps1"
} else {
  Write-Host "`n[3/3] 包里没有 SnowLuma（打包时没加 --with-snowluma）" -ForegroundColor Yellow
}
Write-Host "`n完成。详见 CHECKLIST.md" -ForegroundColor Green
"""


RUN_SNOWLUMA_PS1 = r"""# 起 SnowLuma 并等 OneBot 端口。由 tools/make_bundle.py 生成。
# 注意：param() 之前只能是注释 —— 放一条可执行语句进去，整个 param 块
# 会被当成普通语句解析，报 "The assignment expression is not valid"。
param(
  [string]$Snowluma = "$PSScriptRoot\snowluma",
  [int]$WsPort = 0,
  [long]$Uin = 0,
  [int]$TimeoutSec = 90,
  [string]$WebuiPassword = "mkx-webui-1"
)
# 控制台编码：失败必须忽略。cmd 里 chcp 65001 之后再设 OutputEncoding，
# PS 5.1 有时会抛"句柄无效"；若 ErrorActionPreference 已是 Stop，
# 整个脚本会静默退出 —— 屏幕上什么都不剩，看起来像没运行。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$ErrorActionPreference = "Stop"

if (-not (Test-Path (Join-Path $Snowluma "index.mjs"))) {
  Write-Host "找不到 $Snowluma\index.mjs" -ForegroundColor Red; exit 1
}

# 端口从配置读：SnowLuma 首次启动会自己生成一份默认配置（常见 3000/3001），
# 跟我们以为的 9721 不一致，写死就会一直等一个永远不来的端口。
if ($WsPort -eq 0) {
  $cfgDir = Join-Path $Snowluma "config"
  $files = @()
  if ($Uin -ne 0) {
    $files = @(Join-Path $cfgDir "onebot_$Uin.json")
  } elseif (Test-Path $cfgDir) {
    $files = @(Get-ChildItem $cfgDir -Filter "onebot_*.json" |
               Sort-Object Name | ForEach-Object { $_.FullName })
  }
  foreach ($f in $files) {
    if (-not (Test-Path $f)) { continue }
    try { $cfg = Get-Content -Raw $f | ConvertFrom-Json } catch { continue }
    $ws = @($cfg.networks.wsServers)[0]
    if ($ws) {
      $WsPort = [int]$ws.port
      Write-Host ("从 " + (Split-Path $f -Leaf) + " 读到 WS 端口 " + $WsPort)
      break
    }
  }
  # 注意：这里**不**回退到 9721。全新设备上 `config\onebot_*.json` 是
  # SnowLuma 起来之后、hook 到已登录 QQ 时才自己写的，文件名就是那个 QQ 号。
  # 启动前钉死一个"猜的"端口，等于去等一个永远不会来的端口 —— 实测卡满
  # 超时，屏幕上只剩"超时：9721 没起来"，看着像没装好。
}

# --- 无人值守三件套 -------------------------------------------------------
# 三个都是 SnowLuma 源码里自己认的环境变量，不改它一行代码：
#   HOOK_AUTOLOAD   自动 hook 已登录的 QQ，不用去 WebUI 里挑 PID
#   ACCEPT_EULA     自动同意协议，跳过首次启动的同意页
#   ACCEPT_PRIVACY
# 两边都设才会生效 —— 只设一个会被当成 partial 而忽略（源码里有明确提示）。
$env:SNOWLUMA_HOOK_AUTOLOAD = "1"
$env:SNOWLUMA_ACCEPT_EULA = "1"
$env:SNOWLUMA_ACCEPT_PRIVACY = "1"

# 省掉"必须大小写+特殊字符"的改密码流程：设了引导密码就按
# mustChangePassword=false 落盘，直接能用给定密码登录 WebUI。
if ($WebuiPassword) { $env:SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD = $WebuiPassword }

$log = Join-Path $Snowluma "logs\qqtest.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

Write-Host "启动 SnowLuma（自动 hook 已登录 QQ）…" -ForegroundColor Cyan
Write-Host "日志：$log"
# 必须不带 -NoNewWindow：那样 node 会继承当前控制台，调用方（start.bat /
# 本脚本的调用者）会一直等它退出，看着像"卡住"。加 -WindowStyle Hidden
# 让它真正独立运行，关闭启动窗口也不影响服务。
$proc = Start-Process -FilePath "node" -ArgumentList "index.mjs" `
  -WorkingDirectory $Snowluma -PassThru -WindowStyle Hidden `
  -RedirectStandardOutput $log -RedirectStandardError "$log.err"

for ($i = 0; $i -lt $TimeoutSec; $i++) {
  Start-Sleep -Seconds 1
  if ($proc.HasExited) {
    Write-Host "进程已退出（code=$($proc.ExitCode)），看日志 $log" -ForegroundColor Red
    Get-Content $log -Tail 20
    exit 1
  }
  # 端口还不知道就来翻配置：SnowLuma 一 hook 到 QQ 就会写出
  # onebot_<QQ号>.json，文件名本身就是账号 —— 这就是"免输入识别本机 QQ"
  # 的全部机制，不需要问用户任何东西。
  if ($WsPort -eq 0) {
    $cfgDir = Join-Path $Snowluma "config"
    if (Test-Path $cfgDir) {
      foreach ($f in @(Get-ChildItem $cfgDir -Filter "onebot_*.json" |
                       Sort-Object Name)) {
        try { $cfg = Get-Content -Raw $f.FullName | ConvertFrom-Json } catch { continue }
        $ws = @($cfg.networks.wsServers)[0]
        if ($ws -and [int]$ws.port -gt 0) {
          $WsPort = [int]$ws.port
          $discovered = $f.BaseName -replace "^onebot_", ""
          Write-Host ("发现本机 QQ " + $discovered + "，OneBot WS 端口 " + $WsPort)
          break
        }
      }
    }
    if ($WsPort -eq 0) { continue }
  }
  $open = Test-NetConnection -ComputerName 127.0.0.1 -Port $WsPort -InformationLevel Quiet -WarningAction SilentlyContinue
  if ($open) {
    Write-Host "就绪：OneBot WS 127.0.0.1:$WsPort（PID $($proc.Id)）" -ForegroundColor Green
    Write-Host "WebUI: http://127.0.0.1:5099/"
    exit 0
  }
}
if ($WsPort -eq 0) {
  Write-Host ("超时：没等到配置。多半是 QQ 没登录 —— SnowLuma 只能 hook " +
              "已登录的进程，它不能替你输密码。") -ForegroundColor Red
} else {
  Write-Host "超时：$WsPort 没起来，看日志 $log" -ForegroundColor Red
}
$tail = Get-Content $log -Tail 40
$tail

# 首次跑最常见的“没起来”其实是 EULA 没点，别让人去查网络。
if ($tail -match "awaiting EULA" -or $tail -match "consent") {
  Write-Host ""
  Write-Host "原因：SnowLuma 还等你同意 EULA/PRIVACY，此时不会 hook QQ。" -ForegroundColor Yellow
  Write-Host "  1. 打开 http://127.0.0.1:5099/ 登录"
  Write-Host "  2. 密码看上面的 initial credentials 一行（admin / 随机串）"
  Write-Host "  3. 同意后重跑本脚本"
  exit 2
}
exit 1
"""


CHAT_PS1 = r"""# 挂上本机的 QQ,开终端界面。由 tools/make_bundle.py 生成。
# 用法：.\chat.ps1 -Peer 3925697820 [-Port 8731] [-Psk 共享口令-123]
#
# 部署形态：一台设备 = 一个 QQ 号 + 一份本系统。本系统作为该 QQ 的混淆端,
# 把密文藏在私聊图片里发出去。所以这里**只需要本机那一份配置**,对端是
# 另一台设备上的号 —— 由 -Peer 指明。
#
# 同一台机器上挂了两个号时(只用于自己跟自己试),会自动把两份都接上,
# 这时不需要 -Peer。
param(
  [string]$Peer = "",
  [int]$Port = 8731,
  [string]$Psk = "共享口令-123"
)
# 详见 probe.ps1 顶部的说明：编码设置失败必须忽略，否则整脚本静默退出。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

Write-Host "chat 启动" -ForegroundColor Cyan

$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
if (-not $py) {
  Write-Host "没有找到 python，也没有 py 启动器。先跑 .\install.bat。" -ForegroundColor Red
  exit 1
}

# 库里没有 --onebot 说明装的是旧版（pip 会以为"0.3.0 已满足"而跳过）。
# 不先查这一下，症状是 argparse 报 "unrecognized arguments"，看着像脚本
# 传错了参数，实际是库没换新 —— 这个坑已经踩过一次，别再让人猜。
$canOnebot = & $py -c "import sys; from markovcryptor import cli; sys.exit(0 if '--onebot' in open(cli.__file__, encoding='utf-8').read() else 1)" 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host "装着的 markovcryptor 是旧版（不认识 --onebot）。" -ForegroundColor Red
  Write-Host "  重跑 .\install.bat 换成包里这份，然后再来。" -ForegroundColor Yellow
  exit 1
}

$cfgDir = Join-Path $PSScriptRoot "snowluma\config"
if (-not (Test-Path $cfgDir)) {
  Write-Host "没有 $cfgDir —— 先跑 .\start.bat 让 SnowLuma 生成配置。" -ForegroundColor Red
  exit 1
}
$files = @(Get-ChildItem $cfgDir -Filter "onebot_*.json" | Sort-Object Name)
if ($files.Count -eq 0) {
  Write-Host "snowluma\config 里没有 onebot_*.json —— 先跑 .\start.bat 把 QQ 挂上。" -ForegroundColor Yellow
  exit 1
}
if ($files.Count -gt 2) {
  Write-Host ("找到 " + $files.Count + " 份配置,无法自动选。删掉多余的,或用 serve --onebot 手动指定：") -ForegroundColor Yellow
  $files | ForEach-Object { Write-Host ("    " + $_.Name) }
  exit 1
}

# 从配置里取 端口 + Token + UIN,拼成 --onebot 参数。
# 不叫 $args：那是 PowerShell 的自动变量,随手赋值会炸。
$cliArgs = @()
$uins = @()
foreach ($f in $files) {
  $cfg = Get-Content -Raw $f.FullName | ConvertFrom-Json
  $ws = @($cfg.networks.wsServers)[0]
  if (-not $ws) {
    Write-Host ($f.Name + " 里没有 wsServers,配置不完整。") -ForegroundColor Red
    exit 1
  }
  # 文件名 onebot_<UIN>.json 里的 UIN 就是账号。
  $uin = $f.BaseName -replace "^onebot_", ""
  $url = "ws://127.0.0.1:" + $ws.port + "/"
  $cliArgs += @("--onebot", ($uin + "=" + $url + ";" + $ws.accessToken))
  $uins += $uin
  Write-Host ("  本机 " + $uin + " -> " + $url)
}

# 单号模式必须知道对端是谁,否则第一条消息没有收件人。
#
# 对端号**没法自动识别**:它是另一台设备上的 QQ,本机看到的只是 29 个好友
# 里的某一个,没有任何可靠依据去挑。但只问一次就够了 —— 记住它,之后
# 双击 chat.bat 不再问。本机号则完全不用问(SnowLuma 写出来的配置文件名
# 就是它)。
$peerFile = Join-Path $PSScriptRoot "peer.txt"
if ($uins.Count -eq 1) {
  if (-not $Peer -and (Test-Path $peerFile)) {
    $Peer = (Get-Content -Raw $peerFile).Trim()
    if ($Peer) { Write-Host ("  对端 " + $Peer + " (上次记住的)") -ForegroundColor Gray }
  }
  # 只在真正拿到一个像 QQ 号的串时才记，避免把空值/垃圾写进去
  if ($Peer -match "^\d{5,12}$") {
    Set-Content -Path $peerFile -Value $Peer -Encoding ASCII
  }
  if (-not $Peer) {
    Write-Host "`n本机只挂了一个 QQ 号,还需要知道对端:" -ForegroundColor Yellow
    Write-Host ("    .\chat.ps1 -Peer <对端QQ号>") -ForegroundColor Yellow
    Write-Host "  (对端是另一台设备上运行本系统的那个号；只需填这一次，之后会记住)" -ForegroundColor Yellow
    exit 1
  }
  if ($Peer -eq $uins[0]) {
    Write-Host ("`n-Peer 不能是本机自己的号 " + $Peer) -ForegroundColor Red
    exit 1
  }
  $cliArgs += @("--peer", $Peer)
  Write-Host ("  对端 " + $Peer + " (另一台设备)") -ForegroundColor Gray
} elseif ($Peer) {
  Write-Host "`n本机挂了两份配置,-Peer 用不上(对端就是本机另一个号),已忽略。" -ForegroundColor Yellow
}

Write-Host "`n打开 http://127.0.0.1:$Port/ 选「终端」页签开始。" -ForegroundColor Green
Write-Host "两侧隐写口令必须一致,本次是：$Psk" -ForegroundColor Green
Write-Host "(另一台机器要用同样的 -Psk,否则发出去的图对方解不出来)`n"

& $py -m markovcryptor.cli serve --port $Port --psk $Psk @cliArgs
exit $LASTEXITCODE
"""


CHAT_BAT = r"""@echo off
REM Attach this machine's QQ and open the terminal UI. One QQ per device:
REM   chat.bat -Peer 3925697820
REM (Two local accounts also work; then -Peer is not needed.)
setlocal
REM Chinese console defaults to codepage 936; force UTF-8 for readable output.
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul || where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python not found. Install Python 3.11+ first.
  echo.
  pause
  exit /b 1
)
REM Note: no unescaped parentheses may appear inside this if-block; cmd
REM closes the block at the first one and the rest of the script never runs.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0chat.ps1" %*
if errorlevel 1 (
  echo.
  echo   chat failed ^(exit code %ERRORLEVEL%^). The output above says why.
  echo.
)
pause
"""


PROBE_PS1 = r"""# 发一张图，验 OneBot 通不通。由 tools/make_bundle.py 生成。
# 用法：.\probe.ps1 -Peer <对端QQ号> [-Uin <本机QQ号>] [-WsPort N] [-Token T]
# 不给 Uin/Token/WsPort 时自动读 snowluma\config\onebot_<UIN>.json ——
# SnowLuma 自己生成的配置端口和 token 都是随机的，手抄必错。
#
# 两条硬规矩，都是踩出来的：
#   1. param() 之前只能是注释 —— 放一条可执行语句进去，整个 param 块
#      会被当成普通语句解析，报 "The assignment expression is not valid"。
#   2. 绝不能一上来就 $ErrorActionPreference = "Stop"。连不上 OneBot 是
#      常态（SnowLuma 没起、端口不对），这时 PowerShell 会静默终止脚本，
#      屏幕上只剩 cmd 的 "Press any key" —— 看起来像 probe 什么都没做。
param(
  [Parameter(Mandatory = $true)][long]$Peer,
  [long]$Uin = 0,
  [int]$WsPort = 0,
  [string]$Token = ""
)

# 控制台编码只在真控制台上动，且失败必须忽略。
# cmd 里 chcp 65001 之后再设 OutputEncoding，PS 5.1 有时会抛
# "句柄无效" —— 那不该让整个探测挂掉。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# 先说话，再干活。哪怕后面一行代码都跑不动，用户也知道脚本进来了。
Write-Host "probe 启动（对端 $Peer）" -ForegroundColor Cyan

# python 不一定在 PATH 里（只装 py 启动器、或没勾 Add to PATH 都很常见）。
# install.ps1 能跑不代表这里能跑 —— 两边都要自己找。
# 优先 py 启动器：它总能找到真正装好的 Python。直接找 python 有时会
# 撞上 Microsoft Store 的占位符（运行后跳应用商店，不可用）。
$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
if (-not $py) {
  Write-Host "没有找到 python，也没有 py 启动器。" -ForegroundColor Red
  Write-Host "先跑 .\install.bat 装依赖；它同样需要 Python 3.11+。"
  exit 1
}

# 从配置文件里取真实的端口和 token。优先级：命令行 > 配置文件 > 内置默认。
$cfg = $null
if (-not $Token -or $WsPort -eq 0) {
  $cfgDir = Join-Path $PSScriptRoot "snowluma\config"
  $files = @()
  if ($Uin -ne 0) {
    $files = @(Join-Path $cfgDir "onebot_$Uin.json")
  } elseif (Test-Path $cfgDir) {
    $files = @(Get-ChildItem $cfgDir -Filter "onebot_*.json" |
               Sort-Object Name | ForEach-Object { $_.FullName })
  }
  foreach ($f in $files) {
    if (-not (Test-Path $f)) { continue }
    try { $cfg = Get-Content -Raw $f | ConvertFrom-Json } catch { continue }
    Write-Host ("读取配置：" + (Split-Path $f -Leaf))
    break
  }
  if ($cfg) {
    $ws = @($cfg.networks.wsServers)[0]
    if ($ws) {
      if ($WsPort -eq 0) { $WsPort = [int]$ws.port }
      if (-not $Token) { $Token = [string]$ws.accessToken }
    }
  }
}
if ($WsPort -eq 0) { $WsPort = 9721 }
if (-not $Token) {
  Write-Host "找不到配置里的 token，也没有 -Token。" -ForegroundColor Red
  Write-Host r"先跑 .\start.bat（它会生成配置），或直接 -Token <wsServers[0].accessToken>。"
  exit 1
}
Write-Host ("连接 ws://127.0.0.1:$WsPort/  token 长度 " + $Token.Length)

# 嵌入的探测脚本。单引号 here-string：$ 和 % 都不展开。
# 每条失败路径都翻译成一句人话，绝不留裸 traceback ——
# 用户看不懂 traceback，只会说"probe 无效"。
$code = @'
import asyncio, base64, hashlib, io, json, sys
sys.stdout.reconfigure(encoding="utf-8")   # 中文输出，别依赖控制台代码页
import numpy as np
from PIL import Image

def png(size, seed):
    arr = np.random.default_rng(seed).integers(0, 256, (size, size, 3), dtype=np.uint8)
    b = io.BytesIO(); Image.fromarray(arr, "RGB").save(b, "PNG"); return b.getvalue()

def die(msg, hint=""):
    print("失败：" + msg)
    if hint:
        print("  怎么办：" + hint)
    sys.exit(1)

async def main():
    port, token, peer = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])

    try:
        from markovcryptor.onebot_ws import connect
    except Exception as e:
        die("导入 markovcryptor 失败（%s: %s）" % (type(e).__name__, e),
            r"先跑 .\install.bat。")

    blob = png(200, 3)
    print("发送 %d bytes md5=%s" % (len(blob), hashlib.md5(blob).hexdigest()))

    try:
        ws = await asyncio.wait_for(
            connect("ws://127.0.0.1:%d/" % port, token).__aenter__(), timeout=15)
    except (ConnectionRefusedError, OSError) as e:
        die("连不上 ws://127.0.0.1:%d/（%s）" % (port, type(e).__name__),
            r"SnowLuma 没起，或端口不对。先看 start.bat 打印的 OneBot WS 端口，"
            r"或跑 .\run-snowluma.ps1 看日志。")
    except asyncio.TimeoutError:
        die("连 ws://127.0.0.1:%d/ 超时" % port,
            r"SnowLuma 可能卡住，查 snowluma\logs\qqtest.log。")
    except Exception as e:
        name = type(e).__name__
        if "401" in str(e) or "Unauthorized" in str(e) or name == "InvalidStatus":
            die("OneBot 拒绝了 token（%s）" % e,
                r"配置里的 accessToken 和 SnowLuma 实际用的不一致。重跑 .\start.bat，"
                r"或删掉 snowluma\config\onebot_*.json 让它重新生成。")
        die("连接出错（%s: %s）" % (name, e))

    try:
        await ws.send(json.dumps({"action": "send_private_msg", "echo": "p",
            "params": {"user_id": peer, "message": [{"type": "image",
            "data": {"file": "base64://" + base64.b64encode(blob).decode()}}]}}))
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=60)
            except asyncio.TimeoutError:
                die("60 秒没等到回包",
                    r"OneBot 连上了但不回应，看 snowluma\logs\qqtest.log 有没有报错。")
            m = json.loads(raw)
            if m.get("echo") != "p":
                continue          # 事件推送，不是我们的回包
            if m.get("status") == "ok":
                print("已发出：message_id =", (m.get("data") or {}).get("message_id"))
                print("请确认对端 QQ 是否收到图片。收不到多半不是好友、或对端不在线。")
                return 0
            die("OneBot 返回失败：retcode=%s %s" % (m.get("retcode"), m.get("message") or m.get("wording") or ""),
                "常见原因：对端不是你好友、是机器人、或 QQ 号输错了。")
    except Exception as e:
        die("发送过程出错（%s: %s）" % (type(e).__name__, e))
    finally:
        try:
            await ws.close()
        except Exception:
            pass

sys.exit(asyncio.run(main()) or 0)
'@
$tmp = Join-Path $env:TEMP "mkx_probe.py"
$code | Out-File -Encoding utf8 $tmp
& $py $tmp $WsPort $Token $Peer
exit $LASTEXITCODE
"""


CHECKLIST = r"""# QQ 跨机连通性测试 · 操作清单

本包由 `tools/make_bundle.py` 生成。目标是**本机与虚拟机各挂一个 QQ，
互相收发混淆数据**，验真实网络下的连通性。

---

## 一、前置条件（只需满足一次）

| 项目 | 说明 |
|---|---|
| **Python 3.11+** | 包里 `wheels/` 是离线依赖，Python 本体要自己装 |
| **Node 18+** | SnowLuma 需要 |
| **QQ 客户端** | **已登录**，且两个号**互为好友** |

> 包里不含 Python / Node 本体。QQ 必须**先登录**——SnowLuma 是 hook 已登录的
> 进程，它不能替你输密码、过验证码。这是唯一无法自动化的环节。

---

## 二、一键启动（推荐）

把整个 `qqtest-*` 目录拷进虚拟机，然后**双击 `start.bat`** —— 不需要
传任何号码，也不需要先手工建配置。

它会依次做：

1. 检查依赖（装过就跳过，几秒钟）
2. 后台起 SnowLuma，自动 hook 本机已登录的 QQ
3. 等它自己写出 `snowluma\config\onebot_<QQ号>.json`，从里面读端口和 token
4. 打印识别结果

**本机 QQ 号不用输入**：SnowLuma hook 到已登录 QQ 后写的配置文件名
就是那个号，脚本从中读出来。全新设备（连 `config` 目录都没有）也是
这条路，实测 5 秒。

**全程不需要打开网页**：协议同意、hook QQ、WebUI 密码都由环境变量给定
（见附录）。node 在后台独立运行，关掉窗口也不影响。

预期输出最后几行（号码随实际登录的号变化）：

```
=== 就绪 ===
  本机 QQ   : 1741473279
  OneBot WS : 127.0.0.1:3001
  Token     : <自动生成>
  WebUI     : http://127.0.0.1:5099/  (admin / mkx-webui-1)
```

### 关于对端 QQ 号（只填一次）

本机号能自动识别，**对端号不能**：它是另一台设备上的 QQ，本机看到的
只是好友列表里的某一个，没有任何可靠依据去挑。所以第一次要告诉它：

```powershell
.\chat.bat -Peer 3925697820
```

之后它会记在 `peer.txt` 里，**再双击 `chat.bat` 就不问了**。想换对端，
带新的 `-Peer` 再跑一次即可。

### 分步执行（想看清每一步时）

```powershell
.\install.bat                      # 装依赖，可选：start.bat 会自动装
.\run-snowluma.bat                 # 只起服务
.\probe.bat 3925697820             # 发一张图（参数是对端 QQ 号）
.\chat.bat                         # 两个号互联：握手 + 密文都走真 QQ
```

`run-snowluma.bat` 端口从 `snowluma\config\onebot_*.json` 读，不用手工传。
`chat.bat` 自动读配置并识别本机号；对端号第一次用 `-Peer` 给，之后
从 `peer.txt` 读回。

---

## 三、怎么验证连通（分三层，逐层排除）

**不要一上来就测隐写**。哪层断了就停在那层。

### 第 1 层：QQ 本身通不通

虚拟机给宿主机那个号发一句普通文字。收到了 → 好友关系、网络、QQ 都正常。

### 第 2 层：OneBot 能不能发消息

```powershell
.\probe.bat <对端QQ号>
```

预期 `已发出: {'message_id': ...}`。这同时验了端口通、token 对、
`send_private_msg` 有权限。**不需要知道端口和 token** —— 脚本自己从配置里读。

### 第 3 层：图片能不能原样往返（P0）

这是像素域隐写的前提，需要**宿主机能连到虚拟机的 OneBot**（见第五节）。

在宿主机跑：

```bash
python tools/qq_probe.py \
  --a-uin <本机UIN>  --a-ws ws://127.0.0.1:9711/ --a-token mkx-<本机UIN> \
  --b-uin <guestUIN> --b-ws ws://<guest IP>:9721/ --b-token mkx-<guestUIN>
```

预期：

```
#0 单图     PNG (200, 200)  120366B md5=dacefe…  ✓ 逐字节一致
结论：QQ 原样往返图片，像素域隐写（DCT-QIM）在 QQ 上可用。
```

**跨机之后必须重跑这一步**：一致性结论之前是同一台机器两个号之间得的，
现在中间多了一条虚拟机网络路径。如果这里变成「被改动」，说明有环节重编码。

---

## 四、端口约定

同一台机器挂两个号时才需要分开（会 `EADDRINUSE`）。虚拟机和宿主机是
两个系统，5099 / 8731 相同没有关系。

| | 本机（宿主机） | 虚拟机（Win10） |
|---|---|---|
| QQ 号 | ________________ | ________________ |
| OneBot WS | 9711 | 9721 |
| OneBot HTTP | 9712 | 9722 |
| SnowLuma WebUI | 5099 | 5099 |
| markovcryptor 端口 | 8731 | 8731 |

---

## 五、虚拟机网络怎么配（最容易卡在这）

默认 **NAT** 下 guest 能出去，但宿主机连不进去。

### 方案 A：桥接网卡（推荐）

虚拟机设置 → 网络适配器 → 「桥接（Bridged）」→ 重启 guest。
guest 会拿到同网段 IP（`ipconfig` 看），直接 `ws://<guest IP>:9721/`。

### 方案 B：NAT + 端口转发

虚拟机设置 → 网络 → 高级 → 端口转发：

| 主机端口 | 子系统端口 | 协议 |
|---|---|---|
| 9721 | 9721 | TCP |
| 9711 | 9711 | TCP（要从 guest 连宿主时加） |

NAT 下宿主机用 `127.0.0.1:9721` 去连。

### 还要改 SnowLuma 的监听地址

`onebot_<UIN>.json` 里 `wsServers[0].host` 默认 `127.0.0.1`，**只能本机连**。
要让另一台机器连进来：

```powershell
python -m markovcryptor.cli qq-init --snowluma .\snowluma --uin <UIN> `
  --ws-port 9721 --http-port 9722 --host 0.0.0.0 --force
```

改完重启 SnowLuma。

> 只在本机测时**不要**用 0.0.0.0 —— 那会把隐写接口暴露到局域网。

### 防火墙

guest 上第一次会弹窗，选「允许」。没弹就用管理员 PowerShell：

```powershell
New-NetFirewallRule -DisplayName "OneBot 9721" -Direction Inbound -LocalPort 9721 -Protocol TCP -Action Allow
```

---

## 六、本机（宿主机）步骤

```bash
python tools/qq_setup.py init --snowluma D:/Projects/SnowLuma-v1.14.22-win-x64-lite --a-uin <本机UIN>
python tools/qq_setup.py up   --snowluma D:/Projects/SnowLuma-v1.14.22-win-x64-lite
python -m markovcryptor.cli serve
```

浏览器开 http://127.0.0.1:8731/ → 终端页。

---

## 七、常见失败与判断

| 现象 | 原因 | 处理 |
|---|---|---|
| `ConnectionRefused` | SnowLuma 没起 / 端口不对 | 看 `snowluma\logs\qqtest.log`；`start.bat` 会打印真实端口 |
| `chat.bat` 说"只找到 1 份配置" | 只挂了一个 QQ 号 | 互联要两个号，各跑一次 `start.bat <号>` 生成配置 |
| 握手完成但密文解不出来 | 两侧 `-Psk` 不一致 | 两边用同一个 `chat.bat -Psk <口令>`；不一致时图看起来是普通照片，不会报错 |
| `HTTP 401` | token 不匹配 | 用配置文件里的 `wsServers[0].accessToken`，不是 `httpServers` 那个 |
| 连上了但收不到消息 | 好友关系 / 发的是群聊 | 确认两号互为好友，确认私聊 |
| 没有 `login detected` | QQ 没登录 | 先登录 QQ，再重跑 `start.bat` |
| 卡在 `awaiting EULA` | 旧脚本没设 `SNOWLUMA_ACCEPT_*` | 用本包的 `start.bat`，它已经设好 |
| `EADDRINUSE` | 同机两号端口相同 | 每号一份 `onebot_<UIN>.json`，端口分开 |
| 图片「被改动」 | 中间有重编码 | 改走中文信道，或换文件传输 |
| `禁止运行脚本` | Win10 默认执行策略 | 双击 `.bat`；或 `powershell -ExecutionPolicy Bypass -File x.ps1` |
| `.ps1` 报 `缺少实际参数` | 脚本没 BOM（旧包） | 用 0.3.0 之后重新打的包 |
| `python` 不是命令 | 没勾 Add to PATH | 用 `py`，或跑 `.bat`（内部优先用 `py`） |
| 装依赖报 `No matching distribution found for numpy` | 缺对应 wheel | 用本包（覆盖 cp311–cp315 × 32/64 位） |

排查服务状态：

```powershell
Get-Content .\snowluma\logs\qqtest.log -Tail 30
```

---

## 八、测完收工

```powershell
# guest：只结束我们起的 Node，不动 QQ
Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
  Where-Object { $_.CommandLine -match 'index\.mjs' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

**不要**按进程名杀 QQ（`Stop-Process -Name QQ`）—— 会关掉你正在用的 QQ。

---

## 附录：无人值守用到的 SnowLuma 环境变量

这些都写在 `run-snowluma.ps1` 里，是 SnowLuma 源码自己认的变量，**不改它一行代码**：

| 变量 | 作用 |
|---|---|
| `SNOWLUMA_HOOK_AUTOLOAD=1` | 自动 hook 已登录的 QQ，不用去 WebUI 挑 PID |
| `SNOWLUMA_ACCEPT_EULA=1` | 自动同意 EULA，跳过同意页 |
| `SNOWLUMA_ACCEPT_PRIVACY=1` | 自动同意隐私协议。**两个都设才生效**，只设一个会被忽略 |
| `SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD` | 设定初始密码，同时 `mustChangePassword=false`，跳过强制改密码 |

默认 WebUI 密码：`admin` / `mkx-webui-1`（可改：`run-snowluma.bat -WebuiPassword <新密码>`）。

协议同意是**临时**的：不写 `consent.json`，每次启动靠环境变量生效。
"""


def _write_bundle(root: Path, args: argparse.Namespace, version: str) -> dict:
    manifest = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "markovcryptor_version": version,
        "python_requires": ">=3.11",
        "ports": {"host_ws": 9711, "host_http": 9712,
                  "guest_ws": 9721, "guest_http": 9722,
                  "snowluma_webui": 5099, "mkx_serve": 8731},
    }

    wheels = root / "wheels"
    wheels.mkdir(parents=True)
    _build_wheels(wheels)
    manifest["wheels"] = sorted(p.name for p in wheels.glob("*.whl"))

    if args.with_snowluma:
        print("  复制 SnowLuma…")
        _copy_snowluma(Path(args.with_snowluma).resolve(), root / "snowluma")
        manifest["snowluma_included"] = True
    else:
        manifest["snowluma_included"] = False

    for name, text in (("install.ps1", INSTALL_PS1),
                       ("run-snowluma.ps1", RUN_SNOWLUMA_PS1),
                       ("probe.ps1", PROBE_PS1),
                       ("chat.ps1", CHAT_PS1),
                       ("chat.bat", CHAT_BAT),
                       ("install.bat", INSTALL_BAT),
                       ("run-snowluma.bat", RUN_BAT),
                       ("probe.bat", PROBE_BAT),
                       ("start.ps1", START_PS1),
                       ("start.bat", START_BAT),
                       ("CHECKLIST.md", CHECKLIST)):
        path = root / name
        body = text.replace("\r\n", "\n").replace("\n", "\r\n")
        if name.endswith(".ps1"):
            # Windows PowerShell 5.1（Win10 自带）会按系统 ANSI 代码页读无 BOM
            # 的 .ps1。UTF-8 里的中文按 GBK 解，尾字节会吃掉行尾 CRLF，
            # 把 param( 拼进上一行注释，脚本直接报“缺少实际参数”。
            # 所以：CRLF + UTF-8 BOM，两个都是硬要求。
            path.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
        elif name.endswith(".bat"):
            # .bat 走 OEM 代码页（中文机是 936）。保持纯 ASCII，不要放中文，
            # 否则 cmd 下一样会乱。
            path.write_bytes(body.encode("ascii"))
        else:
            path.write_bytes(body.encode("utf-8"))

    manifest["files"] = {
        str(path.relative_to(root)).replace("\\", "/"): _md5(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--with-snowluma", metavar="DIR",
                        help="把已解压的 SnowLuma release 一起打进包里")
    parser.add_argument("--out", metavar="DIR", help="输出目录（默认 dist/）")
    args = parser.parse_args(argv)

    version = _version()
    stamp = time.strftime("%Y%m%d-%H%M")
    target = Path(args.out or DIST) / f"qqtest-{version}-{stamp}"
    target.mkdir(parents=True, exist_ok=True)
    print(f"打包到 {target}")

    manifest = _write_bundle(target, args, version)

    archive = target.parent / f"{target.name}.zip"
    print(f"压缩 -> {archive.name}")
    with tempfile.TemporaryDirectory() as staging:
        shutil.copytree(target, Path(staging) / target.name)
        shutil.make_archive(str(archive.with_suffix("")), "zip",
                            Path(staging), target.name)

    size = archive.stat().st_size / 1048576
    snow = "已包含" if manifest["snowluma_included"] else "未包含"
    print(f"\n完成：{archive}  ({size:.1f} MB)")
    print(f"  wheel {len(manifest['wheels'])} 个，SnowLuma {snow}")
    print("  拷到 guest（Win10）后：.\\install.ps1  ->  .\\CHECKLIST.md")
    print("\n注意：包里没有 Python 和 Node 本体，guest 需自行安装"
          "（Python 3.11+、Node 18+）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
