// 浮层：右键菜单 + 看大图。
//
// 屏蔽浏览器默认右键菜单是刻意的：这是 --app 模式的应用窗口，不是网页，
// "重新加载 / 检查 / 另存为 / 翻译"对用户毫无意义，还会让人以为这是个网页。
// 只提供与上下文相关的几项。
//
// 输入框例外 —— 那里的系统菜单（剪切/粘贴/拼写）比我们自己做的更好。

import { toast } from "../core/dom.js";

let ctxEl = null;

export function closeCtx() {
  if (ctxEl) {
    ctxEl.remove();
    ctxEl = null;
  }
}

export function showCtx(x, y, items) {
  closeCtx();
  const menu = document.createElement("div");
  menu.className = "ctxmenu";
  for (const item of items) {
    if (item === "-") {
      const sep = document.createElement("div");
      sep.className = "sep";
      menu.appendChild(sep);
      continue;
    }
    const btn = document.createElement("button");
    btn.textContent = item.label;
    if (item.disabled) btn.disabled = true;
    btn.onclick = () => { closeCtx(); item.run(); };
    menu.appendChild(btn);
  }
  document.body.appendChild(menu);

  // 贴边时翻转，别让菜单跑出窗口。
  const r = menu.getBoundingClientRect();
  const left = x + r.width > window.innerWidth ? x - r.width : x;
  const top = y + r.height > window.innerHeight ? y - r.height : y;
  menu.style.left = Math.max(4, left) + "px";
  menu.style.top = Math.max(4, top) + "px";
  ctxEl = menu;
}

/** 取当前选中的文字。 */
export function selectionText() {
  const sel = window.getSelection();
  return sel ? String(sel).trim() : "";
}

/** 一条消息的右键菜单：复制全文 + 复制密文（有的话）。 */
export function entryMenu(entry) {
  const items = [];
  const plain = (entry.text || "").trim();
  items.push({
    label: "复制消息",
    disabled: !plain,
    run: () => copyText(plain),
  });
  const covert = (entry.covert || []).join("\n").trim();
  if (covert) {
    items.push({ label: "复制密文", run: () => copyText(covert) });
  }
  const sel = selectionText();
  if (sel) {
    items.unshift({ label: "复制选中内容", run: () => copyText(sel) });
    items.splice(1, 0, "-");
  }
  return items;
}

export async function copyText(text) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制");
  } catch (_) {
    // 剪贴板 API 需要安全上下文；file:// 或旧浏览器会失败。
    // 用老办法兜底，别让"复制"什么都不做。
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); toast("已复制"); }
    catch (_e) { toast("复制失败"); }
    ta.remove();
  }
}

/** 全屏看大图，点任意处或按 Esc 关闭。 */
export function openLightbox(src) {
  const box = document.createElement("div");
  box.className = "lightbox";
  const img = document.createElement("img");
  img.src = src;
  box.appendChild(img);
  box.onclick = () => box.remove();
  document.addEventListener("keydown", function esc(e) {
    if (e.key === "Escape") {
      box.remove();
      document.removeEventListener("keydown", esc);
    }
  });
  document.body.appendChild(box);
}

/** 装配全局监听。由 app.js 在启动时调用一次。 */
export function install() {
  document.addEventListener("contextmenu", (e) => {
    // 输入框保留系统的复制/粘贴菜单。
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea") return;
    e.preventDefault();

    const msgEl = e.target.closest(".msg");
    if (msgEl && msgEl._entry) {
      showCtx(e.clientX, e.clientY, entryMenu(msgEl._entry));
      return;
    }
    // 其他位置：有选中就给"复制选中内容"，否则什么也不给。
    const sel = selectionText();
    if (sel) {
      showCtx(e.clientX, e.clientY,
              [{ label: "复制选中内容", run: () => copyText(sel) }]);
    }
  });

  document.addEventListener("click", closeCtx);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeCtx();
  });
  window.addEventListener("blur", closeCtx);
  window.addEventListener("resize", closeCtx);

  // 事件委托：气泡里的图片点一下看大图。
  document.addEventListener("click", (e) => {
    const img = e.target.closest(".bubble img");
    if (img && img.src) openLightbox(img.src);
  });

  // 应用窗口里 F5 / Ctrl+R 会重载界面，丢掉当前会话和滚动位置，
  // 而用户没有任何理由需要它。挡掉。
  document.addEventListener("keydown", (e) => {
    const k = e.key.toLowerCase();
    if (k === "f5" || ((e.ctrlKey || e.metaKey) && k === "r")) {
      e.preventDefault();
    }
  });
}
