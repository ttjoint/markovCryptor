// 设置面板：常规设置、图片文件夹、文本模型管理。
//
// 面板是 modal（fixed 定位），和主布局无关 —— 它挂在 body 上，
// 不受 .workspace 的 grid 约束。
//
// 模型文件**先校验再落盘**：MarkovModel.load() 失败就直接 422，
// 不会在磁盘上留下半张没用的模型。

import { $, toast } from "../core/dom.js";
import { api } from "../core/api.js";
import { refreshState } from "../app.js";

export const SET = { data: null, models: [] };

export function openSheet(tab) {
  $("modal").hidden = false;
  switchSheetTab(tab || "general");
  loadSettings();
  loadModelList();
}

export function closeSheet() {
  $("modal").hidden = true;
}

export function switchSheetTab(tab) {
  const general = tab === "general";
  $("tabGeneral").setAttribute("aria-selected", general ? "true" : "false");
  $("tabModels").setAttribute("aria-selected", general ? "false" : "true");
  $("paneGeneral").hidden = !general;
  $("paneModels").hidden = general;
  // 「保存设置」只对常规页有意义；模型页的按钮各管各的。
  $("btnSaveSettings").hidden = !general;
}

export async function loadSettings() {
  try {
    const d = await api("/client/settings");
    SET.data = d;
    renderFolders(d);
    $("maxQueue").value = d.max_queue;
    $("pickMode").value = d.pick;
    $("modelFolder").value = d.model_folder || "";
    $("configPath").textContent = "配置文件：" + d.path;
  } catch (e) {
    toast("读取设置失败：" + e.message);
  }
}

export function renderFolders(d) {
  const list = $("folderList");
  list.innerHTML = "";
  const usable = new Set(d.usable_folders || []);
  if (!(d.cover_folders || []).length) {
    const li = document.createElement("li");
    li.innerHTML = '<span class="p" style="color:var(--dim)">还没有配置</span>';
    list.appendChild(li);
    return;
  }
  for (const folder of d.cover_folders) {
    const li = document.createElement("li");
    const span = document.createElement("span");
    span.className = "p";
    span.textContent = folder;
    span.title = folder;
    li.appendChild(span);
    if (!usable.has(folder)) {
      // 区分"没配"和"配了但没图/不存在"—— 两种情况下用户该做的事不一样。
      const warn = document.createElement("span");
      warn.className = "warn";
      warn.textContent = "无可用图片";
      li.appendChild(warn);
    }
    const del = document.createElement("button");
    del.className = "btn danger";
    del.textContent = "移除";
    del.onclick = async () => {
      const rest = (SET.data.cover_folders || []).filter((f) => f !== folder);
      await saveSettings({ cover_folders: rest });
      await loadSettings();
    };
    li.appendChild(del);
    list.appendChild(li);
  }
}

export async function saveSettings(patch) {
  try {
    await api("/client/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    await refreshState();
    return true;
  } catch (e) {
    toast("保存失败：" + e.message);
    return false;
  }
}

export async function pickFolder(title, targetInput) {
  try {
    const d = await api("/client/settings/pick-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    if (d.ok && d.path) {
      if (targetInput) targetInput.value = d.path;
      return d.path;
    }
    if (d.cancelled) return null;
    toast("系统没有提供选择对话框，请手动粘贴路径");
    return null;
  } catch (e) {
    toast("打开选择框失败：" + e.message);
    return null;
  }
}

export async function loadModelList() {
  try {
    const d = await api("/client/models");
    SET.models = d.names || [];
    const list = $("modelList");
    list.innerHTML = "";
    const all = (d.names || []).slice();
    // 已加载的和失败的都要显示：失败的要能看见、能删掉，否则用户
    // 永远不知道为什么少了一个模型。
    const failed = (d.failed_names || []);
    $("modelEmpty").hidden = all.length > 0 || failed.length > 0;
    for (const name of all) {
      list.appendChild(modelRow(name, true));
    }
    for (const name of failed) {
      list.appendChild(modelRow(name, false));
    }
  } catch (e) {
    toast("读取模型列表失败：" + e.message);
  }
}

export function modelRow(name, loaded) {
  const li = document.createElement("li");
  const nm = document.createElement("span");
  nm.className = "nm";
  nm.textContent = name;
  li.appendChild(nm);
  const tag = document.createElement("span");
  tag.className = "tag " + (loaded ? "ok" : "bad");
  tag.textContent = loaded ? "已加载" : "加载失败";
  li.appendChild(tag);

  const exp = document.createElement("button");
  exp.className = "btn";
  exp.textContent = "导出";
  exp.onclick = async () => {
    const dir = await pickFolder("选择导出到的文件夹", null);
    if (!dir) return;
    try {
      const d = await api("/client/models/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, path: dir }),
      });
      toast("已导出到 " + d.path);
    } catch (e) { toast(e.message); }
  };
  li.appendChild(exp);

  const del = document.createElement("button");
  del.className = "btn danger";
  del.textContent = "删除";
  del.onclick = async () => {
    if (!confirm(`删除模型「${name}」？这会删掉磁盘上的 .json 文件。`)) return;
    try {
      await api("/client/models/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      toast("已删除 " + name);
      await loadModelList();
      await refreshState();
    } catch (e) { toast(e.message); }
  };
  li.appendChild(del);
  return li;
}

// 把 .json 拖到窗口任意位置 = 安装文本模型。
// 原来只能"设置 -> 选目录 -> 把文件拷进去 -> 重新扫描"，四步而且要在两个
// 窗口之间来回切；拖进来一步到位，还会立刻加载。
//
// 注意：图片拖到**明文输入区**是发图（见 initAttach），这里只管 .json，
// 两者用文件类型区分，不会打架。
export function initModelDrop() {
  let overlay = null;

  const show = (text) => {
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.className = "dropveil";
      const box = document.createElement("div");
      box.className = "dropbox";
      box.id = "dropBoxText";
      overlay.appendChild(box);
      document.body.appendChild(overlay);
    }
    const box = overlay.querySelector("#dropBoxText");
    box.textContent = text;
    overlay.hidden = false;
  };
  const hide = () => { if (overlay) overlay.hidden = true; };

  const isModelFile = (file) =>
    /\.json$/i.test(file.name || "") || file.type === "application/json";

  document.addEventListener("dragenter", (e) => {
    if (!e.dataTransfer) return;
    const hasModel = Array.from(e.dataTransfer.items || [])
      .some((it) => it.kind === "file" && isModelFile(it));
    if (hasModel) {
      e.preventDefault();
      show("松开以安装文本模型");
    }
  });

  document.addEventListener("dragover", (e) => {
    if (!e.dataTransfer) return;
    const hasModel = Array.from(e.dataTransfer.items || [])
      .some((it) => it.kind === "file" && isModelFile(it));
    if (hasModel) e.preventDefault();
  });

  document.addEventListener("dragleave", (e) => {
    // 只有真正离开窗口才收起，划过子元素不算。
    if (e.relatedTarget === null) hide();
  });

  document.addEventListener("drop", async (e) => {
    const files = Array.from((e.dataTransfer || {}).files || []);
    const models = files.filter(isModelFile);
    if (!models.length) return;             // 图片交给明文输入区处理
    e.preventDefault();
    hide();

    for (const file of models) {
      if (file.size > 64 * 1024 * 1024) {
        toast(`${file.name} 太大（超过 64MB）`);
        continue;
      }
      try {
        const data = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onerror = () => reject(new Error("读取失败"));
          reader.onload = () => resolve(String(reader.result || ""));
          reader.readAsDataURL(file);
        });
        const d = await api("/client/models/upload", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: file.name, data }),
        });
        toast(`已安装模型：${d.name}`);
      } catch (err) {
        toast(`${file.name}：${err.message}`);
      }
    }
    await refreshState();
    if (!$("modal").hidden) await loadModelList();
  });
}

/** 绑定设置面板的全部交互。由 app.js 在启动时调用一次。 */
export function installSettings() {
  $("btnSettings").onclick = () => openSheet("general");
  $("btnCloseSheet").onclick = closeSheet;
  $("btnCancelSheet").onclick = closeSheet;
  $("tabGeneral").onclick = () => switchSheetTab("general");
  $("tabModels").onclick = () => switchSheetTab("models");
  $("modal").addEventListener("click", (e) => {
    if (e.target === $("modal")) closeSheet();     // 点遮罩关闭
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("modal").hidden) closeSheet();
  });

  $("btnSaveSettings").onclick = async () => {
    const ok = await saveSettings({
      max_queue: Number($("maxQueue").value) || 8,
      pick: $("pickMode").value,
      model_folder: $("modelFolder").value.trim(),
    });
    if (ok) {
      toast("设置已保存");
      await loadSettings();
      await loadModelList();
    }
  };

  $("btnAddFolder").onclick = async () => {
    const chosen = await pickFolder("选择图片文件夹", null);
    if (!chosen) return;
    const rest = [...(SET.data.cover_folders || []), chosen];
    if (await saveSettings({ cover_folders: rest })) {
      await loadSettings();
      toast("已添加：" + chosen);
    }
  };

  $("btnAddManual").onclick = async () => {
    const value = $("manualFolder").value.trim();
    if (!value) return;
    const rest = [...(SET.data.cover_folders || []), value];
    if (await saveSettings({ cover_folders: rest })) {
      $("manualFolder").value = "";
      await loadSettings();
    }
  };

  $("btnPickModelFolder").onclick = async () => {
    const chosen = await pickFolder("选择模型目录", $("modelFolder"));
    if (!chosen) return;
    if (await saveSettings({ model_folder: chosen })) {
      toast("模型目录已更新");
      await loadModelList();
    }
  };

  $("btnPickCorpus").onclick = async () => {
    const chosen = await pickFolder("选择语料所在文件夹", null);
    if (!chosen) return;
    // 目录选择器拿不到文件名，提示用户补全 —— 比假装选好了更诚实。
    $("corpusPath").value = chosen + String.fromCharCode(92);
    $("corpusPath").focus();
    toast("请补全语料文件名");
  };

  $("btnTrain").onclick = async () => {
    const path = $("corpusPath").value.trim();
    if (!path) { toast("请填写语料文件路径"); return; }
    const btn = $("btnTrain");
    btn.disabled = true;
    btn.textContent = "训练中…";
    try {
      const d = await api("/client/models/train", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, name: $("modelName").value.trim() }),
      });
      toast("训练完成：" + d.path);
      $("corpusPath").value = "";
      $("modelName").value = "";
      await loadModelList();
      await refreshState();
    } catch (e) {
      toast("训练失败：" + e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "开始训练";
    }
  };

  $("btnImport").onclick = async () => {
    const path = $("importPath").value.trim();
    if (!path) { toast("请填写模型文件路径"); return; }
    try {
      const d = await api("/client/models/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      toast("已导入：" + d.path);
      $("importPath").value = "";
      await loadModelList();
      await refreshState();
    } catch (e) { toast(e.message); }
  };

  $("btnOnboardSettings").onclick = () => openSheet("general");
}
