// 密文区：遮挡层状态机 + 信道切换 + 握手。
//
// 状态只有四种，遮挡层的内容完全由状态决定：
//   locked   未握手：只有一个「发起握手」大按钮
//   waiting  已发起：显示"已发起第 n 次握手"，仍可再点
//   ready    已握手：遮挡消失，露出输入框
//   noimage  已握手但图片装不下：只盖住图片相关部分，仍可改用文本信道
//
// COVERT 状态在 core/store.js —— chat 视图也要读它（判断能不能发）。

import { $, toast } from "../core/dom.js";
import { api } from "../core/api.js";
import { S, COVERT } from "../core/store.js";

// 拉全局状态的入口由 app.js 注入。covert 不能直接 import app.js：
// app.js 已经 import 了 covert，那会形成循环引用。
let hooks = {
  refreshState: async () => {},
  // 信道切换后要重算容量：图片信道看"占几张"，文本信道看"占多少字符"，
  // 两个数字完全不同，切过去还显示旧数字就是骗人。
  onChannelChanged: () => {},
};

export function setCovertHooks(next) {
  hooks = Object.assign(hooks, next);
}

// 密文区只有三种状态，遮挡层的内容由状态决定：
//   locked   —— 未握手：只有一个「发起握手」大按钮
//   waiting  —— 已发起：显示"已发起第 n 次握手，等待回应"，仍可再点
//   ready    —— 已握手：遮挡消失，露出输入框
//   noimage  —— 已握手但图片装不下：只盖住图片相关部分，并提示

export function setCovertState(state, extra) {
  COVERT.state = state;
  const veil = $("covertVeil");
  const icon = $("veilIcon");
  const title = $("veilTitle");
  const text = $("veilText");
  const action = $("veilAction");
  const composer = $("covertComposer");

  const ignore = $("veilIgnore");
  const fp = $("veilFp");

  veil.classList.remove("warn");
  action.hidden = false;
  if (ignore) ignore.hidden = true;
  if (fp) { fp.hidden = true; fp.className = "veilfp"; fp.textContent = ""; }

  if (state === "pending") {
    // 对方发来握手请求，等用户决定。
    //
    // **不自动回复**：自动回复在 QQ 侧看就是机器行为，用户也失去核对指纹的
    // 机会，而且客户端挂着时任何人都能自动把连接建立起来。
    veil.hidden = false;
    icon.textContent = "🤝";
    // 说清楚是"几秒前"的请求：对方反复发时，用户得知道自己点的是
    // 最新那个还是半小时前的。只回最新一个 —— 一次建立一堆连接没有意义。
    const age = (extra && typeof extra.pending_age === "number")
      ? extra.pending_age : 0;
    const when = age < 5 ? "刚刚" : `${Math.round(age)} 秒前`;
    title.textContent = extra && extra.rekey
      ? `对方请求更换密钥（${when}）`
      : `对方请求建立加密连接（${when}）`;
    const status = (extra && extra.fingerprint_status) || "first";
    if (status === "changed") {
      veil.classList.add("warn");
      text.textContent = "⚠ 对方的密钥和上次不同。可能是重装了客户端，"
        + "也可能是有人在中间。请在别的渠道核对指纹后再接受。";
      if (fp) {
        fp.hidden = false;
        fp.className = "veilfp changed";
        fp.textContent = `上次 ${extra.previous_fingerprint}
现在 ${extra.fingerprint}`;
      }
    } else {
      text.textContent = status === "first"
        ? "首次与该联系人建立连接。确认对方身份后点接受。"
        : "密钥与上次一致。";
      if (fp && extra && extra.fingerprint) {
        fp.hidden = false;
        fp.textContent = `指纹 ${extra.fingerprint}`;
      }
    }
    action.textContent = "接受并回复";
    action.disabled = false;
    if (ignore) ignore.hidden = false;
    composer.classList.add("muted");
  } else if (state === "locked") {
    veil.hidden = false;
    icon.textContent = "🔒";
    title.textContent = "尚未握手";
    text.textContent = "先与对方建立密钥，才能收发隐藏内容。";
    action.textContent = "发起握手";
    action.disabled = false;
    composer.classList.add("muted");
  } else if (state === "waiting") {
    veil.hidden = false;
    icon.textContent = "⏳";
    title.textContent = `已发起第 ${COVERT.attempts} 次握手`;
    text.textContent = "等待对方回应。对方没收到或没开终端时可以再点一次。";
    action.textContent = "再发起一次握手";
    action.disabled = false;
    composer.classList.add("muted");
  } else if (state === "noimage") {
    // 已握手但图片不足：只提示，不挡输入（文本信道仍可能可用）。
    veil.hidden = false;
    veil.classList.add("warn");
    icon.textContent = "!";
    title.textContent = "图片数量不足";
    text.textContent = (extra && extra.message)
      || "待发图片装不下要隐藏的内容。往图片文件夹里加几张图，或改用文本信道。";
    action.textContent = "补图";
    action.disabled = false;
    composer.classList.remove("muted");
  } else {
    veil.hidden = true;
    composer.classList.remove("muted");
  }
  updateCovertControls();
}

// 遮挡显示时，下面的输入框不能操作 —— 但**待发图片列表要能看见并操作**，
// 用户需要在握手前管理它们。所以只禁用输入区，不禁用队列。
export function updateCovertControls() {
  const locked = COVERT.state === "locked" || COVERT.state === "waiting"
    || COVERT.state === "pending";
  const input = $("covertInput");
  const btn = $("btnSendCovert");
  const sw = $("channelSwitch");
  if (input) input.disabled = locked;
  if (btn) btn.disabled = locked;
  if (sw) sw.classList.toggle("disabled", locked);
}


export function setChannel(channel, opts) {
  const usableText = !!(opts && opts.textUsable);
  // 没有模型就不能切到文本 —— 切过去也发不出去，那是骗用户。
  if (channel === "text" && !usableText) {
    channel = "image";
    if (!(opts && opts.quiet)) {
      toast("未配置文本模型，只能使用图片信道");
    }
  }
  COVERT.channel = channel;
  const sw = $("channelSwitch");
  sw.setAttribute("aria-checked", channel === "text" ? "true" : "false");
  sw.classList.toggle("disabled", !usableText && channel === "image"
                       && COVERT.state !== "locked"
                       && COVERT.state !== "waiting");
  renderChannelExtras(opts || {});
  hooks.onChannelChanged(channel);
}

export function renderChannelExtras(opts) {
  const textUsable = !!opts.textUsable;
  const names = opts.modelNames || [];
  const pick = $("modelPick");
  const hint = $("modelHint");

  if (COVERT.channel === "text") {
    pick.hidden = false;
    pick.disabled = !textUsable;
    if (names.length) {
      const wanted = names.join("\u0000");
      if (pick.dataset.names !== wanted) {
        pick.dataset.names = wanted;
        const previous = pick.value;
        pick.innerHTML = "";
        for (const name of names) {
          const opt = document.createElement("option");
          opt.value = name;
          opt.textContent = name;
          pick.appendChild(opt);
        }
        if (previous) pick.value = previous;
      }
      hint.className = "sub";
      hint.textContent = "";
    } else {
      pick.hidden = true;
      hint.className = "chanwarn";
      hint.textContent = "⚠ 未配置文本模型";
    }
  } else {
    // 图片信道时不需要选模型，但要让用户知道文本信道为什么不能用。
    pick.hidden = true;
    if (textUsable) {
      hint.className = "sub";
      hint.textContent = "";
    } else {
      hint.className = "chanwarn";
      hint.textContent = "⚠ 未配置文本模型";
    }
  }
}

export function initChannelSwitch() {
  const sw = $("channelSwitch");
  const toggle = () => {
    if (COVERT.state === "locked" || COVERT.state === "waiting") return;
    const textUsable = S.textUsable;
    if (COVERT.channel === "image" && !textUsable) {
      toast("未配置文本模型，只能使用图片信道");
      return;
    }
    setChannel(COVERT.channel === "image" ? "text" : "image",
               { textUsable, modelNames: S.modelNames });
  };
  sw.addEventListener("click", toggle);
  sw.addEventListener("keydown", (e) => {
    if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggle(); }
  });
}


export async function doHandshake() {
  if (!S.current) return;
  try {
    const d = await api("/client/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ peer: S.current, mode: "handshake" }),
    });
    COVERT.attempts += 1;
    if (d && d.established) {
      setCovertState("ready");
      toast("信道已建立");
    } else {
      setCovertState("waiting");
    }
    await hooks.refreshState();
  } catch (e) {
    toast("发起握手失败：" + e.message);
  }
}

// 轮询：没有 WebSocket 推送到浏览器，用短轮询就够（单机、一个用户）。
// 首次加载要显式兜住：未处理的 rejection 在控制台之外看不见，
// 用户只会看到一个永远空着的界面。

// 刷新由 initLifecycle 里的自适应探测驱动（见 scheduleProbe），
// 不再单独 setInterval —— 两套定时器会互相打架。
