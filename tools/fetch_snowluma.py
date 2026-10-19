"""下载 SnowLuma release 到 vendor/（不进版本库）。

本库**不打包** SnowLuma，因为：

1. 它的 ``native/`` 目录是平台相关的（Windows 是 hook QQ 客户端的 DLL），
   换平台要换另一个 release 包；
2. 它有自己的 EULA，塞进本库分发会混淆许可边界；
3. 它升级频繁，绑死版本会让本库跟着过时。

所以只用脚本按平台拉对应 release，装到 ``vendor/snowluma/``，
``.gitignore`` 已排除整个 ``vendor/``。

用法：

    python tools/fetch_snowluma.py                 # 拉最新版
    python tools/fetch_snowluma.py --tag v1.14.22  # 钉死版本
    python tools/fetch_snowluma.py --list          # 只列出可用的 release

拉完照 ``python tools/qq_setup.py init/up/probe`` 走。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO = "SnowLuma/SnowLuma"
VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "snowluma"


def _platform_tag() -> str:
    """按当前系统选 release 资产的关键词。"""
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    if system == "windows":
        return f"win-{arch}"
    if system == "darwin":
        return f"mac-{arch}"
    if system == "linux":
        return f"linux-{arch}"
    raise SystemExit(f"不支持的平台：{system}/{machine}")


def _api(path: str) -> object:
    url = f"https://api.github.com/repos/{REPO}/{path}"
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "markovcryptor-fetch",
        **({"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"}
           if os.environ.get("GITHUB_TOKEN") else {}),
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _releases() -> list[dict]:
    return _api("releases?per_page=30")            # type: ignore[return-value]


def _pick(release: dict, tag: str) -> dict:
    """从 release 里挑出匹配当前平台的 zip。

    release 里有 `lite`（无 GUI，体积小）和完整版；隐写终端只需要 OneBot 服务，
    优先 lite。
    """
    assets = release.get("assets") or []
    matching = [a for a in assets
                if tag in a["name"].lower() and a["name"].lower().endswith(".zip")]
    if not matching:
        names = ", ".join(a["name"] for a in assets) or "（没有资产）"
        raise SystemExit(f"{release.get('tag_name')} 里没有 {tag} 的 zip：{names}")
    matching.sort(key=lambda a: ("lite" not in a["name"].lower(), a["size"]))
    return matching[0]


def cmd_list(args: argparse.Namespace) -> int:
    print(f"当前平台关键词：{_platform_tag()}")
    for release in _releases()[: args.limit]:
        tag = release.get("tag_name", "?")
        names = [a["name"] for a in release.get("assets") or []]
        hit = [n for n in names if args.arch in n.lower()]
        print(f"  {tag}  {len(names)} 个资产，匹配 {hit or '无'}")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    arch = _platform_tag()
    releases = _releases()
    if not releases:
        raise SystemExit("GitHub 上没有 release")
    release = releases[0]
    if args.tag:
        release = next((r for r in releases if r.get("tag_name") == args.tag), None)
        if release is None:
            raise SystemExit(f"找不到 {args.tag}")

    asset = _pick(release, arch)
    print(f"下载 {asset['name']} ({asset['size'] / 1048576:.1f} MB) "
          f"来自 {release['tag_name']}")

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
        archive = Path(handle.name)
    try:
        request = urllib.request.Request(asset["browser_download_url"],
                                         headers={"User-Agent": "markovcryptor-fetch"})
        with urllib.request.urlopen(request, timeout=180) as response, \
                archive.open("wb") as target:
            shutil.copyfileobj(response, target, length=1 << 20)

        if VENDOR.exists():
            shutil.rmtree(VENDOR)
        VENDOR.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(VENDOR)
    finally:
        archive.unlink(missing_ok=True)

    print(f"解压到 {VENDOR}")
    entry = VENDOR / "index.mjs"
    if not entry.is_file():
        # 有些包多套一层目录，提示一下而不是猜。
        inner = list(VENDOR.glob("*/index.mjs"))
        if inner:
            print(f"注意：入口在 {inner[0].parent}，用 --snowluma 指向那一层")
        else:
            print("注意：没找到 index.mjs，检查 release 结构", file=sys.stderr)
            return 1
    print(f"下一步：python tools/qq_setup.py init --snowluma {VENDOR}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="下载并解压（默认动作）")
    fetch.add_argument("--tag", help="指定版本，如 v1.14.22")
    fetch.set_defaults(func=cmd_fetch)

    listing = sub.add_parser("list", help="列出可用 release")
    listing.add_argument("--limit", type=int, default=10)
    listing.add_argument("--arch", default=_platform_tag())
    listing.set_defaults(func=cmd_list)

    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0].startswith("-"):
        argv = ["fetch", *argv]
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
