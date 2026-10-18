from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

from .codec import MarkovStegoCodec, decode_with_models, identify_model
from .model import (
    MarkovModel,
    default_model_dir,
    discover_corpus_files,
    discover_model_paths,
    read_text_file,
    unique_model_path,
)
from .core.progress import Cancelled


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="markovcryptor",
        description="语料压缩 + 一阶马尔可夫链算术编码的中文伪装隐写工具（数据只存于汉字，标点由语料统计后加）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    train = sub.add_parser("train", help="从一个或多个中文语料文件/文件夹训练模型")
    train.add_argument(
        "--corpus",
        type=Path,
        nargs="+",
        required=True,
        help="中文语料文件（自动识别 UTF-8/GBK 等编码），可指定多个文件或文件夹（文件夹递归读取 .txt）",
    )
    train.add_argument("--name", help="合并模型的显示名称（默认取语料文件名）")
    train.add_argument("--model", type=Path, help="模型输出 JSON（默认保存到模型文件夹，按名称命名）")
    train.add_argument("--split", action="store_true", help="每个语料文件单独训练一个模型")
    train.add_argument("--model-dir", type=Path, default=default_model_dir(), help="模型文件夹")
    train.add_argument("--vocab-size", type=int, default=3000, help="汉字词表大小（至少 64）")
    train.add_argument("--smoothing", type=float, default=1.0, help="Witten-Bell 回退强度（越大越随机、容量越高）")

    encode = sub.add_parser("encode", help="将二进制文件编码为中文伪装文本")
    encode.add_argument("--model", type=Path, required=True, help="用于编码的单个模型 JSON")
    encode.add_argument("--input", type=Path, required=True)
    encode.add_argument("--output", type=Path, required=True)
    encode.add_argument("--password", help="可选 AES-GCM 密码")
    encode.add_argument("--no-punctuation", action="store_true", help="只输出汉字，不添加标点与换行")

    decode = sub.add_parser("decode", help="从中文伪装文本恢复二进制文件（自动识别模型）")
    decode.add_argument("--model", type=Path, nargs="+", default=[default_model_dir()], help="模型 JSON 或文件夹")
    decode.add_argument("--input", type=Path, required=True)
    decode.add_argument("--output", type=Path, required=True)
    decode.add_argument("--password", help="AES-GCM 密码")

    listing = sub.add_parser("models", help="列出模型及其识别码、训练数据规模、标点统计等信息")
    listing.add_argument("--model", type=Path, nargs="+", default=[default_model_dir()])

    identify = sub.add_parser("identify", help="只读取帧头，判断伪装文本由哪个模型生成")
    identify.add_argument("--model", type=Path, nargs="+", default=[default_model_dir()])
    identify.add_argument("--input", type=Path, required=True)

    demo = sub.add_parser("demo", help="运行内置样例往返")
    demo.add_argument("--model", type=Path, required=True)
    demo.add_argument("--text", default="这是一个用于演示的中文隐写样例。")
    return parser


class _ConsoleProgress:
    """Single-line progress for stderr, redrawn at most ten times a second."""

    def __init__(self) -> None:
        self._last = 0.0

    def __call__(self, fraction: float, message: str) -> None:
        now = time.monotonic()
        if now - self._last < 0.1 and fraction < 1.0:
            return
        self._last = now
        print(f"\r{fraction * 100:5.1f}%  {message[:60]:<60}", end="", file=sys.stderr, flush=True)

    def done(self) -> None:
        print("\r" + " " * 70 + "\r", end="", file=sys.stderr, flush=True)


def _load_all(inputs: list[Path]) -> tuple[list[Path], list[MarkovModel]]:
    paths = discover_model_paths(inputs)
    return paths, [MarkovModel.load(path) for path in paths]


def _describe(model: MarkovModel, path: Path) -> str:
    return f"{model.model_id}  {model.name or path.stem}  ({path})"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    progress = _ConsoleProgress()
    if args.command == "train":
        files = discover_corpus_files(args.corpus)
        if args.split:
            trained = MarkovModel.train_many(files, args.vocab_size, args.smoothing, progress=progress)
            models = [model for _, model in trained]
        else:
            models = [
                MarkovModel.train_from_files(files, args.vocab_size, args.smoothing, name=args.name, progress=progress)
            ]
        progress.done()
        for model in models:
            path = args.model if args.model and not args.split else unique_model_path(args.model_dir, model.name)
            model.save(path)
            print(f"saved {_describe(model, path)}: {len(model.vocabulary)} hanzi, {model.training_tokens} tokens")
        return 0

    if args.command == "encode":
        codec = MarkovStegoCodec(MarkovModel.load(args.model))
        payload = args.input.read_bytes()
        covertext, stats = codec.encode_with_stats(
            payload, args.password, punctuate=not args.no_punctuation, progress=progress
        )
        progress.done()
        args.output.write_text(covertext, encoding="utf-8", newline="")
        print(
            f"encoded {stats.payload_bytes} bytes ({stats.method} -> {stats.compressed_bytes} bytes) -> "
            f"{stats.hanzi_characters} hanzi ({stats.cover_characters} chars with punctuation); "
            f"{stats.bits_per_hanzi:.2f} bits/hanzi; {stats.expansion_ratio:.2f}x length"
        )
        return 0

    if args.command == "decode":
        paths, models = _load_all(args.model)
        payload, selected = decode_with_models(read_text_file(args.input), models, args.password, progress)
        progress.done()
        args.output.write_bytes(payload)
        print(f"decoded {len(payload)} bytes -> {args.output} using {_describe(selected, paths[models.index(selected)])}")
        return 0

    if args.command == "models":
        for path in discover_model_paths(args.model):
            try:
                model = MarkovModel.load(path)
            except (OSError, ValueError, KeyError) as exc:
                print(f"{path}: unreadable ({exc})")
                continue
            meta = model.metadata
            print(_describe(model, path))
            print(
                f"    trained {meta.get('created_at', '-')}; corpus chars {meta.get('corpus_characters', '-')}; "
                f"hanzi {model.training_tokens}; vocab {len(model.vocabulary)}; smoothing {model.smoothing}"
            )
            if model.punctuation is not None:
                summary = model.punctuation.summary()
                print(
                    f"    punctuation: {summary['token_kinds']} kinds; clause {summary['mean_clause_length']:.1f} hanzi; "
                    f"{summary['mean_sentence_clauses']:.1f} clauses/sentence; "
                    f"{summary['mean_paragraph_sentences']:.1f} sentences/paragraph"
                )
            for source in meta.get("sources", []):
                print(f"    - {source.get('name')} ({source.get('bytes')} bytes)")
        return 0

    if args.command == "identify":
        paths, models = _load_all(args.model)
        selected = identify_model(read_text_file(args.input), models)
        if selected is None:
            print("no model matched")
            return 1
        print(_describe(selected, paths[models.index(selected)]))
        return 0

    if args.command == "demo":
        codec = MarkovStegoCodec(MarkovModel.load(args.model))
        payload = args.text.encode("utf-8")
        covertext = codec.encode(payload)
        if codec.decode(covertext) != payload:
            raise RuntimeError("demo round-trip failed")
        print(covertext)
        print(f"round-trip ok; payload={len(payload)} bytes, cover={len(covertext)} chars")
        return 0

    raise AssertionError(f"unknown command {args.command!r}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, Cancelled) as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        raise SystemExit(2)
