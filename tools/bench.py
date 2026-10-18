"""Large-scale batch evaluation of the MKX1 image / GIF channels.

Run it directly for a capacity + success-rate sweep over a picture library::

    python -m tools.bench --root "D:/Pictures/表情包" --limit 500

The module deliberately keeps all reporting in plain data structures so the
same numbers can be reused by tests or written to CSV.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image

from markovcryptor import gif_stego, image_stego

#: Extensions we attempt.  Everything is decoded through Pillow.
STILL_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".avif"}
GIF_SUFFIXES = {".gif"}

#: A fixed 32-byte payload (an X25519 public key) for handshake-shaped runs.
HANDSHAKE_PAYLOAD = bytes(range(32))


@dataclass
class Cover:
    """One decodable picture from the library."""

    path: Path
    width: int
    height: int
    mode: str
    frames: int
    bytes: int
    palette_colors: int = 0
    error: str = ""


@dataclass
class Result:
    """Outcome of one embed/extract attempt."""

    path: str
    kind: str
    width: int
    height: int
    payload: int
    ok: bool = False
    stage: str = ""
    detail: str = ""
    step: float = 0.0
    carrier: str = ""
    max_block_change: float = 0.0
    resized: bool = False
    gif_bytes_before: int = 0
    gif_bytes_after: int = 0
    seconds: float = 0.0
    colors: int = 0
    frames: int = 1
    lead_profile: int = 0


@dataclass
class Summary:
    total: int = 0
    ok: int = 0
    failures: list[Result] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.ok / self.total if self.total else 0.0


# --------------------------------------------------------------------- survey


def survey(root: Path, limit: int | None = None,
           kinds: tuple[str, ...] = ("still", "gif")) -> list[Cover]:
    """List decodable pictures under ``root`` without loading full pixels."""
    found: list[Cover] = []
    wanted = set()
    if "still" in kinds:
        wanted |= STILL_SUFFIXES
    if "gif" in kinds:
        wanted |= GIF_SUFFIXES

    for dirpath, _dirnames, filenames in os.walk(root):
        for name in sorted(filenames):
            suffix = Path(name).suffix.lower()
            if suffix not in wanted:
                continue
            path = Path(dirpath) / name
            try:
                size = path.stat().st_size
                with Image.open(path) as image:
                    cover = Cover(
                        path=path,
                        width=image.width,
                        height=image.height,
                        mode=image.mode,
                        frames=getattr(image, "n_frames", 1),
                        bytes=size,
                    )
                    if cover.frames > 1 and image.mode == "P":
                        image.seek(0)
                        palette = image.getpalette() or []
                        cover.palette_colors = len(palette) // 3
            except Exception as exc:              # noqa: BLE001 - report, don't crash
                continue
            found.append(cover)
            if limit is not None and len(found) >= limit:
                return found
    return found


# ------------------------------------------------------------------ execution


def run_still(cover: Cover, payload: bytes, key: bytes) -> Result:
    result = Result(path=str(cover.path), kind="still", width=cover.width,
                    height=cover.height, payload=len(payload),
                    colors=cover.palette_colors, frames=cover.frames)
    started = time.perf_counter()
    try:
        with Image.open(cover.path) as image:
            rgb = image.convert("RGB")
    except Exception as exc:                      # noqa: BLE001
        result.stage, result.detail = "decode", str(exc)[:160]
        return result

    scaled, _original, resized = image_stego.upscale_cover(rgb, len(payload))
    result.resized = resized
    result.width, result.height = scaled.size

    usable, reason = image_stego.check_cover(scaled, len(payload))
    if not usable:
        result.stage, result.detail = "capacity", reason
        return result

    frame = image_stego.build_frame(payload)
    try:
        stego, report = image_stego.embed(scaled, frame, key)
    except image_stego.StegoError as exc:
        result.stage, result.detail = "embed", str(exc)[:160]
        return result
    result.step = report["step"]
    result.carrier = report["carrier"]
    result.max_block_change = report["max_block_change"]
    result.lead_profile = report.get("lead_profile", 0)

    # Round-trip through the medium the user would actually send: PNG.
    from io import BytesIO

    buffer = BytesIO()
    stego.save(buffer, "PNG")
    buffer.seek(0)
    with Image.open(buffer) as reloaded:
        try:
            got, _info = image_stego.try_extract(reloaded, key)
        except image_stego.StegoError as exc:
            result.stage, result.detail = "extract", str(exc)[:160]
            return result
    if got != payload:
        result.stage, result.detail = "mismatch", "提取结果与原文不符"
        return result
    result.ok = True
    result.seconds = time.perf_counter() - started
    return result


def run_gif(cover: Cover, payload: bytes, key: bytes) -> Result:
    result = Result(path=str(cover.path), kind="gif", width=cover.width,
                    height=cover.height, payload=len(payload),
                    colors=cover.palette_colors, frames=cover.frames)
    started = time.perf_counter()
    try:
        with Image.open(cover.path) as image:
            data, report = gif_stego.embed_gif(
                image, image_stego.build_frame(payload), key)
    except image_stego.StegoError as exc:
        result.stage, result.detail = "embed", str(exc)[:160]
        return result
    except Exception as exc:                      # noqa: BLE001
        result.stage, result.detail = "decode", str(exc)[:160]
        return result

    result.step = report["step"]
    result.carrier = report["carriers"][0]
    result.max_block_change = report["max_block_change"]
    result.lead_profile = report.get("lead_profile", 0)
    result.resized = bool(report.get("resized"))
    result.gif_bytes_before = cover.bytes
    result.gif_bytes_after = len(data)
    try:
        got, _info = gif_stego.extract_gif(data, key)
    except image_stego.StegoError as exc:
        result.stage, result.detail = "extract", str(exc)[:160]
        return result
    if got != payload:
        result.stage, result.detail = "mismatch", "提取结果与原文不符"
        return result
    result.ok = True
    result.seconds = time.perf_counter() - started
    return result


# -------------------------------------------------------------------- commands


def command_survey(args) -> int:
    covers = survey(Path(args.root), args.limit)
    stills = [c for c in covers if Path(c.path).suffix.lower() in STILL_SUFFIXES]
    gifs = [c for c in covers if Path(c.path).suffix.lower() in GIF_SUFFIXES]
    print(f"可解码总数 {len(covers)}（静态 {len(stills)} · GIF {len(gifs)}）")
    for label, group in (("静态", stills), ("GIF", gifs)):
        if not group:
            continue
        sides = sorted(min(c.width, c.height) for c in group)
        areas = sorted(c.width * c.height for c in group)
        caps = sorted(image_stego.max_payload_bytes(c.width, c.height) for c in group)
        print(f"\n[{label}]")
        print(f"  短边   min {sides[0]} / 中位 {sides[len(sides) // 2]} / max {sides[-1]}")
        print(f"  像素   min {areas[0]} / 中位 {areas[len(areas) // 2]} / max {areas[-1]}")
        print(f"  容量   min {caps[0]} B / 中位 {caps[len(caps) // 2]} B / max {caps[-1]} B")
        print(f"  够放32B公钥: {sum(1 for c in caps if c >= 32)}/{len(caps)}")
        if label == "GIF":
            frames = sorted(c.frames for c in group)
            print(f"  帧数   min {frames[0]} / 中位 {frames[len(frames) // 2]} / max {frames[-1]}")
    return 0


def command_bench(args) -> int:
    root = Path(args.root)
    covers = survey(root, args.limit)
    key = image_stego.normalise_key(args.key)
    payload = bytes(range(min(args.payload, 32))) * (args.payload // 32 or 1)
    payload = payload[: args.payload]
    if not payload:
        payload = HANDSHAKE_PAYLOAD

    if args.shuffle:
        rng = random.Random(args.seed)
        rng.shuffle(covers)

    stills = [c for c in covers if Path(c.path).suffix.lower() in STILL_SUFFIXES]
    gifs = [c for c in covers if Path(c.path).suffix.lower() in GIF_SUFFIXES]
    if args.max_stills:
        stills = stills[: args.max_stills]
    if args.max_gifs:
        gifs = gifs[: args.max_gifs]

    results: list[Result] = []
    for index, cover in enumerate(stills, 1):
        result = run_still(cover, payload, key)
        results.append(result)
        if index % 50 == 0 or index == len(stills):
            print(f"  静态 {index}/{len(stills)} …", flush=True)
    for index, cover in enumerate(gifs, 1):
        result = run_gif(cover, payload, key)
        results.append(result)
        if index % 25 == 0 or index == len(gifs):
            print(f"  GIF  {index}/{len(gifs)} …", flush=True)

    report(results, args.out)
    return 0


def report(results: list[Result], out: Path | None) -> None:
    if not results:
        print("没有可测试的图片")
        return
    for kind in ("still", "gif"):
        group = [r for r in results if r.kind == kind]
        if not group:
            continue
        ok = [r for r in group if r.ok]
        print(f"\n=== {kind} ===")
        print(f"  成功 {len(ok)}/{len(group)}  ({len(ok) / len(group):.1%})")
        if ok:
            changes = sorted(r.max_block_change for r in ok)
            times = sorted(r.seconds for r in ok)
            steps = sorted({r.step for r in ok})
            print(f"  最大块改动 中位 {changes[len(changes) // 2]:.1f} / max {changes[-1]:.1f}")
            print(f"  耗时       中位 {times[len(times) // 2]:.2f}s / max {times[-1]:.2f}s")
            print(f"  QIM step   使用过的值 {steps}")
            carriers: dict[str, int] = {}
            for r in ok:
                carriers[r.carrier] = carriers.get(r.carrier, 0) + 1
            print(f"  载体分布   {carriers}")
            profiles: dict[int, int] = {}
            for r in ok:
                profiles[r.lead_profile] = profiles.get(r.lead_profile, 0) + 1
            print(f"  引导头档位 {profiles}")
            print(f"  需放大     {sum(1 for r in ok if r.resized)}")
            if kind == "gif":
                deltas = sorted(
                    (r.gif_bytes_after - r.gif_bytes_before) / r.gif_bytes_before
                    for r in ok if r.gif_bytes_before
                )
                if deltas:
                    print(f"  体积变化   中位 {deltas[len(deltas) // 2]:+.1%} / "
                          f"min {deltas[0]:+.1%} / max {deltas[-1]:+.1%}")
        failures = [r for r in group if not r.ok]
        if failures:
            buckets: dict[str, int] = {}
            for r in failures:
                buckets[f"{r.stage}"] = buckets.get(r.stage, 0) + 1
            print(f"  失败分类   {buckets}")
            for r in failures[:12]:
                name = Path(r.path).name
                print(f"    · {name} [{r.width}x{r.height}] {r.stage}: {r.detail}")
            if len(failures) > 12:
                print(f"    … 另有 {len(failures) - 12} 条")

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(asdict(results[0])))
            writer.writeheader()
            for r in results:
                writer.writerow(asdict(r))
        print(f"\n明细已写入 {out}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MKX1 图片信道批量评测")
    parser.add_argument("--root", default="D:/Pictures/表情包")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--payload", type=int, default=32)
    parser.add_argument("--key", default="bench-key")
    parser.add_argument("--max-stills", type=int, default=0)
    parser.add_argument("--max-gifs", type=int, default=0)
    parser.add_argument("--shuffle", action="store_true")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--survey-only", action="store_true")
    args = parser.parse_args(argv)
    if args.survey_only:
        return command_survey(args)
    return command_bench(args)


if __name__ == "__main__":
    sys.exit(main())
