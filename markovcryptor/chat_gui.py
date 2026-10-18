"""Two-window chat demo: key exchange hidden inside ordinary photos.

Run ``python run_chat.py`` (or ``python -m markovcryptor.chat_gui``).  Two
side-by-side windows represent Alice and Bob.  In each window you can:

* **pick a cover photo** — choose any image from ``data/`` (or browse for your
  own file), see a thumbnail and its usable capacity;
* **发送** — embed this window's ephemeral X25519 public key into the chosen
  photo and hand the stego PNG to the peer window;
* **打开图片…** — load a stego PNG from disk instead, to imitate a real chat
  round trip through the filesystem;
* once both images have crossed, both sides derive the same session keys and
  the message box switches to AES-256-GCM encrypted chat.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import random
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from PIL import Image, ImageTk

from . import gif_stego
from .handshake import HandshakeParty, HandshakeError, seal, open_
from .image_stego import (
    DEFAULT_STEP,
    StegoError,
    check_cover,
    max_payload_bytes,
    plan,
    suggest_size,
    upscale_cover,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
IMAGE_TYPES = [
    ("图片文件", "*.png;*.gif;*.jpg;*.jpeg;*.bmp;*.webp;*.tif;*.tiff"),
    ("PNG（无损，推荐）", "*.png"),
    ("GIF（动图）", "*.gif"),
    ("所有文件", "*.*"),
]
THEME = {
    "alice": {"bg": "#eaf2fb", "accent": "#1565c0", "light": "#d3e3f8"},
    "bob": {"bg": "#fdeef2", "accent": "#ad1457", "light": "#f8d6e1"},
}
THUMB = (340, 150)


def _short(blob: bytes, n: int = 8) -> str:
    return blob[:n].hex() + "…"


class CoverInfo:
    """A selectable cover: where it came from plus its embedding capacity.

    Small images are enlarged proportionally instead of being rejected, so
    almost any picture can be used.  GIF covers keep the original file around
    so the sender can write back an animation rather than a single frame.
    """

    def __init__(self, image: Image.Image, label: str, payload_bytes: int = 32):
        self.original = image
        self.label = label
        self.is_gif = getattr(image, "n_frames", 1) > 1 or image.format == "GIF"
        self.frames = getattr(image, "n_frames", 1)
        # For GIF the first frame decides the geometry and the capacity.
        image.seek(0)
        base = image.convert("RGB")
        scaled, self.original_size, self.resized = upscale_cover(base, payload_bytes)
        self.image = scaled
        self.usable, self.reason = check_cover(self.image, payload_bytes)
        self.info = plan(self.image.size[0], self.image.size[1], payload_bytes)

    @property
    def summary(self) -> str:
        width, height = self.image.size
        if not self.usable:
            return f"不可用：{self.reason}"
        note = ""
        if self.resized:
            note = f" · 已由 {self.original_size[0]}×{self.original_size[1]} 放大"
        kind = f" · GIF {self.frames} 帧（嵌首帧）" if self.is_gif else ""
        return (f"{width}×{height}{kind} · 容量 {self.info['max_payload_bytes']} 字节"
                f" · 需 {self.info['blocks_needed']}/{self.info['blocks_available']} 块"
                f"{note}")


class ChatWindow(tk.Toplevel):
    def __init__(self, master, name: str, psk: str, covers: list[CoverInfo],
                 inbox: list, sender, *, side: str):
        super().__init__(master)
        self.title(f"{name} — MKX1 隐写密钥交换")
        self.geometry("460x760")
        self.minsize(430, 700)
        theme = THEME[side]
        self.bg, self.accent = theme["bg"], theme["accent"]

        self.party = HandshakeParty(name, psk)
        self.covers = covers
        self.inbox = inbox            # list of (from_name, kind, payload)
        self.sender = sender          # callable(window, message)
        self.peer_window: ChatWindow | None = None
        self.session = None
        self.selected: CoverInfo | None = None
        self._thumbs: dict[str, ImageTk.PhotoImage] = {}
        self._preview_ref: ImageTk.PhotoImage | None = None

        self._build_header(name)
        self._build_body(name)
        self._select_default()
        self._poll_inbox()

    # ------------------------------------------------------------------ UI

    def _build_header(self, name: str):
        header = tk.Frame(self, bg=self.accent)
        header.pack(fill="x")
        tk.Label(header, text=name, fg="white", bg=self.accent,
                 font=("Microsoft YaHei", 15, "bold"), padx=14, pady=9).pack(side="left")
        self.status = tk.Label(header, text="未握手", fg="white", bg=self.accent,
                               font=("Microsoft YaHei", 9), padx=14)
        self.status.pack(side="right")

    def _build_body(self, name: str):
        # --- cover picker ---------------------------------------------------
        picker = tk.LabelFrame(self, text=" ① 选择底图 ", bg=self.bg, fg=self.accent,
                               font=("Microsoft YaHei", 9, "bold"), padx=8, pady=6)
        picker.pack(fill="x", padx=10, pady=(10, 6))

        row = tk.Frame(picker, bg=self.bg)
        row.pack(fill="x")
        self.cover_var = tk.StringVar()
        self.combo = ttk.Combobox(row, textvariable=self.cover_var, state="readonly",
                                  values=[c.label for c in self.covers],
                                  font=("Microsoft YaHei", 9))
        self.combo.pack(side="left", fill="x", expand=True)
        self.combo.bind("<<ComboboxSelected>>", lambda _e: self._on_combo())
        ttk.Button(row, text="浏览…", width=7, command=self.browse_cover).pack(side="left", padx=(4, 0))

        self.cover_info = tk.Label(picker, text="", bg=self.bg, fg="#455a64",
                                   font=("Microsoft YaHei", 8), anchor="w", justify="left")
        self.cover_info.pack(fill="x", pady=(4, 0))

        thumb_row = tk.Frame(picker, bg=self.bg)
        thumb_row.pack(fill="x", pady=(4, 0))
        self.cover_thumb = tk.Label(thumb_row, bg="#ffffff", relief="solid", bd=1,
                                    anchor="center")
        self.cover_thumb.pack(anchor="center")

        send_row = tk.Frame(picker, bg=self.bg)
        send_row.pack(fill="x", pady=(6, 0))
        self.send_btn = tk.Button(
            send_row, text="② 发送握手图 →", command=self.send_handshake,
            bg=self.accent, fg="white", activebackground=self.accent,
            activeforeground="white", font=("Microsoft YaHei", 10, "bold"),
            relief="flat", padx=12, pady=5, cursor="hand2")
        self.send_btn.pack(side="left")
        ttk.Button(send_row, text="打开图片…", width=11,
                   command=self.receive_image).pack(side="left", padx=(6, 0))
        self.send_hint = tk.Label(send_row, text="", bg=self.bg, fg="#78909c",
                                  font=("Microsoft YaHei", 8))
        self.send_hint.pack(side="left", padx=(8, 0))

        # --- received image preview ----------------------------------------
        prev = tk.LabelFrame(self, text=" 最近收到的图片 ", bg=self.bg, fg=self.accent,
                             font=("Microsoft YaHei", 9, "bold"), padx=8, pady=6)
        prev.pack(fill="x", padx=10, pady=(0, 6))
        self.preview = tk.Label(prev, bg="#ffffff", relief="solid", bd=1,
                                text="（尚无）", anchor="center",
                                font=("Microsoft YaHei", 8), fg="#90a4ae")
        self.preview.pack(fill="x")

        # --- transcript -----------------------------------------------------
        logf = tk.LabelFrame(self, text=" ③ 会话记录 ", bg=self.bg, fg=self.accent,
                             font=("Microsoft YaHei", 9, "bold"), padx=6, pady=6)
        logf.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.log = scrolledtext.ScrolledText(
            logf, height=12, wrap="word", font=("Consolas", 9),
            bg="#ffffff", relief="flat", state="disabled")
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("me", foreground="#2e7d32")
        self.log.tag_configure("peer", foreground="#6a1b9a")
        self.log.tag_configure("sys", foreground="#78909c")
        self.log.tag_configure("err", foreground="#c62828")

        # --- message entry --------------------------------------------------
        msg_row = tk.Frame(self, bg=self.bg)
        msg_row.pack(fill="x", padx=10, pady=(0, 10))
        self.entry = tk.Entry(msg_row, font=("Microsoft YaHei", 10))
        self.entry.pack(side="left", fill="x", expand=True, ipady=3)
        self.entry.bind("<Return>", lambda _e: self.send_message())
        self.send_msg_btn = tk.Button(
            msg_row, text="加密发送", command=self.send_message, state="disabled",
            bg=self.accent, fg="white", relief="flat", padx=12, pady=3,
            font=("Microsoft YaHei", 9), cursor="hand2")
        self.send_msg_btn.pack(side="left", padx=(6, 0))

        self._append("提示：①②完成互发后才会建立会话密钥。", "sys")

    # ------------------------------------------------------------- covers

    def _select_default(self):
        usable = [c for c in self.covers if c.usable]
        if usable:
            self._select(random.choice(usable))
        elif self.covers:
            self._select(self.covers[0])

    def _select(self, cover: CoverInfo):
        self.selected = cover
        self.cover_var.set(cover.label)
        self.cover_info.configure(text=cover.summary)
        thumb = cover.image.copy()
        thumb.thumbnail(THUMB)
        photo = ImageTk.PhotoImage(thumb)
        self._thumbs[cover.label] = photo
        self.cover_thumb.configure(image=photo, text="")
        if cover.usable:
            self.send_btn.configure(state="normal", bg=self.accent)
            self.send_hint.configure(text="PNG 无损，盲提取最稳")
        else:
            self.send_btn.configure(state="disabled", bg="#b0bec5")
            self.send_hint.configure(text="该图不可用，请换一张")

    def _on_combo(self):
        label = self.cover_var.get()
        for cover in self.covers:
            if cover.label == label:
                self._select(cover)
                return

    def browse_cover(self):
        path = filedialog.askopenfilename(
            parent=self, title="选择底图（覆盖原图）", filetypes=IMAGE_TYPES)
        if not path:
            return
        try:
            image = Image.open(path).convert("RGB")
        except Exception as exc:
            messagebox.showerror("打不开图片", str(exc), parent=self)
            return
        cover = CoverInfo(image, f"{Path(path).name}（自定义）")
        self.covers.append(cover)
        self.combo.configure(values=[c.label for c in self.covers])
        self._select(cover)

    # ------------------------------------------------------------- helpers

    def _poll_inbox(self):
        while self.inbox:
            from_name, kind, payload = self.inbox.pop(0)
            try:
                if kind == "image":
                    self._on_image(from_name, payload)
                elif kind == "gif":
                    self._on_gif(from_name, payload)
                else:
                    self._on_message(from_name, payload)
            except Exception as exc:
                self._append(f"✗ 处理消息出错：{exc}", "err")
        self.after(150, self._poll_inbox)

    def _append(self, text: str, tag: str = "sys"):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.configure(state="disabled")
        self.log.see("end")

    def _show_preview(self, image: Image.Image, caption: str):
        thumb = image.copy()
        thumb.thumbnail((THUMB[0], 150))
        photo = ImageTk.PhotoImage(thumb)
        self._preview_ref = photo
        self.preview.configure(image=photo, text=caption, compound="bottom",
                               fg="#37474f")

    # ------------------------------------------------------------ handshake

    def send_handshake(self):
        if self.selected is None:
            messagebox.showinfo("请选择底图", "先从下拉框里挑一张图片。", parent=self)
            return
        if not self.selected.usable:
            messagebox.showwarning("底图不可用", self.selected.reason, parent=self)
            return
        cover = self.selected.image
        if self.selected.is_gif:
            self._send_gif_handshake()
            return
        try:
            stego, rep = self.party.make_stego_image(cover, step=DEFAULT_STEP)
        except Exception as exc:
            messagebox.showerror("嵌入失败", str(exc), parent=self)
            return
        self._append(f"[我] 用《{self.selected.label}》生成握手图 "
                     f"（载体 {rep['carrier']}，{rep['blocks_needed']}/{rep['blocks_reserved']} 块，"
                     f"最大块改动 {rep['max_block_change']:.1f}）", "me")
        self._append(f"     我的公钥：{_short(self.party.public_raw, 10)}", "sys")
        self._show_preview(stego, "已发送（含隐藏公钥）")
        self.sender(self, stego)

    def _send_gif_handshake(self):
        """Embed into frame 0 of an animated GIF and send the animation."""
        frame_bytes = self.party.build_handshake_frame()
        try:
            data, rep = gif_stego.embed_gif(
                self.selected.original, frame_bytes, self.party.psk)
        except Exception as exc:
            messagebox.showerror("GIF 嵌入失败", str(exc), parent=self)
            return
        self._append(
            f"[我] 用动图《{self.selected.label}》生成握手 GIF "
            f"（{rep['frames']} 帧，嵌入第 {rep['embedded_frames'][0]} 帧，"
            f"QIM step {rep['step']:.0f}，加载通道 {rep['carriers'][0]}，"
            f"{len(data) / 1024:.0f} KB）", "me")
        self._append(f"     我的公钥：{_short(self.party.public_raw, 10)}", "sys")
        self._show_preview(Image.open(BytesIO(data)), "已发送动图（含隐藏公钥）")
        self.sender(self, ("gif", data))

    def _on_image(self, from_name: str, image: Image.Image):
        self._append(f"[{from_name}] → 收到图片，开始盲提取…", "peer")
        try:
            peer_key = self.party.receive_stego_image(image)
        except StegoError as exc:
            self._append(f"     ✗ 提取失败：{exc}", "err")
            messagebox.showwarning("提取失败", str(exc), parent=self)
            return
        self._show_preview(image, f"来自 {from_name}（已提取 32 B 公钥）")
        self._append(f"     ✓ 提取到对端公钥：{_short(peer_key, 10)}", "peer")
        self._maybe_finish(from_name)

    def _on_gif(self, from_name: str, data: bytes):
        self._append(f"[{from_name}] → 收到动图 {len(data) / 1024:.0f} KB，盲提取首帧…", "peer")
        try:
            payload, info = gif_stego.extract_gif(data, self.party.psk)
        except StegoError as exc:
            self._append(f"     ✗ 提取失败：{exc}", "err")
            messagebox.showwarning("提取失败", str(exc), parent=self)
            return
        try:
            peer_key = self.party.receive_payload(payload)
        except HandshakeError as exc:
            self._append(f"     ✗ 公钥异常：{exc}", "err")
            return
        self._show_preview(Image.open(BytesIO(data)), f"来自 {from_name}（动图，已提取公钥）")
        self._append(f"     ✓ 从第 {info['frame_index']} 帧（共 {info['gif_frames']} 帧）"
                     f"提取到公钥：{_short(peer_key, 10)}", "peer")
        self._maybe_finish(from_name)

    def receive_image(self, path: str | None = None):
        """Load a stego image from disk instead of the in-process channel."""
        if path is None:
            path = filedialog.askopenfilename(
                parent=self, title="选择收到的图片（含隐藏公钥）", filetypes=IMAGE_TYPES)
            if not path:
                return
        try:
            probe = Image.open(path)
            is_gif = probe.format == "GIF" and getattr(probe, "n_frames", 1) > 1
            image = probe.convert("RGB")
        except Exception as exc:
            messagebox.showerror("打不开图片", str(exc), parent=self)
            return
        if is_gif:
            with open(path, "rb") as handle:
                self._append(f"[文件] 打开动图 {Path(path).name}", "sys")
                self._on_gif(f"文件 {Path(path).name}", handle.read())
            return
        self._append(f"[文件] 打开 {Path(path).name}", "sys")
        self._on_image(f"文件 {Path(path).name}", image)

    def _maybe_finish(self, from_name: str):
        if self.party.peer_public is None:
            return
        if self.session is not None:
            return
        try:
            self.session = self.party.derive_session()
        except HandshakeError as exc:
            self._append(f"     ✗ 派生失败：{exc}", "err")
            return
        self.status.configure(text=f"✓ 已与 {from_name} 建立会话")
        self._append("     ✓ 握手完成，会话密钥已派生：", "peer")
        self._append(f"        发送键 {_short(self.session.send_key, 12)}", "sys")
        self._append(f"        接收键 {_short(self.session.recv_key, 12)}", "sys")
        self.send_msg_btn.configure(state="normal")
        self.send_hint.configure(text="会话已建立")

    # -------------------------------------------------------------- messages

    def send_message(self):
        text = self.entry.get().strip()
        if not text:
            return
        if self.session is None:
            messagebox.showinfo("尚未握手", "请先双方互发握手图建立会话。", parent=self)
            return
        nonce, ciphertext = seal(self.session.send_key, text.encode("utf-8"))
        self.entry.delete(0, "end")
        self._append(f"[我] {text}", "me")
        self._append(f"     ↳ AES-GCM 密文 {len(ciphertext)} B：{ciphertext[:12].hex()}…", "sys")
        self.sender(self, ("msg", (nonce, ciphertext)))

    def _on_message(self, from_name: str, payload):
        if self.session is None:
            self._append(f"[{from_name}] （密文，但本地尚未建立会话）", "err")
            return
        nonce, ciphertext = payload
        try:
            plain = open_(self.session.recv_key, nonce, ciphertext).decode("utf-8")
        except Exception:
            self._append(f"[{from_name}] （解密失败）", "err")
            return
        self._append(f"[{from_name}] {plain}", "peer")


class ChatApp(tk.Tk):
    def __init__(self, psk: str = "共享口令-123"):
        super().__init__()
        self.title("MKX1 隐写密钥交换演示 — 双窗口")
        self.withdraw()
        self.psk = psk
        covers = self._load_covers()
        self.alice = ChatWindow(self, "Alice", psk, covers, [],
                                sender=self._route, side="alice")
        self.bob = ChatWindow(self, "Bob", psk, covers, [],
                              sender=self._route, side="bob")
        self.alice.peer_window, self.bob.peer_window = self.bob, self.alice
        for win in (self.alice, self.bob):
            win.protocol("WM_DELETE_WINDOW", self._quit)
        self.after(50, self._place)

    def _place(self):
        self.alice.geometry("460x760+60+60")
        self.bob.geometry("460x760+560+60")

    def _load_covers(self) -> list[CoverInfo]:
        covers: list[CoverInfo] = []
        if DATA_DIR.exists():
            def key(path: Path):
                stem = path.stem
                return (0, int(stem), "") if stem.isdigit() else (1, 0, stem)

            for path in sorted(DATA_DIR.glob("*.png"), key=key):
                try:
                    covers.append(CoverInfo(Image.open(path).convert("RGB"), path.name))
                except Exception:
                    continue
            # Animated GIFs: kept as the original file so the sender can write
            # the animation back out, with the payload in frame 0 only.
            gif_dir = DATA_DIR / "gif"
            if gif_dir.is_dir():
                for path in sorted(gif_dir.glob("*.gif"), key=lambda p: p.name.lower()):
                    try:
                        gif = Image.open(path)
                        gif.seek(0)
                        covers.append(CoverInfo(gif, f"gif/{path.name}"))
                    except Exception:
                        continue
        if not covers:
            covers = [CoverInfo(Image.new("RGB", (512, 512), (180, 190, 200)), "内置占位图")]
        return covers

    def _route(self, window: ChatWindow, message):
        peer = window.peer_window
        if peer is None:
            return
        if isinstance(message, Image.Image):
            peer.inbox.append((window.party.name, "image", message.copy()))
        elif isinstance(message, tuple) and message and message[0] == "gif":
            peer.inbox.append((window.party.name, "gif", message[1]))
        else:
            kind, payload = message
            peer.inbox.append((window.party.name, kind, payload))

    def _quit(self):
        for win in (self.alice, self.bob):
            try:
                win.destroy()
            except tk.TclError:
                pass
        self.destroy()


def main():
    app = ChatApp()
    app.mainloop()


if __name__ == "__main__":
    main()
