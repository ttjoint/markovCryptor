from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from .channels import Carrier, CarrierKind
from .codec import MarkovStegoCodec
from .core.errors import StegoError
from .engine import Config, Engine
from .jobs import ANIMATION_SUFFIXES, TEXT_SUFFIXES
from .web import DEFAULT_HOST, DEFAULT_PORT
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
    # Global, before the subcommand: verbose logging is a property of the run,
    # not of any one subcommand, and putting it on each would multiply the flags.
    parser.add_argument("--log-level", default=None,
                        metavar="{DEBUG,INFO,WARNING,ERROR}",
                        help="日志级别，默认 INFO，也可用环境变量 MKX1_LOG_LEVEL")
    parser.add_argument("--log-file", type=Path, default=None,
                        help="日志写到文件而非 stderr（UTF-8，单行 key=value）")

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

    sub.add_parser("channels", help="列出所有可用信道及其参数（供程序与界面调用）")

    probe = sub.add_parser("probe", help="不下笔，先问某个信道能否装下载荷")
    probe.add_argument("--channel", required=True, help="信道 id，见 channels 子命令")
    probe.add_argument("--input", type=Path, required=True, help="载体文件")
    probe.add_argument("--bytes", type=int, required=True, help="载荷字节数")
    probe.add_argument("--model-dir", type=Path, default=default_model_dir())

    serve = sub.add_parser("serve", help="启动本机 Web API（无鉴权，仅监听回环地址）")
    serve.add_argument("--host", default=DEFAULT_HOST, help=f"监听地址，默认 {DEFAULT_HOST}")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT,
                       help=f"端口，默认 {DEFAULT_PORT}（也可用环境变量 MKX1_PORT）")
    serve.add_argument("--model-dir", type=Path, default=default_model_dir())

    batch = sub.add_parser("batch", help="对一整个文件夹批量嵌入或提取")
    batch.add_argument("--channel", required=True, help="信道 id，见 channels 子命令")
    batch.add_argument("--input", type=Path, required=True, help="输入文件夹")
    batch.add_argument("--output", type=Path, required=True, help="输出文件夹")
    batch.add_argument("--action", choices=("embed", "extract"), default="embed")
    batch.add_argument("--payload", type=Path, help="嵌入时每个载体要携带的载荷文件")
    batch.add_argument("--key", default="", help="口令")
    batch.add_argument("--options", default=None, help="信道参数的 JSON 字符串")
    batch.add_argument("--kind", default="image", choices=("image", "animation", "text"))
    batch.add_argument("--workers", type=int, default=4)
    batch.add_argument("--limit", type=int, default=None, help="只处理前 N 个文件")
    batch.add_argument("--on-error", choices=("skip", "abort", "retry"), default="skip")
    batch.add_argument("--resume", type=Path, help="断点文件（JSONL）；再次运行会跳过已完成项")
    batch.add_argument("--no-recursive", action="store_true")
    batch.add_argument("--report", type=Path, help="把逐项结果写成 CSV")
    batch.add_argument("--model-dir", type=Path, default=default_model_dir())
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


def _command_batch(args, progress: "_ConsoleProgress") -> int:
    """Batch embed/extract a folder. Returns a shell exit code."""
    from .jobs import BatchRunner, JobSpec, discover, plan_targets

    if not args.input.is_dir():
        print(f"输入不是一个文件夹：{args.input}", file=sys.stderr)
        return 2
    kind = CarrierKind(args.kind)
    sources = discover(args.input, kind, recursive=not args.no_recursive,
                       limit=args.limit)
    if not sources:
        print(f"{args.input} 下没有 {args.kind} 类型的文件", file=sys.stderr)
        return 2

    payload = b""
    if args.action == "embed":
        if args.payload is None:
            print("批量嵌入需要 --payload 指定载荷文件", file=sys.stderr)
            return 2
        payload = args.payload.read_bytes()

    suffix = ".png" if args.action == "embed" and kind is not CarrierKind.TEXT else ""
    if args.action == "embed" and kind is CarrierKind.ANIMATION:
        suffix = ".gif"
    if args.action == "extract":
        suffix = ".bin" if kind is not CarrierKind.TEXT else ".txt"
    # The output name for an embed is whatever the channel emits; the file suffix
    # is therefore cosmetic, and the channel is free to disagree.
    items = plan_targets(sources, args.output, suffix=suffix)

    engine = Engine(Config(model_dir=args.model_dir))
    runner = BatchRunner(engine, args.resume)
    spec = JobSpec(
        channel_id=args.channel, items=items, key=args.key.encode(),
        options=json.loads(args.options) if args.options else {},
        action=args.action, payload=payload, on_error=args.on_error,
        max_workers=args.workers, kind=kind,
    )

    def on_progress(status) -> None:
        progress(status.progress,
                 f"{status.succeeded} 成功 / {status.failed} 失败 / {status.total}")

    try:
        status = runner.run(spec, on_progress=on_progress)
    except KeyboardInterrupt:
        print(chr(10) + "已中断。加 --resume <断点文件> 可从断点继续。", file=sys.stderr)
        return 130
    progress.done()

    if args.report:
        _write_batch_report(status.results, args.report)

    print(f"{status.state}：{status.succeeded} 成功，{status.failed} 失败，"
          f"共 {status.total} 项，耗时 {status.elapsed:.1f}s")
    failures = [r for r in status.results if not r.ok]
    for result in failures[:10]:
        print(f"  {result.source}: {result.error}")
    if len(failures) > 10:
        print(f"  ...其余 {len(failures) - 10} 项见 --report 输出")
    return 0 if status.state == "done" and not failures else 1


def _write_batch_report(results, path: Path) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["source", "target", "ok", "error_code", "error",
                         "reasons", "hints", "payload_bytes", "elapsed_ms"])
        for r in results:
            writer.writerow([r.source, r.target, r.ok, r.error_code, r.error,
                             " | ".join(r.reasons), " | ".join(r.hints),
                             r.payload_bytes, r.elapsed_ms])


def _carrier_from_file(path: Path) -> Carrier:
    """Wrap a file as a lazy carrier, guessing its kind from the suffix.

    The suffix only selects a *candidate* channel -- the channel itself still
    validates the bytes and names the real problem if they do not match.
    """
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return Carrier(kind=CarrierKind.TEXT,
                       load=lambda: read_text_file(path),
                       size_hint=path.stat().st_size if path.exists() else None,
                       media_type="text/plain", label=path.name)
    kind = CarrierKind.ANIMATION if suffix in ANIMATION_SUFFIXES else CarrierKind.IMAGE
    return Carrier(kind=kind, load=path.read_bytes,
                   size_hint=path.stat().st_size if path.exists() else None,
                   label=path.name)


def _load_all(inputs: list[Path]) -> tuple[list[Path], list[MarkovModel]]:
    paths = discover_model_paths(inputs)
    return paths, [MarkovModel.load(path) for path in paths]


def _describe(model: MarkovModel, path: Path) -> str:
    return f"{model.model_id}  {model.name or path.stem}  ({path})"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    from . import observability

    observability.configure(args.log_level, log_file=args.log_file)
    return _dispatch(args, parser)


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    progress = _ConsoleProgress()

    if args.command == "channels":
        engine = Engine()
        for data in engine.describe_channels():
            print(f"{data['id']:<10} {data['display_name']}")
            print(f"            {data['summary']}")
            print(f"            mime_in={', '.join(data['mime_in'])} -> {data['mime_out']}")
            schema = data["options_schema"] or {}
            for name, spec in (schema.get("properties") or {}).items():
                default = spec.get("default", "-")
                print(f"            - {name}={default}: {spec.get('description', '')}")
        return 0

    if args.command == "probe":
        engine = Engine(Config(model_dir=args.model_dir))
        carrier = _carrier_from_file(args.input)
        feas = engine.probe(args.channel, carrier, args.bytes)
        print(feas)
        for reason in feas.reasons:
            print(f"  原因：{reason}")
        for hint in feas.hints:
            print(f"  建议：{hint}")
        for key, value in feas.cost.items():
            print(f"  {key}: {value}")
        return 0 if feas.ok else 1

    if args.command == "batch":
        return _command_batch(args, progress)

    if args.command == "serve":
        from .web import ApiConfig, run_server

        port = int(os.environ.get("MKX1_PORT") or args.port)
        try:
            run_server(ApiConfig(host=args.host, port=port, model_dir=args.model_dir))
        except StegoError as exc:
            print(exc, file=sys.stderr)
            for hint in exc.hints:
                print(f"  建议：{hint}", file=sys.stderr)
            return 2
        return 0

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
        # Paths are resolved by ``_load_all`` above; injecting the models keeps
        # the engine from re-reading the same files.
        engine = Engine(models=models)
        payload, selected = engine.decode_text(read_text_file(args.input), args.password, progress)
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
        engine = Engine(models=models)
        selected = engine.identify(read_text_file(args.input))
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
