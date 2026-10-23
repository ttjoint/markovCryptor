// 输入区：明文附件、待发图片队列缩略图、输入框自适应高度。
//
// 三件事放在一起是因为它们共用同一块 DOM（底部的输入条）。拆开反而要
// 互相惦记对方的元素。
//
// ATTACH 是纯浏览器侧状态：用户选了哪些图、还没发出去。真实队列在服务端，
// 这里只是预览。

import { $, toast, clone } from "../core/dom.js";
import { api } from "../core/api.js";

// 刷新状态的入口由 app.js 注入 —— composer 改完队列要重新拉一次 state，
// 但"怎么拉"不该由它决定。
let refresh = async () => {};
export function setRefresh(fn) {
  refresh = fn;
}

// 队列里每张图显示成小缩略图，右上角一个叉可以撤掉。
// 只有数字（"队列 5/8"）说明不了"下一张到底发的是哪张"。
export function renderQueue(items) {
  const grid = $("queueGrid");
  if (!grid) return;
  // 内容没变就不重建，否则 2.5 秒轮询会让用户刚点到的叉消失。
  const signature = (items || []).map((i) => i.name).join("|");
  if (grid.dataset.sig === signature) return;
  grid.dataset.sig = signature;
  grid.innerHTML = "";

  (items || []).forEach((item, index) => {
    const box = clone("tpl-qthumb");       // 结构见 index.html 的 <template>
    if (!box) return;

    // 容量徽标：一张 128×128 和一张 1600×1600 能装的字数差几十倍，
    // 光看缩略图判断不出"够不够这条消息用"。
    const cap = box.querySelector(".cap");
    if (cap) {
      const bytes = item.capacity || 0;
      // 汉字按 UTF-8 3 字节估。留一点余量给握手帧。
      const chars = Math.max(0, Math.floor((bytes - 38) / 3));
      cap.textContent = chars > 0 ? `${chars}字` : "太小";
      cap.classList.toggle("small", chars <= 0);
      cap.title = `可承载约 ${chars} 个汉字（${bytes} 字节）`;
    }
    const up = box.querySelector(".up");
    if (up && item.origin === "upload") up.hidden = false;

    const img = box.querySelector("img");
    if (item.thumb) {
      img.src = item.thumb;
      img.alt = item.name || "";
      img.title = item.name || "";
    } else {
      // 缩略图生成失败也要占位，否则用户会以为队列里没东西。
      const ph = document.createElement("div");
      ph.style.cssText = "width:100%;height:100%;display:grid;"
                       + "place-items:center;font-size:10px;color:var(--dim)";
      ph.textContent = "无预览";
      img.replaceWith(ph);
    }
    box.querySelector(".x").onclick = async (ev) => {
      ev.stopPropagation();
      try {
        const d = await api("/client/queue/remove", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ index }),
        });
        renderQueue(d.items);
        await refresh();
      } catch (e) { toast(e.message); }
    };
    grid.appendChild(box);
  });
}

// 粘贴/拖入的图片先攒在这里，按 Enter 时和文本一起作为**一条**消息发出。
// QQ 允许只发图，所以文本可以为空。
export const ATTACH = { items: [], max: 9, maxBytes: 8 * 1024 * 1024 };

export function renderAttach() {
  const bar = $("attachBar");
  bar.innerHTML = "";
  bar.hidden = ATTACH.items.length === 0;
  ATTACH.items.forEach((item, index) => {
    const box = document.createElement("div");
    box.className = "attach";
    const img = document.createElement("img");
    img.src = item.thumb;
    img.alt = item.name || "";
    img.title = item.name || "";
    box.appendChild(img);
    const x = document.createElement("button");
    x.className = "x";
    x.textContent = "×";
    x.title = "移除这张图";
    x.onclick = () => { ATTACH.items.splice(index, 1); renderAttach(); };
    box.appendChild(x);
    bar.appendChild(box);
  });
  updatePlainHint();
}

export function updatePlainHint() {
  const hint = $("plainHint");
  if (!hint) return;
  if (ATTACH.items.length) {
    hint.className = "hintline";
    hint.textContent = `将随消息一起发送 ${ATTACH.items.length} 张图片`;
  } else {
    hint.className = "hintline";
    hint.textContent = "";
  }
}

// 把 File 读成 data URL，同时生成一个缩略图给界面看。
// 缩略图在浏览器里做，避免为了预览把整张原图传给服务端再传回来。
export async function readImageFile(file) {
  return new Promise((resolve, reject) => {
    if (!file.type.startsWith("image/")) {
      reject(new Error("不是图片"));
      return;
    }
    if (file.size > ATTACH.maxBytes) {
      reject(new Error("单张图片不能超过 8MB"));
      return;
    }
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("读取失败"));
    reader.onload = () => {
      const dataUrl = String(reader.result || "");
      const img = new Image();
      img.onload = () => {
        const size = 54;
        const canvas = document.createElement("canvas");
        const scale = Math.min(size / img.width, size / img.height, 1);
        canvas.width = Math.max(1, Math.round(img.width * scale));
        canvas.height = Math.max(1, Math.round(img.height * scale));
        canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
        resolve({
          name: file.name || "粘贴的图片",
          data: dataUrl,
          thumb: canvas.toDataURL("image/jpeg", 0.7),
        });
      };
      img.onerror = () => reject(new Error("图片无法解码"));
      img.src = dataUrl;
    };
    reader.readAsDataURL(file);
  });
}

export async function addFiles(files) {
  const list = Array.from(files || []);
  if (!list.length) return;
  for (const file of list) {
    if (ATTACH.items.length >= ATTACH.max) {
      toast(`最多一次发 ${ATTACH.max} 张图片`);
      break;
    }
    try {
      ATTACH.items.push(await readImageFile(file));
    } catch (e) {
      toast(e.message);
    }
  }
  renderAttach();
}

export function initAttach() {
  const input = $("input");
  const composer = $("plainComposer");

  input.addEventListener("paste", async (e) => {
    const files = [];
    for (const item of (e.clipboardData || {}).items || []) {
      if (item.kind === "file") {
        const file = item.getAsFile();
        if (file) files.push(file);
      }
    }
    if (files.length) {
      e.preventDefault();          // 别把图片当成路径文本贴进输入框
      await addFiles(files);
    }
  });

  // 拖入：dragover 必须 preventDefault，否则浏览器会直接打开图片。
  composer.addEventListener("dragover", (e) => {
    e.preventDefault();
    composer.classList.add("dragover");
  });
  composer.addEventListener("dragleave", () => composer.classList.remove("dragover"));
  composer.addEventListener("drop", async (e) => {
    e.preventDefault();
    composer.classList.remove("dragover");
    const files = Array.from((e.dataTransfer || {}).files || [])
      .filter((f) => f.type.startsWith("image/"));
    if (files.length) await addFiles(files);
  });
}

// 输入框跟着内容长高，到上限再滚。
//
// 为什么不用 CSS `field-sizing: content`：那是很新的属性，Chromium 123+
// 才有，而客户端要跑在 Win10 上（可能配的是较旧的 Chrome/Edge）。
// 用 scrollHeight 量一次是兼容性最稳的做法。
const AUTOSIZE_MIN = 46;
const AUTOSIZE_MAX = 168;

export function autosize(el) {
  if (!el) return;
  // 先归零再量，否则 scrollHeight 只增不减（元素高度会卡在历史最大值）。
  el.style.height = AUTOSIZE_MIN + "px";
  const needed = el.scrollHeight + 2;      // +2 补边框，避免刚好卡出滚动条
  el.style.height = Math.min(needed, AUTOSIZE_MAX) + "px";
  // 到上限之后才允许滚，否则内容不足时不该有滚动条
  el.style.overflowY = needed > AUTOSIZE_MAX ? "auto" : "hidden";
}

export function initAutosize() {
  for (const id of ["input", "covertInput"]) {
    const el = $(id);
    if (!el) continue;
    el.addEventListener("input", () => autosize(el));
    // 用键盘删空时 input 会触发，但程序性赋值（测试、发送后清空）不会，
    // 所以再加一个 change 兜底。真正的清空统一走 clearInput()。
    el.addEventListener("change", () => autosize(el));
    autosize(el);
  }
}

// 统一"清空并收回"的写法，避免每处都记得调 autosize。
export function clearInput(id) {
  const el = $(id);
  if (!el) return;
  el.value = "";
  autosize(el);
}
