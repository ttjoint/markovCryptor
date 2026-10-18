"""Generate the small carrier fixtures the test suite runs on.

Tests must not depend on ``data/`` (32 MB of photos, never versioned) or on
``D:/Pictures`` — CI has neither.  Everything here is produced from a fixed
seed so the bytes are identical on every machine, and deliberately small so the
whole suite stays fast.

Run this once after checkout, or from CI before the tests:

    python -m tools.make_fixtures

Outputs land in ``tests/fixtures/`` and *are* versioned, so the generation step
is normally unnecessary — it exists so the fixtures can be regenerated
reproducibly when a test needs a different shape.
"""

from __future__ import annotations

import argparse
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

#: Photographic-looking carrier.  Needs enough low-frequency content for
#: DCT-QIM to bite; flat colour is explicitly *not* representative (see the
#: colour-limit tests, which build their own extreme covers).  160 px keeps the
#: PNG near 37 KB while still offering ~180 B of capacity — plenty for tests,
#: and small enough to version.
PHOTO_SIDE = 160


def _smooth_noise(seed: int, width: int, height: int) -> np.ndarray:
    """Blurred noise: random but with the low-frequency energy a photo has.

    Pure white noise has almost no energy in the low DCT band, so QIM writes
    there are dominated by rounding; blurring first makes the fixture behave
    like the real photographs the channel was tuned on.
    """
    rng = np.random.default_rng(seed)
    raw = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
    blurred = Image.fromarray(raw, "RGB").filter(ImageFilter.GaussianBlur(2.0))
    # Blurring compresses contrast toward mid-grey; stretch it back so the
    # fixture keeps a realistic dynamic range.
    array = np.asarray(blurred, dtype=np.float64)
    low, high = array.min(), array.max()
    if high - low > 1:
        array = (array - low) * (255.0 / (high - low))
    return np.clip(array, 0, 255).astype(np.uint8)


def photo(seed: int = 1, side: int = PHOTO_SIDE) -> Image.Image:
    return Image.fromarray(_smooth_noise(seed, side, side), "RGB")


def animation(seed: int = 2, side: int = 120, frames: int = 3) -> bytes:
    """A small GIF whose frames differ, so frame handling is exercised."""
    images = []
    for index in range(frames):
        frame = photo(seed + index, side)
        images.append(frame.quantize(colors=256, method=Image.MEDIANCUT,
                                     dither=Image.NONE))
    buffer = BytesIO()
    images[0].save(buffer, "GIF", save_all=True, append_images=images[1:],
                   duration=[40] * frames, loop=0, optimize=False)
    return buffer.getvalue()


def _write_photos() -> list[Path]:
    written = []
    for name, seed, side in (
        ("photo.png", 1, PHOTO_SIDE),
        ("photo-small.png", 3, 120),      # the smallest usable still cover
        ("photo-tall.png", 4, 0),         # replaced below with a non-square
    ):
        if side:
            image = photo(seed, side)
        else:
            image = Image.fromarray(_smooth_noise(seed, 100, 240), "RGB")
        path = FIXTURES / name
        image.save(path, "PNG")
        written.append(path)
    return written


def _write_jpeg() -> list[Path]:
    """A JPEG source for the "survives q88" test.

    Lossy by construction: this is what the channel actually has to survive on
    a photo taken by a phone.
    """
    path = FIXTURES / "photo.jpg"
    photo(5, 200).save(path, "JPEG", quality=95)
    return [path]


def _write_gif() -> list[Path]:
    path = FIXTURES / "anim.gif"
    path.write_bytes(animation())
    return [path]


def _write_corpus() -> list[Path]:
    """Chinese corpus for training tests.

    Small but with enough bigram variety that the model has real entropy; a
    corpus that is too repetitive makes the arithmetic coder degenerate and the
    encoding tests stop being meaningful.
    """
    sentences = [
        "清晨的山谷里弥漫着薄雾，溪水沿着石头缓缓流下。",
        "他推开木门，看见院子里的老槐树又抽出了新芽。",
        "远处传来钟声，一声接着一声，像是有人在低声诉说。",
        "街角的书店还亮着灯，橱窗里摆着几本旧小说。",
        "风从湖面吹过来，带着水草和泥土的气息。",
        "她坐在窗边写字，笔尖划过纸面发出沙沙的声响。",
        "列车穿过隧道，光线一瞬间照亮了整个车厢。",
        "老人在树下摆开棋盘，围观的人渐渐多了起来。",
        "夜色渐深，城市的灯火一盏一盏地亮起又熄灭。",
        "春天来的时候，田野上开满了细碎的野花。",
        "他翻着手里的旧信，纸张已经泛黄发脆。",
        "雨停了，屋檐上的水珠一颗一颗落进石阶的缝隙。",
        "厨房里飘出饭菜的香味，孩子们围着桌子等候。",
        "山路弯弯曲曲地向上延伸，看不见尽头在哪里。",
        "她把窗帘拉开，阳光铺满了整间屋子。",
        "码头的船陆续靠岸，渔民们开始整理渔网。",
        "图书馆里很安静，只有翻书的声音偶尔响起。",
        "冬天的早晨，河面上结着一层薄薄的冰。",
        "他站在桥上看风景，身后的行人来来往往。",
        "炉火噼啪作响，屋里渐渐暖和起来。",
        "院子里的枣树结了果，压得枝头弯了下来。",
        "琴声从楼上传下来，慢慢地散在夜色里。",
        "集市上人声鼎沸，摊贩们大声地吆喝着。",
        "她把行李放在门口，回头看了一眼熟悉的房间。",
        "田野尽头有一间小屋，屋顶上覆着青苔。",
        "他沿着河岸慢慢走，脚下是松软的沙土。",
        "窗外的雨越下越大，敲在玻璃上啪啪作响。",
        "老人讲起从前的事情，声音低沉而缓慢。",
        "天边泛起鱼肚白，村庄渐渐从睡梦中醒来。",
        "他把书合上，闭上眼睛回想刚才读过的段落。",
    ]
    text = "\n".join(sentences) + "\n"
    # Repeat with variation so token statistics are not degenerate, while
    # keeping the file small enough to version.
    text = (text * 6) + text[::-1]
    path = FIXTURES / "corpus.txt"
    path.write_text(text, encoding="utf-8")
    return [path]


def build(target: Path | None = None) -> list[Path]:
    global FIXTURES
    if target is not None:
        FIXTURES = target
    FIXTURES.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    written += _write_photos()
    written += _write_jpeg()
    written += _write_gif()
    written += _write_corpus()
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成测试夹具")
    parser.add_argument("--out", type=Path, default=None, help="输出目录")
    args = parser.parse_args(argv)

    paths = build(args.out)
    total = sum(p.stat().st_size for p in paths)
    for path in paths:
        print(f"  {path.name:20s} {path.stat().st_size:>7d} B")
    print(f"共 {len(paths)} 个文件，{total / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
