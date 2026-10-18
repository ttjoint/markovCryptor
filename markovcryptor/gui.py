from __future__ import annotations

from datetime import datetime
import shutil
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, Callable

from .codec import CodecStats, MarkovStegoCodec, decode_with_models
from .model import (
    MarkovModel,
    default_model_dir,
    discover_corpus_files,
    discover_model_paths,
    read_text_file,
    unique_model_path,
)
from .core.progress import Cancelled, ProgressFn, stage
from .punctuation import OPEN_TO_CLOSE

_POLL_MS = 100


def _format_count(value: object) -> str:
    return f"{value:,}" if isinstance(value, int) else "—"


def _format_bytes(value: object) -> str:
    if not isinstance(value, int):
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "—"


def _format_time(value: object) -> str:
    if not isinstance(value, str):
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def _stats_line(stats: CodecStats) -> str:
    unit = "字" if stats.payload_characters is not None else "字节"
    source = stats.payload_characters if stats.payload_characters is not None else stats.payload_bytes
    return (
        f"{_format_count(source)} {unit} → 压缩（{stats.method}）{_format_count(stats.compressed_bytes)} 字节 → "
        f"{_format_count(stats.hanzi_characters)} 个汉字（含标点 {_format_count(stats.cover_characters)} 字），"
        f"长度约为原文 {stats.expansion_ratio:.2f} 倍，{stats.bits_per_hanzi:.2f} bit/汉字"
    )


def _punctuation_lines(model: MarkovModel) -> list[str]:
    """Describe the punctuation layer's corpus statistics for the details pane."""
    punctuation = model.punctuation
    if punctuation is None:
        return ["标点统计：无（编码结果只有汉字）"]
    summary = punctuation.summary()
    top = "  ".join(
        f"{token.replace(chr(10), '↵')}×{_format_count(count)}" for token, count in punctuation.token_counts()[:16]
    )
    spans = "；".join(
        f"{opener}…{OPEN_TO_CLOSE[opener]} 平均 {total / count:.1f} 字（{_format_count(count)} 次）"
        for opener, (count, total) in sorted(punctuation.bracket_spans.items(), key=lambda item: -item[1][0])
        if count
    )
    return [
        f"标点统计：{summary['token_kinds']} 种标点组合；断句点 {_format_count(summary['boundaries'])} 个",
        f"  平均分句长度 {summary['mean_clause_length']:.1f} 个汉字；每句 {summary['mean_sentence_clauses']:.1f} 个分句；"
        f"每段 {summary['mean_paragraph_sentences']:.1f} 句",
        f"  常用标点（↵ 为换行）：{top or '—'}",
        f"  引号/括号平均长度：{spans or '—'}",
    ]


class _ModelEntry:
    """A loaded model together with the file it came from."""

    def __init__(self, path: Path, model: MarkovModel) -> None:
        self.path = path
        self.model = model

    @property
    def label(self) -> str:
        return f"{self.model.name or self.path.stem}  [{self.model.model_id}]"

    @property
    def sources(self) -> list[dict]:
        sources = self.model.metadata.get("sources")
        return sources if isinstance(sources, list) else []

    @property
    def source_names(self) -> str:
        names = [str(source.get("name", "?")) for source in self.sources if isinstance(source, dict)]
        if not names:
            return "—"
        return "、".join(names[:3]) + (f" 等 {len(names)} 个" if len(names) > 3 else "")

    @property
    def corpus_bytes(self) -> int | None:
        sizes = [source.get("bytes") for source in self.sources if isinstance(source, dict)]
        return sum(sizes) if sizes and all(isinstance(size, int) for size in sizes) else None


class MarkovCryptorApp(tk.Tk):
    """Friendly desktop UI for model training and text/file stego operations."""

    def __init__(self) -> None:
        super().__init__()
        self.title("MarkovCryptor · 中文伪装隐写")
        self.geometry("1180x800")
        self.minsize(940, 640)
        self.entries: list[_ModelEntry] = []
        self.current: _ModelEntry | None = None
        self.model_dir = default_model_dir()
        self.aes_enabled = tk.BooleanVar(value=False)
        self.password_var = tk.StringVar()
        self.status_var = tk.StringVar(value="正在查找本地模型…")
        self.model_choice_var = tk.StringVar()
        self.task_var = tk.StringVar(value="")
        self._busy_widgets: list[ttk.Widget] = []
        self._task_active = False
        self._task_cancel = False
        self._task_state: tuple[float, str] = (0.0, "")
        self._task_result: tuple[str, Any] | None = None
        self._build_ui()
        self._load_models()

    # ----------------------------------------------------------------- layout

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self, padding=(16, 12, 16, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="MarkovCryptor", font=("Microsoft YaHei UI", 16, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text="语料文本压缩 + 一阶马尔可夫链算术编码 · 统计伪装工具（保密性请勾选 AES）",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 8))
        ttk.Label(header, text="编码使用模型：").grid(row=0, column=1, sticky="e", padx=(20, 4))
        self.model_combo = ttk.Combobox(header, textvariable=self.model_choice_var, state="readonly", width=46)
        self.model_combo.grid(row=0, column=2, sticky="ew")
        self.model_combo.bind("<<ComboboxSelected>>", self._select_from_combo)
        ttk.Label(header, textvariable=self.status_var, foreground="#555555").grid(
            row=2, column=0, columnspan=3, sticky="w"
        )

        security = ttk.Frame(header)
        security.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Checkbutton(security, text="前置 AES-256-GCM", variable=self.aes_enabled, command=self._toggle_password).pack(side="left")
        ttk.Label(security, text="密码：").pack(side="left", padx=(12, 3))
        self.password_entry = ttk.Entry(security, textvariable=self.password_var, show="•", width=24, state="disabled")
        self.password_entry.pack(side="left")
        ttk.Label(security, text="（先压缩再加密；密码只用于本次操作，不写入模型）", foreground="#666666").pack(
            side="left", padx=8
        )

        task = ttk.Frame(header)
        task.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        task.columnconfigure(0, weight=1)
        self.task_bar = ttk.Progressbar(task, mode="determinate", maximum=100)
        self.task_bar.grid(row=0, column=0, sticky="ew")
        self.cancel_button = ttk.Button(task, text="取消", command=self._cancel_task, state="disabled")
        self.cancel_button.grid(row=0, column=1, padx=(8, 0))
        ttk.Label(task, textvariable=self.task_var, foreground="#555555", width=36).grid(
            row=0, column=2, sticky="w", padx=(8, 0)
        )

        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.notebook.add(self._build_text_tab(self.notebook), text="文本模式")
        self.notebook.add(self._build_file_tab(self.notebook), text="文件模式")
        self.notebook.add(self._build_models_tab(self.notebook), text="模型管理")

    def _build_text_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=12)
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(1, weight=1)
        tab.rowconfigure(4, weight=1)

        ttk.Label(tab, text="输入明文（UTF-8）").grid(row=0, column=0, sticky="w")
        ttk.Label(tab, text="中文伪装文本（可粘贴解码，自动识别模型）").grid(row=0, column=1, sticky="w")
        self.plain_text = ScrolledText(tab, wrap=tk.WORD, undo=True, font=("Microsoft YaHei UI", 11))
        self.cover_text = ScrolledText(tab, wrap=tk.WORD, undo=True, font=("Microsoft YaHei UI", 11))
        self.plain_text.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(4, 8))
        self.cover_text.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(4, 8))

        actions = ttk.Frame(tab)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        encode_button = ttk.Button(actions, text="明文 → 伪装文本", command=self._encode_text)
        encode_button.pack(side="left")
        decode_button = ttk.Button(actions, text="伪装文本 → 明文", command=self._decode_text)
        decode_button.pack(side="left", padx=8)
        clear_button = ttk.Button(actions, text="清空", command=self._clear_text)
        clear_button.pack(side="right")
        self._busy_widgets += [encode_button, decode_button, clear_button]

        ttk.Label(tab, text="最近一次解码结果").grid(row=3, column=0, columnspan=2, sticky="w")
        self.decoded_text = ScrolledText(tab, wrap=tk.WORD, height=7, font=("Microsoft YaHei UI", 11))
        self.decoded_text.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(4, 0))
        self.decoded_text.configure(state="disabled")
        return tab

    def _build_file_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=18)
        tab.columnconfigure(1, weight=1)

        ttk.Label(tab, text="输入文件").grid(row=0, column=0, sticky="w", pady=8)
        self.file_input_var = tk.StringVar()
        ttk.Entry(tab, textvariable=self.file_input_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(tab, text="选择…", command=self._choose_file_input).grid(row=0, column=2)

        ttk.Label(tab, text="输出文件").grid(row=1, column=0, sticky="w", pady=8)
        self.file_output_var = tk.StringVar()
        ttk.Entry(tab, textvariable=self.file_output_var).grid(row=1, column=1, sticky="ew", padx=8)
        ttk.Button(tab, text="另存为…", command=self._choose_file_output).grid(row=1, column=2)

        hint = (
            "编码：输入任意文件 → 自动选择最佳压缩（中文文本用语料模型）→ 用顶部选中的模型输出 UTF-8 中文伪装文本。\n"
            "解码：输入由本工具生成的伪装文本 → 自动识别所用模型并恢复原始文件。\n"
            "输出路径留空时，会在输入文件旁自动生成对应文件名。进度显示在窗口顶部，可随时取消。"
        )
        ttk.Label(tab, text=hint, foreground="#555555", justify="left").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(16, 20)
        )
        buttons = ttk.Frame(tab)
        buttons.grid(row=3, column=0, columnspan=3, sticky="w")
        encode_button = ttk.Button(buttons, text="文件编码为伪装文本", command=self._encode_file)
        encode_button.pack(side="left")
        decode_button = ttk.Button(buttons, text="伪装文本解码为文件", command=self._decode_file)
        decode_button.pack(side="left", padx=10)
        self._busy_widgets += [encode_button, decode_button]
        return tab

    _COLUMNS = (
        ("name", "模型名称", 150),
        ("id", "识别码", 100),
        ("sources", "训练数据文件", 200),
        ("size", "数据集规模", 90),
        ("tokens", "有效字数", 90),
        ("vocab", "词表", 60),
        ("smoothing", "回退", 50),
        ("created", "训练时间", 130),
        ("file", "模型文件", 160),
    )

    def _build_models_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=12)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(tab)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.train_button = ttk.Button(toolbar, text="训练新模型…", command=self._train_model)
        self.train_button.pack(side="left")
        self._busy_widgets.append(self.train_button)
        ttk.Button(toolbar, text="导入模型文件…", command=self._import_models).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="设为编码模型", command=self._use_selected).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="删除", command=self._delete_selected).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="重新扫描", command=self._load_models).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="打开模型文件夹", command=self._open_model_dir).pack(side="right")

        table = ttk.Frame(tab)
        table.grid(row=1, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.model_tree = ttk.Treeview(
            table, columns=[key for key, _, _ in self._COLUMNS], show="headings", selectmode="browse"
        )
        for key, title, width in self._COLUMNS:
            self.model_tree.heading(key, text=title)
            anchor = "e" if key in ("size", "tokens", "vocab", "smoothing") else "w"
            self.model_tree.column(key, width=width, anchor=anchor, stretch=key in ("sources", "file"))
        self.model_tree.tag_configure("current", background="#e3f0ff")
        self.model_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.model_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.model_tree.configure(yscrollcommand=scroll.set)
        self.model_tree.bind("<<TreeviewSelect>>", lambda _: self._show_details())
        self.model_tree.bind("<Double-1>", lambda _: self._use_selected())

        ttk.Label(tab, text="模型详情（双击列表行可设为编码模型）").grid(row=2, column=0, sticky="w", pady=(10, 4))
        self.details_text = ScrolledText(tab, wrap=tk.WORD, height=9, font=("Microsoft YaHei UI", 10))
        self.details_text.grid(row=3, column=0, sticky="nsew")
        self.details_text.configure(state="disabled")
        return tab

    # ---------------------------------------------------------- model registry

    def _load_models(self, select: Path | None = None) -> None:
        previous = select or (self.current.path if self.current else None)
        entries: list[_ModelEntry] = []
        failures: list[str] = []
        seen: dict[bytes, Path] = {}
        try:
            paths = discover_model_paths([self.model_dir]) if self.model_dir.is_dir() else []
        except ValueError:
            paths = []
        for path in paths:
            try:
                model = MarkovModel.load(path)
            except Exception as exc:
                failures.append(f"{path.name}（{exc}）")
                continue
            if model.fingerprint in seen:
                failures.append(f"{path.name}（与 {seen[model.fingerprint].name} 完全相同，已跳过）")
                continue
            seen[model.fingerprint] = path
            entries.append(_ModelEntry(path, model))
        self.entries = entries
        self.current = next((entry for entry in entries if entry.path == previous), None)
        if self.current is None and entries:
            self.current = entries[0]
        self._refresh_model_views()
        if not entries:
            self.status_var.set("尚未找到本地模型，请在“模型管理”页训练或导入模型；模型保存在 models 文件夹。")
            self.notebook.select(2)
        else:
            message = f"已加载 {len(entries)} 个模型；编码使用：{self.current.label}。"
            if failures:
                message += f" 跳过 {len(failures)} 个文件：" + "；".join(failures[:2])
            self.status_var.set(message)

    def _refresh_model_views(self) -> None:
        self.model_combo.configure(values=[entry.label for entry in self.entries])
        self.model_choice_var.set(self.current.label if self.current else "")
        self.model_tree.delete(*self.model_tree.get_children())
        for index, entry in enumerate(self.entries):
            model, meta = entry.model, entry.model.metadata
            self.model_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    ("★ " if entry is self.current else "") + (model.name or entry.path.stem),
                    model.model_id,
                    entry.source_names,
                    _format_bytes(entry.corpus_bytes),
                    _format_count(model.training_tokens),
                    len(model.vocabulary),
                    f"{model.smoothing:g}",
                    _format_time(meta.get("created_at")),
                    entry.path.name,
                ),
                tags=("current",) if entry is self.current else (),
            )
        if self.current is not None:
            iid = str(self.entries.index(self.current))
            self.model_tree.selection_set(iid)
            self.model_tree.see(iid)
        self._show_details()

    def _selected_entry(self) -> _ModelEntry | None:
        selection = self.model_tree.selection()
        return self.entries[int(selection[0])] if selection else None

    def _show_details(self) -> None:
        entry = self._selected_entry()
        lines: list[str] = []
        if entry is not None:
            model, meta = entry.model, entry.model.metadata
            lines += [
                f"名称：{model.name or '（未命名）'}",
                f"识别码：{model.model_id}（完整指纹 {model.fingerprint.hex()}）",
                f"模型文件：{entry.path}",
                f"训练时间：{_format_time(meta.get('created_at'))}"
                + (f"，耗时 {meta['training_seconds']} 秒" if "training_seconds" in meta else ""),
                f"语料总字符：{_format_count(meta.get('corpus_characters'))}；"
                f"汉字：{_format_count(meta.get('hanzi_characters'))}；"
                f"不同汉字：{_format_count(meta.get('distinct_characters'))}",
                f"有效训练字数（词表内）：{_format_count(model.training_tokens)}",
                f"词表：{len(model.vocabulary)} 个汉字（请求 {meta.get('requested_vocabulary_size', '—')}）；"
                f"回退强度：{model.smoothing:g}",
                f"转移上下文：{_format_count(model.context_count)}；非零转移对：{_format_count(model.transition_pairs)}",
                f"高频汉字：{''.join(model.vocabulary[:40])}",
            ]
            if model.text is not None:
                lines.append(
                    f"压缩用字符模型：{_format_count(len(model.text.alphabet))} 个字符，"
                    f"{_format_count(model.text.pair_count)} 个二元组"
                )
            lines += _punctuation_lines(model)
            if entry.sources:
                lines.append(f"训练数据文件（{len(entry.sources)} 个，合计 {_format_bytes(entry.corpus_bytes)}）：")
                lines += [
                    f"  · {source.get('name', '?')}  {_format_bytes(source.get('bytes'))}，"
                    f"{_format_count(source.get('characters'))} 字符"
                    for source in entry.sources
                    if isinstance(source, dict)
                ]
            else:
                lines.append("训练数据文件：未记录。")
        self.details_text.configure(state="normal")
        self.details_text.delete("1.0", tk.END)
        self.details_text.insert("1.0", "\n".join(lines))
        self.details_text.configure(state="disabled")

    def _set_current(self, entry: _ModelEntry) -> None:
        self.current = entry
        self._refresh_model_views()
        self.status_var.set(f"编码模型已切换为：{entry.label}")

    def _select_from_combo(self, _event: tk.Event | None = None) -> None:
        index = self.model_combo.current()
        if 0 <= index < len(self.entries):
            self._set_current(self.entries[index])

    def _use_selected(self) -> None:
        entry = self._selected_entry()
        if entry is not None:
            self._set_current(entry)

    def _delete_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        if not messagebox.askyesno(
            "删除模型",
            f"确定删除模型“{entry.label}”？\n\n文件：{entry.path}\n\n"
            "删除后，用该模型生成的伪装文本将无法再解码。",
            icon="warning",
        ):
            return
        try:
            entry.path.unlink()
        except OSError as exc:
            messagebox.showerror("删除失败", str(exc))
            return
        if self.current is entry:
            self.current = None
        self._load_models()

    def _import_models(self) -> None:
        chosen = filedialog.askopenfilenames(
            title="选择模型 JSON（可多选）", filetypes=[("模型文件", "*.json"), ("所有文件", "*.*")]
        )
        if not chosen:
            return
        known = {entry.model.fingerprint: entry for entry in self.entries}
        imported: list[Path] = []
        problems: list[str] = []
        for raw in chosen:
            source = Path(raw)
            try:
                model = MarkovModel.load(source)
            except Exception as exc:
                problems.append(f"{source.name}：无法读取（{exc}）")
                continue
            if model.fingerprint in known:
                problems.append(f"{source.name}：与已有模型“{known[model.fingerprint].label}”相同")
                continue
            target = unique_model_path(self.model_dir, model.name or source.stem)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            known[model.fingerprint] = _ModelEntry(target, model)
            imported.append(target)
        self._load_models(select=imported[-1] if imported else None)
        summary = f"已导入 {len(imported)} 个模型到：\n{self.model_dir}"
        if problems:
            summary += "\n\n未导入：\n" + "\n".join(problems)
        messagebox.showinfo("导入模型", summary)

    def _open_model_dir(self) -> None:
        import os

        self.model_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(self.model_dir)  # type: ignore[attr-defined]
        except AttributeError:
            messagebox.showinfo("模型文件夹", str(self.model_dir))

    # --------------------------------------------------------------- training

    def _train_model(self) -> None:
        corpus_paths = filedialog.askopenfilenames(
            title="选择中文语料文件（可多选）",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if corpus_paths:
            sources = [Path(path) for path in corpus_paths]
        else:
            corpus_directory = filedialog.askdirectory(title="或选择包含多个 TXT 的语料文件夹")
            if not corpus_directory:
                return
            sources = [Path(corpus_directory)]
        try:
            files = discover_corpus_files(sources)
        except (OSError, ValueError) as exc:
            messagebox.showerror("语料无效", str(exc))
            return
        default_name = files[0].stem if len(files) == 1 else f"{files[0].stem} 等 {len(files)} 个文件"
        dialog = _TrainingOptions(self, default_name, len(files))
        self.wait_window(dialog)
        if dialog.result is None:
            return
        name, vocabulary_size, smoothing, split = dialog.result
        model_dir = self.model_dir

        def work(progress: ProgressFn) -> list[Path]:
            training = stage(progress, 0.0, 0.95)
            if split:
                trained = MarkovModel.train_many(
                    files, vocabulary_size=vocabulary_size, smoothing=smoothing, progress=training
                )
                models = [model for _, model in trained]
            else:
                models = [
                    MarkovModel.train_from_files(
                        files, vocabulary_size=vocabulary_size, smoothing=smoothing, name=name, progress=training
                    )
                ]
            saved: list[Path] = []
            for number, model in enumerate(models):
                progress(0.95 + 0.05 * number / len(models), f"保存模型 {model.name}")
                path = unique_model_path(model_dir, model.name)
                model.save(path)
                saved.append(path)
            return saved

        self._run_task(f"训练模型（{len(files)} 个语料文件）", work, self._training_finished, "训练失败")

    def _training_finished(self, paths: list[Path]) -> None:
        self._load_models(select=paths[-1])
        listing = "\n".join(str(path) for path in paths)
        self.status_var.set(f"训练完成：已保存 {len(paths)} 个模型，并设为编码模型：{self.current.label if self.current else ''}")
        messagebox.showinfo("模型已保存", f"训练完成，共 {len(paths)} 个模型：\n\n{listing}")

    # ------------------------------------------------------------ background tasks

    def _run_task(
        self,
        label: str,
        work: Callable[[ProgressFn], Any],
        on_done: Callable[[Any], None],
        error_title: str,
    ) -> None:
        """Run ``work`` on a worker thread with a determinate progress bar and a cancel button.

        The worker only writes plain attributes; the Tk thread polls them, so no
        Tk call ever happens off the main thread.
        """
        if self._task_active:
            messagebox.showinfo("请稍候", "已有任务正在进行，请等待完成或先取消。")
            return
        self._task_active = True
        self._task_cancel = False
        self._task_state = (0.0, label)
        self._task_result = None
        for widget in self._busy_widgets:
            widget.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status_var.set(f"{label}…")

        def progress(fraction: float, message: str) -> None:
            if self._task_cancel:
                raise Cancelled("操作已取消")
            self._task_state = (fraction, message)

        def worker() -> None:
            try:
                self._task_result = ("ok", work(progress))
            except BaseException as exc:  # reported on the Tk thread
                self._task_result = ("error", exc)

        threading.Thread(target=worker, daemon=True).start()
        self.after(_POLL_MS, lambda: self._poll_task(label, on_done, error_title))

    def _poll_task(self, label: str, on_done: Callable[[Any], None], error_title: str) -> None:
        fraction, message = self._task_state
        self.task_bar["value"] = fraction * 100
        self.task_var.set(f"{fraction * 100:.0f}%  {message}")
        if self._task_result is None:
            self.after(_POLL_MS, lambda: self._poll_task(label, on_done, error_title))
            return
        kind, value = self._task_result
        self._task_active = False
        for widget in self._busy_widgets:
            widget.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.task_bar["value"] = 100 if kind == "ok" else 0
        self.task_var.set("完成" if kind == "ok" else "")
        if kind == "ok":
            on_done(value)
        elif isinstance(value, Cancelled):
            self.status_var.set(f"{label}：已取消。")
        else:
            self.status_var.set(f"{error_title}：{value}")
            messagebox.showerror(error_title, str(value))

    def _cancel_task(self) -> None:
        if self._task_active:
            self._task_cancel = True
            self.task_var.set("正在取消…")

    # ----------------------------------------------------------- encode/decode

    def _require_codec(self) -> MarkovStegoCodec | None:
        if self.current is None:
            messagebox.showwarning("尚未加载模型", "请先在“模型管理”页训练或导入一个中文语料模型。")
            return None
        return MarkovStegoCodec(self.current.model)

    def _require_models(self) -> list[MarkovModel] | None:
        if not self.entries:
            messagebox.showwarning("尚未加载模型", "请先在“模型管理”页训练或导入一个中文语料模型。")
            return None
        return [entry.model for entry in self.entries]

    def _label_for(self, model: MarkovModel) -> str:
        return next((entry.label for entry in self.entries if entry.model is model), model.model_id)

    def _toggle_password(self) -> None:
        self.password_entry.configure(state="normal" if self.aes_enabled.get() else "disabled")

    def _password(self) -> str | None:
        if not self.aes_enabled.get():
            return None
        if not self.password_var.get():
            raise ValueError("启用 AES 后必须输入密码")
        return self.password_var.get()

    def _encode_text(self) -> None:
        codec = self._require_codec()
        if codec is None:
            return
        password = self._password_or_warn()
        if password is False:
            return
        plain = self.plain_text.get("1.0", "end-1c").encode("utf-8")
        label = self._label_for(codec.model)

        def work(progress: ProgressFn) -> tuple[str, CodecStats]:
            return codec.encode_with_stats(plain, password, progress=progress)

        def done(result: tuple[str, CodecStats]) -> None:
            cover, stats = result
            self.cover_text.delete("1.0", tk.END)
            self.cover_text.insert("1.0", cover)
            self.status_var.set(f"文本编码完成（模型 {label}）：{_stats_line(stats)}。")

        self._run_task("文本编码", work, done, "文本编码失败")

    def _decode_text(self) -> None:
        models = self._require_models()
        if models is None:
            return
        password = self._password_or_warn()
        if password is False:
            return
        cover = self.cover_text.get("1.0", "end-1c")

        def work(progress: ProgressFn) -> tuple[str, MarkovModel]:
            payload, selected = decode_with_models(cover, models, password, progress)
            try:
                return payload.decode("utf-8"), selected
            except UnicodeDecodeError as exc:
                raise ValueError("恢复的数据不是有效 UTF-8；如果原始输入是二进制文件，请使用“文件模式”。") from exc

        def done(result: tuple[str, MarkovModel]) -> None:
            plain, selected = result
            self.decoded_text.configure(state="normal")
            self.decoded_text.delete("1.0", tk.END)
            self.decoded_text.insert("1.0", plain)
            self.decoded_text.configure(state="disabled")
            self.status_var.set(f"文本解码完成：自动识别为模型 {self._label_for(selected)}，恢复 {len(plain)} 个字符。")

        self._run_task("文本解码", work, done, "文本解码失败")

    def _clear_text(self) -> None:
        for widget in (self.plain_text, self.cover_text):
            widget.delete("1.0", tk.END)
        self.decoded_text.configure(state="normal")
        self.decoded_text.delete("1.0", tk.END)
        self.decoded_text.configure(state="disabled")
        self.status_var.set("文本区域已清空。")

    def _choose_file_input(self) -> None:
        path = filedialog.askopenfilename(title="选择输入文件")
        if path:
            self.file_input_var.set(path)
            if not self.file_output_var.get():
                self.file_output_var.set(str(Path(path).with_name(Path(path).name + ".cover.txt")))

    def _choose_file_output(self) -> None:
        path = filedialog.asksaveasfilename(title="选择输出文件")
        if path:
            self.file_output_var.set(path)

    def _file_paths(self) -> tuple[Path, Path] | None:
        source = Path(self.file_input_var.get().strip())
        target_text = self.file_output_var.get().strip()
        if not source.is_file():
            messagebox.showwarning("文件不存在", "请选择有效的输入文件。")
            return None
        if not target_text:
            target_text = str(source.with_name(source.name + ".cover.txt"))
            self.file_output_var.set(target_text)
        return source, Path(target_text)

    def _file_finished(self, result: tuple[str, str]) -> None:
        self.status_var.set(result[0])
        messagebox.showinfo("操作完成", result[1])

    def _encode_file(self) -> None:
        codec = self._require_codec()
        paths = self._file_paths() if codec is not None else None
        if codec is None or paths is None:
            return
        source, target = paths
        password = self._password_or_warn()
        if password is False:
            return
        label = self._label_for(codec.model)

        def work(progress: ProgressFn) -> tuple[str, str]:
            payload = source.read_bytes()
            cover, stats = codec.encode_with_stats(payload, password, progress=stage(progress, 0.0, 0.98))
            progress(0.98, "写入文件")
            target.write_text(cover, encoding="utf-8", newline="")
            return (
                f"文件编码完成（模型 {label}）：{_stats_line(stats)}。",
                f"伪装文本已保存到：\n{target}\n\n使用模型：{label}\n{_stats_line(stats)}。",
            )

        self._run_task("文件编码", work, self._file_finished, "文件编码失败")

    def _decode_file(self) -> None:
        models = self._require_models()
        paths = self._file_paths() if models is not None else None
        if models is None or paths is None:
            return
        source, target = paths
        password = self._password_or_warn()
        if password is False:
            return

        def work(progress: ProgressFn) -> tuple[str, str]:
            progress(0.0, "读取伪装文本")
            cover = read_text_file(source)
            payload, selected = decode_with_models(cover, models, password, stage(progress, 0.02, 0.98))
            progress(0.98, "写入文件")
            target.write_bytes(payload)
            label = self._label_for(selected)
            return (
                f"文件解码完成：自动识别为模型 {label}，恢复 {len(payload)} 字节。",
                f"恢复文件已保存到：\n{target}\n\n识别出的模型：{label}",
            )

        self._run_task("文件解码", work, self._file_finished, "文件解码失败")

    def _password_or_warn(self) -> str | None | bool:
        """Read the password on the UI thread; returns False after showing a warning."""
        try:
            return self._password()
        except ValueError as exc:
            messagebox.showwarning("缺少密码", str(exc))
            return False


def main() -> None:
    app = MarkovCryptorApp()
    app.mainloop()


class _TrainingOptions(tk.Toplevel):
    def __init__(self, parent: tk.Misc, default_name: str, file_count: int) -> None:
        super().__init__(parent)
        self.title("训练参数")
        self.resizable(False, False)
        self.result: tuple[str, int, float, bool] | None = None
        self.transient(parent)
        self.grab_set()
        body = ttk.Frame(self, padding=16)
        body.grid()
        ttk.Label(body, text=f"已选择 {file_count} 个语料文件", foreground="#555555").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )
        ttk.Label(body, text="模型名称").grid(row=1, column=0, sticky="w", pady=5)
        self.name_entry = ttk.Entry(body, width=28)
        self.name_entry.insert(0, default_name)
        self.name_entry.grid(row=1, column=1, padx=(12, 0), sticky="w")
        ttk.Label(body, text="词表大小（至少 64）").grid(row=2, column=0, sticky="w", pady=5)
        self.vocab_entry = ttk.Entry(body, width=12)
        self.vocab_entry.insert(0, "3000")
        self.vocab_entry.grid(row=2, column=1, padx=(12, 0), sticky="w")
        ttk.Label(body, text="回退强度（默认 1，越大越随机）").grid(row=3, column=0, sticky="w", pady=5)
        self.smoothing_entry = ttk.Entry(body, width=12)
        self.smoothing_entry.insert(0, "1")
        self.smoothing_entry.grid(row=3, column=1, padx=(12, 0), sticky="w")
        self.split_var = tk.BooleanVar(value=False)
        split = ttk.Checkbutton(
            body, text="每个文件单独训练一个模型（以文件名命名）", variable=self.split_var, command=self._toggle_split
        )
        split.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        if file_count < 2:
            split.configure(state="disabled")
        buttons = ttk.Frame(body)
        buttons.grid(row=5, column=0, columnspan=2, pady=(14, 0), sticky="e")
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="开始训练", command=self._submit).pack(side="right", padx=(0, 8))
        self.bind("<Return>", lambda _: self._submit())
        self.bind("<Escape>", lambda _: self.destroy())
        self.name_entry.focus_set()

    def _toggle_split(self) -> None:
        self.name_entry.configure(state="disabled" if self.split_var.get() else "normal")

    def _submit(self) -> None:
        try:
            vocabulary_size = int(self.vocab_entry.get())
            smoothing = float(self.smoothing_entry.get())
            if vocabulary_size < 64 or not smoothing > 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("参数无效", "词表大小必须至少为 64，回退强度必须为正数。", parent=self)
            return
        name = self.name_entry.get().strip()
        if not name and not self.split_var.get():
            messagebox.showwarning("参数无效", "请填写模型名称。", parent=self)
            return
        self.result = (name, vocabulary_size, smoothing, self.split_var.get())
        self.destroy()


if __name__ == "__main__":
    main()
