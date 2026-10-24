"""发布前逆向风险审计：扫 wheel 与 bundle。

只读，不改任何东西。输出一份人类可读的报告。
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
def _find_wheel() -> Path | None:
    """最新构建出来的 markovcryptor wheel。"""
    candidates = sorted(ROOT.glob("dist/**/markovcryptor-*.whl")) +                  sorted(ROOT.glob("dist/*.whl"))
    return candidates[-1] if candidates else None


WHEEL = _find_wheel()

#: 看起来像"不该出现在发布物里的东西"的模式。
SECRET_PATTERNS = [
    ("私钥字面量", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS key", re.compile(rb"AKIA[0-9A-Z]{16}")),
    ("GitHub token", re.compile(rb"gh[pousr]_[A-Za-z0-9]{36}")),
    ("QQ 号(10位数字串)", re.compile(rb"(?<!\d)[1-9]\d{5,10}(?!\d)")),
    ("Windows 用户路径", re.compile(rb"[Cc]:[\\/]Users[\\/][A-Za-z0-9_.-]+")),
    ("IP 地址", re.compile(rb"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("邮箱", re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
]

#: 明显是"填充/示例"的值，不必报。
BENIGN = {
    b"127.0.0.1", b"0.0.0.0", b"255.255.255.255", b"1.2.3.4",
}


def scan_bytes(name: str, data: bytes) -> list[str]:
    hits = []
    for label, pattern in SECRET_PATTERNS:
        for match in pattern.findall(data):
            if match in BENIGN:
                continue
            snippet = match[:40].decode("utf-8", "replace")
            hits.append(f"{name}: {label} -> {snippet!r}")
    return hits


def main() -> None:
    print("=" * 72)
    print("markovCryptor 发布包逆向风险审计")
    print("=" * 72)

    if not WHEEL.exists():
        print(f"找不到 wheel：{WHEEL}")
        return

    z = zipfile.ZipFile(WHEEL)
    names = z.namelist()

    # ---------- 1. 源码形态 ----------
    py = [n for n in names if n.endswith(".py")]
    pyc = [n for n in names if n.endswith(".pyc")]
    so = [n for n in names if n.endswith((".so", ".pyd", ".dll"))]
    print("\n[1] 源码形态")
    print(f"    .py 明文           {len(py)}")
    print(f"    .pyc 字节码        {len(pyc)}")
    print(f"    .so/.pyd/.dll      {len(so)}")
    total_py = sum(z.getinfo(n).file_size for n in py)
    print(f"    .py 总字节         {total_py:,}")

    # ---------- 2. 敏感字符串 ----------
    print("\n[2] 敏感字符串扫描")
    all_hits: list[str] = []
    for name in names:
        if not name.endswith((".py", ".json", ".txt", ".md", ".cfg", ".ini")):
            continue
        all_hits.extend(scan_bytes(name, z.read(name)))
    if all_hits:
        for hit in all_hits[:40]:
            print("    ⚠ " + hit)
        print(f"    共 {len(all_hits)} 条")
    else:
        print("    未发现")

    # ---------- 3. 默认口令 ----------
    print("\n[3] 默认口令")
    psk_hits = []
    for name in py:
        text = z.read(name).decode("utf-8", "replace")
        if "pbx1" in text:
            for i, line in enumerate(text.split("\n"), 1):
                if "pbx1" in line:
                    psk_hits.append(f"{name}:{i}: {line.strip()[:70]}")
    for hit in psk_hits:
        print("    ⚠ " + hit)
    print(f"    共 {len(psk_hits)} 处")
    print("    （默认口令是**设计上公开**的共享秘密，出现在源码里不算漏洞；")
    print("      真正的风险是用户不换它。UI 已有常驻警告。）")

    # ---------- 4. 运行时数据 ----------
    print("\n[4] 运行时数据是否混进包")
    bad = [n for n in names
           if any(part in n.lower() for part in
                  ("config.json", "peers.json", ".log", "player.db", "/logs/",
                   "/data/", "onebot_"))]
    if bad:
        for n in bad:
            print("    ⚠ " + n)
    else:
        print("    未发现（config/peers/log/db 都不在包内）")

    # ---------- 5. WebUI 暴露 ----------
    print("\n[5] WebUI 是否泄露内部信息")
    ui = [(n, z.read(n).decode("utf-8", "replace"))
          for n in names if "/webui/" in n and n.endswith((".js", ".html", ".css"))]
    print(f"    WebUI 文件 {len(ui)} 个（浏览器端必然可见，本来就是明文）")
    for n, text in ui:
        for pattern, label in [
            (r"console\.log\(['\"](?:DEBUG|TODO)", "遗留调试输出"),
            (r"/client/debug|/__debug", "调试端点"),
            (r"password", "口令字段"),
        ]:
            m = re.search(pattern, text)
            if m:
                print(f"    ⚠ {n}: {label} -> {m.group(0)[:40]}")

    # ---------- 6. 文档是否夹带 ----------
    print("\n[6] 包内文档")
    docs = [n for n in names if n.endswith(".md")]
    for n in docs:
        size = z.getinfo(n).file_size
        print(f"    {n} ({size:,} 字节)")


if __name__ == "__main__":
    main()
