// 把图片拖进来 / 点选 / 粘贴进待发队列。
//
// 三条入口最终都走同一个后端端点 `/client/queue/import`：它把图片**插到
// 队首**（用户刚选的就是这次想用的），只留在内存（项目坚持零持久化）。
//
// 为什么不复用"明文附件"那套 ATTACH：那是**明文**消息的图片，不经过隐写。
// 隐写用的图必须进服务端队列，因为容量要按 8×8 块真实计算，前端算不出来。

import { $, toast } from "../core/dom.js";
import { api } from "../core/api.js";

const REFRESH_EVENTS = ["dragenter", "dragover", "dragleave", "drop"];

let onImported = null;      // 导入成功后刷新界面（由 app.js 注入）
let depth = 0;              // dragenter/leave 计数，避免子元素触发闪烁

export function setImportedCallback(fn) {
  onImported = fn;
}

/** 装好拖放、点选、粘贴三个入口。启动时调一次。 */
export function installDropper(panel) {
  const host = panel || document.body;
  const veil = $("dropVeil");

  // ---- 拖放 ----
  // dragover 必须 preventDefault，否则浏览器会用默认行为（打开文件）并且
  // drop 事件根本不会来。
  host.addEventListener("dragenter", (e) => {
    if (!_hasFiles(e)) return;
    e.preventDefault();
    depth += 1;
    if (veil) veil.classList.add("on");
  });

  host.addEventListener("dragover", (e) => {
    if (!_hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  });

  host.addEventListener("dragleave", (e) => {
    if (!_hasFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (depth === 0 && veil) veil.classList.remove("on");
  });

  host.addEventListener("drop", async (e) => {
    if (!_hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    if (veil) veil.classList.remove("on");
    const files = [...(e.dataTransfer?.files || [])];
    await _importFiles(files);
  });

  // ---- 粘贴 ----
  // 绑在 document 上：用户可能焦点在输入框里直接 Ctrl+V 一张截图。
  document.addEventListener("paste", async (e) => {
    const files = [...(e.clipboardData?.files || [])];
    if (!files.length) return;
    // 只在图片区可见时才接管，免得在设置面板里粘贴路径也被拦。
    e.preventDefault();
    await _importFiles(files);
  });

  // ---- 点选 ----
  const pick = $("btnQueuePick");
  if (pick) {
    const input = $("queueFilePick");
    pick.onclick = () => input && input.click();
    if (input) {
      input.onchange = async () => {
        await _importFiles([...input.files]);
        input.value = "";                  // 允许连续选同一个文件
      };
    }
  }

  // 拖放时别让浏览器接管整页（比如把图片当导航打开）
  for (const name of REFRESH_EVENTS) {
    window.addEventListener(name, (e) => {
      if (_hasFiles(e)) e.preventDefault();
    });
  }
}

function _hasFiles(e) {
  const types = e.dataTransfer?.types;
  return !!types && Array.from(types).includes("Files");
}

async function _importFiles(files) {
  const images = files.filter((f) => f.type.startsWith("image/"));
  if (!images.length) {
    toast("只能拖入图片文件");
    return;
  }

  // 转成 data URL 发给后端。以本机回环传输几百 KB 是无感的，
  // 而且省掉了 multipart 解析。
  const urls = [];
  for (const file of images) {
    try {
      urls.push(await _readAsDataUrl(file));
    } catch (e) {
      toast(`读取 ${file.name} 失败`);
    }
  }
  if (!urls.length) return;

  try {
    const result = await api("/client/queue/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ images: urls, name: images[0].name || "" }),
    });
    const added = result.added || 0;
    if (result.rejected && result.rejected.length) {
      toast(`已加入 ${added} 张；${result.rejected.length} 张被拒：`
            + result.rejected[0]);
    } else {
      toast(`已加入 ${added} 张待发图片`);
    }
    if (onImported) await onImported();
  } catch (e) {
    toast(e.message);
  }
}

function _readAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error || new Error("读取失败"));
    reader.readAsDataURL(file);
  });
}
