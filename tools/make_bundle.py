"""打一个可拷到干净 Windows 机器上离线安装的包。

给"本机 + 虚拟机各挂一个 QQ"这种真实连通性测试用：guest 里通常没网、
没装 Python 依赖，甚至可能没装 Python。

产物（``dist/qqtest-<版本>-<时间>/``）

    wheels/              本库 + 全部依赖的 wheel（离线 pip 装）
    snowluma/            SnowLuma 运行所需文件（可选，--with-snowluma）
    install.ps1          guest 端一键安装
    run-snowluma.ps1     起 SnowLuma 并等 OneBot 端口
    probe.ps1            发一张图，验 OneBot 通不通
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
        capture_output=True, text=True)
    if result.returncode == 0:
        return True
    tail = (result.stderr.strip().splitlines() or ["?"])[-1]
    print(f"    cp{version}-{platform} 失败：{tail[:80]}")
    return False


def _build_wheels(target: Path) -> None:
    print("  构建本库 wheel…")
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(target), str(ROOT)],
        check=True, capture_output=True)
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
    missing = [n for n in ("index.mjs", "native", "config")
               if not (target / n).exists()]
    if missing:
        print(f"  注意：SnowLuma 里缺 {missing}，guest 上可能起不来")


INSTALL_BAT = r"""@echo off
REM Double-click entry. Win10 ships with ExecutionPolicy=Restricted, so
REM running a .ps1 directly fails with SecurityError. -ExecutionPolicy Bypass
REM only affects this one process; nothing on the machine is changed.
setlocal
REM Chinese console defaults to codepage 936; force UTF-8 so the
REM script's Chinese output is readable.
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || where py >nul 2>nul
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
if "%~1"=="" (
  echo Usage: probe.bat TOKEN WSPORT PEER_QQ
  echo Example: probe.bat mkx-123456 9721 654321
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0probe.ps1" -Token "%~1" -WsPort "%~2" -Peer "%~3"
echo.
pause
"""


INSTALL_PS1 = r"""# 在 guest 上运行：离线安装依赖并自检。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
# 这个文件由 tools/make_bundle.py 生成。
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
Write-Host "=== markovcryptor QQ 连通性测试包 ===" -ForegroundColor Cyan

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
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
& $py -m pip install --no-index --find-links "$here\wheels" --upgrade `
  numpy pillow reedsolo cryptography `
  fastapi uvicorn python-multipart websockets markovcryptor
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
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
param(
  [string]$Snowluma = "$PSScriptRoot\snowluma",
  [int]$WsPort = 9721,
  [int]$TimeoutSec = 90
)
$ErrorActionPreference = "Stop"

if (-not (Test-Path (Join-Path $Snowluma "index.mjs"))) {
  Write-Host "找不到 $Snowluma\index.mjs" -ForegroundColor Red; exit 1
}

$env:SNOWLUMA_HOOK_AUTOLOAD = "1"     # 自动挂载已登录的 QQ
$log = Join-Path $Snowluma "logs\qqtest.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

Write-Host "启动 SnowLuma（自动 hook 已登录 QQ）…" -ForegroundColor Cyan
Write-Host "日志：$log"
$proc = Start-Process -FilePath "node" -ArgumentList "index.mjs" `
  -WorkingDirectory $Snowluma -PassThru -NoNewWindow `
  -RedirectStandardOutput $log -RedirectStandardError "$log.err"

for ($i = 0; $i -lt $TimeoutSec; $i++) {
  Start-Sleep -Seconds 1
  if ($proc.HasExited) {
    Write-Host "进程已退出（code=$($proc.ExitCode)），看日志 $log" -ForegroundColor Red
    Get-Content $log -Tail 20
    exit 1
  }
  $open = Test-NetConnection -ComputerName 127.0.0.1 -Port $WsPort -InformationLevel Quiet -WarningAction SilentlyContinue
  if ($open) {
    Write-Host "就绪：OneBot WS 127.0.0.1:$WsPort（PID $($proc.Id)）" -ForegroundColor Green
    Write-Host "WebUI: http://127.0.0.1:5099/"
    exit 0
  }
}
Write-Host "超时：$WsPort 没起来，看日志 $log" -ForegroundColor Red
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


PROBE_PS1 = r"""# 发一张图，验 OneBot 通不通。由 tools/make_bundle.py 生成。
# 用法：.\probe.ps1 -Token mkx-<UIN> -WsPort 9721 -Peer <对端QQ号>
param(
  [Parameter(Mandatory = $true)][string]$Token,
  [int]$WsPort = 9721,
  [Parameter(Mandatory = $true)][long]$Peer
)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

$code = @'
import asyncio, base64, hashlib, io, json, sys
sys.stdout.reconfigure(encoding="utf-8")   # 中文输出，别依赖控制台代码页
import numpy as np
from PIL import Image
from markovcryptor.onebot_ws import connect

def png(size, seed):
    arr = np.random.default_rng(seed).integers(0, 256, (size, size, 3), dtype=np.uint8)
    b = io.BytesIO(); Image.fromarray(arr, "RGB").save(b, "PNG"); return b.getvalue()

async def main():
    port, token, peer = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
    blob = png(200, 3)
    print("发送 %d bytes md5=%s" % (len(blob), hashlib.md5(blob).hexdigest()))
    async with connect("ws://127.0.0.1:%d/" % port, token) as ws:
        await ws.send(json.dumps({"action": "send_private_msg", "echo": "p",
            "params": {"user_id": peer, "message": [{"type": "image",
            "data": {"file": "base64://" + base64.b64encode(blob).decode()}}]}}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
            if m.get("echo") == "p":
                if m.get("status") != "ok":
                    print("发送失败:", m); return 1
                print("已发出:", m.get("data")); return 0

sys.exit(asyncio.run(main()) or 0)
'@
$tmp = Join-Path $env:TEMP "mkx_probe.py"
$code | Out-File -Encoding utf8 $tmp
& python $tmp $WsPort $Token $Peer
exit $LASTEXITCODE
"""


CHECKLIST = r"""# QQ 跨机连通性测试 · 操作清单

本包由 `tools/make_bundle.py` 生成。目标是**本机与虚拟机各挂一个 QQ，
互相收发混淆数据**，验真实网络下的连通性。

---

## 一、两边都要准备的东西

| 项目 | 说明 |
|---|---|
| Windows | guest 是 Win10，本机不限 |
| **Python 3.11+** | 包里 `wheels/` 是离线依赖，但 Python 本体要自己装 |
| **Node 18+** | SnowLuma 需要 |
| **QQ 客户端** | 已登录，且两个号**互为好友** |
| SnowLuma | 本包 `snowluma/`（若打包时加了 `--with-snowluma`） |

> 两个 QQ 必须互为好友，否则 `send_private_msg` 会失败或消息不投递。

---

## 二、端口约定

装之前先把表填好，两边**不能冲突**（同一台机器上冲突；两台机器各自独立）。

| | 本机（宿主机） | 虚拟机（Win10） |
|---|---|---|
| QQ 号 | ________________ | ________________ |
| OneBot WS | 9711 | 9721 |
| OneBot HTTP | 9712 | 9722 |
| SnowLuma WebUI | 5099 | 5099 |
| markovcryptor 端口 | 8731 | 8731 |

之前实测确认的两件事：

1. **一个 SnowLuma 只挂一个号时不用改端口**；同一台机器挂两个号才需要分开
   （会 `EADDRINUSE`）。
2. **虚拟机和宿主机是两个系统**，所以 5099 / 8731 两边相同也没关系。

---

## 三、guest（Win10 虚拟机）步骤

1. 把整个 `qqtest-*` 目录拷进虚拟机（共享文件夹、U 盘都行）。
2. 确认 guest 上装好 **Python 3.11+**（`python --version`）和 **Node 18+**
   （`node --version`）。
3. 在 PowerShell 里：
   ```powershell
   cd <拷进去的目录>
   .\install.ps1
   ```
   预期最后一行：`imports ok, webui 5xxxx bytes`。
4. **首次启动要先点同意**（重要）：
   干净的 SnowLuma 在没有 `config/consent.json` 时会停在
   「awaiting EULA/PRIVACY consent」，**此时 QQ hook 和 OneBot 端口都不会起**。

   先正常启一次，看日志里给出的初始密码：
   ```powershell
   .\run-snowluma.ps1 -WsPort 9721     # 这次会超时，是预期的
   Get-Content .\snowluma\logs\qqtest.log | Select-String "initial credentials"
   ```
   浏览器开 http://127.0.0.1:5099/ ，用 `admin` + 那个密码登录，
   同意 EULA，再改一个自己的密码。**这一步只需做一次**，之后
   `config/consent.json` 与 `config/webui.json` 都会留下，重启不再问。
5. 配 SnowLuma（`<UIN>` 换成 guest 上登录的号）：
   ```powershell
   python -m markovcryptor.cli qq-init --snowluma .\snowluma --uin <UIN> --ws-port 9721 --http-port 9722
   ```
6. 起 SnowLuma：
   ```powershell
   .\run-snowluma.ps1 -WsPort 9721
   ```
   预期：`就绪：OneBot WS 127.0.0.1:9721`。

   双击 `run-snowluma.bat` 也行（端口默认 9721）。

---

## 四、本机步骤

1. 配 SnowLuma（用本机那个号的 UIN）：
   ```bash
   python tools/qq_setup.py init --snowluma D:/Projects/SnowLuma-v1.14.22-win-x64-lite --a-uin <本机UIN>
   python tools/qq_setup.py up --snowluma D:/Projects/SnowLuma-v1.14.22-win-x64-lite
   ```
   本机 `qq_setup init` 默认给 a 号 9711。

2. 起终端页：
   ```bash
   python -m markovcryptor.cli serve
   ```
   浏览器开 http://127.0.0.1:8731/ → 终端页。

---

## 五、连通性怎么验（分三层，逐层排除）

**不要一上来就测隐写**。哪层断了就停在那层。

### 第 1 层：QQ 本身通不通

guest 上给宿主机那个号发一句普通文字。收到了 → 好友关系、网络、QQ 都正常。

### 第 2 层：OneBot 能不能发消息

在 guest 本机跑，不需要跨机网络：

```powershell
.\probe.ps1 -Token mkx-<guest的UIN> -WsPort 9721 -Peer <宿主机QQ号>
```

预期 `已发出: {'message_id': ...}`。这同时验了三件事：端口通、token 对、
`send_private_msg` 有权限。

### 第 3 层：图片能不能原样往返（P0）

这是像素域隐写的前提。需要**宿主机能连到 guest 的 9721**（见第六节）。

在宿主机跑：

```bash
python tools/qq_probe.py `
  --a-uin <本机UIN>  --a-ws ws://127.0.0.1:9711/ --a-token mkx-<本机UIN> `
  --b-uin <guest的UIN> --b-ws ws://<guest IP>:9721/ --b-token mkx-<guestUIN>
```

预期：

```
#0 单图     PNG (200, 200)  120366B md5=dacefe…  ✓ 逐字节一致
结论：QQ 原样往返图片，像素域隐写（DCT-QIM）在 QQ 上可用。
```

**跨机之后必须重跑这一步**：之前的一致性结论是同一台机器两个号之间得的，
中间现在多了一条虚拟机网络路径。如果这里变成「被改动」，说明中间有环节
重编码了图片。

> 不想改网络也行：把这条命令反过来在 guest 上跑，`--a-ws` 指向 guest 自己的
> `127.0.0.1:9721`，`--b-ws` 指向宿主机的 9711。

---

## 六、虚拟机网络怎么配（最容易卡在这）

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
python -m markovcryptor.cli qq-init --snowluma .\snowluma --uin <UIN> ``
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

## 七、常见失败与判断

| 现象 | 原因 | 处理 |
|---|---|---|
| `ConnectionRefused` | SnowLuma 没起 / 端口不对 | 看 `snowluma\logs\qqtest.log` |
| `HTTP 401` | token 不匹配 | token 就是 `mkx-<UIN>`，核对 UIN |
| 连上了但收不到消息 | 好友关系 / 发的是群聊 | 确认两号互为好友，确认私聊 |
| 没有 `login detected` | QQ 没登录 / hook 失败 | 手动登录 QQ，重启 SnowLuma |
| 卡在 `awaiting EULA/PRIVACY consent` | 首次运行没同意 | 看第三节第 4 步，登录 WebUI 点同意 |
| WebUI 密码不知道 | 全新目录会生成随机密码 | 在日志里搜 `initial credentials` |
| `EADDRINUSE` | 同机两号端口相同 | 每号一份 `onebot_<UIN>.json`，端口分开 |
| 图片「被改动」 | 中间有重编码 | 改走中文信道，或换文件传输 |
| `markovcryptor` 找不到 | 没跑 install.ps1 | 先跑 `install.bat` |
| `禁止运行脚本` / `UnauthorizedAccess` | Win10 默认执行策略 | 双击 `.bat`，或加 `-ExecutionPolicy Bypass` |
| `.ps1` 报 `缺少实际参数` / 中文乱码 | 脚本没 BOM（旧包） | 用 0.3.0 之后重新打的包 |
| `python` 不是命令 | 没勾 Add to PATH | 用 `py` 代替 |

---

## 八、测完收工

```powershell
# guest：只结束我们起的 Node，不动 QQ
Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
  Where-Object { $_.CommandLine -match 'index\.mjs' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# 本机
python tools/qq_setup.py down
```

**不要**按进程名杀 QQ（`Stop-Process -Name QQ`）—— 会关掉你正在用的 QQ。
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
                       ("install.bat", INSTALL_BAT),
                       ("run-snowluma.bat", RUN_BAT),
                       ("probe.bat", PROBE_BAT),
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
