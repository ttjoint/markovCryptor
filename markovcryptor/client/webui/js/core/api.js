// 与后端的唯一通道。

import { toast } from "./dom.js";

// 默认给每个请求一个超时。
//
// 为什么必须有：后端进程被强杀后，Chrome 对已经死掉的端口**不会**立刻回
// ECONNREFUSED —— 它会重试连接，实测单次耗时约 2 秒。没有超时的话，
// "探测后端是否还在"这件事本身就比轮询周期还慢，缩短间隔完全无效。
//
// 本机回环的正常请求是毫秒级的（包括带十几张图的 /client/state，实测
// 也在 100ms 内），所以 1.2 秒足够宽松；只会在"服务真的没了"时触发。
const REQUEST_TIMEOUT = 1200;

//: 需要**用户动手**的请求（原生选文件/选文件夹对话框）用这个。
//:
//: 1.2 秒对普通请求够，但对着一个要人点选的对话框就必然超时 ——
//: 用户在对话框里挑目录要几秒到几十秒，前端 1.2 秒就 abort 了，
//: 弹出的是"请求超时（后端无响应）"，而后端其实好好的、对话框就在那儿。
//: 实测就是这样：后端心跳全程 200，界面却报超时。
export const INTERACTIVE_TIMEOUT = 10 * 60 * 1000;

export async function api(path, opts) {
  const options = Object.assign({}, opts || {});
  const timeout = Number(options.timeout) || REQUEST_TIMEOUT;
  delete options.timeout;
  let timer = null;
  if (typeof AbortController !== "undefined") {
    const ctrl = new AbortController();
    options.signal = ctrl.signal;
    timer = setTimeout(() => ctrl.abort(), timeout);
  }
  let res;
  try {
    res = await fetch(path, options);
  } catch (e) {
    // abort 出来的错误名字不统一，统一翻译成"连不上"，交给上层判断。
    if (e && (e.name === "AbortError" || /abort/i.test(String(e.message)))) {
      throw new Error("请求超时（后端无响应）");
    }
    throw e;
  } finally {
    if (timer) clearTimeout(timer);
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* 空响应 */ }
  if (!res.ok) {
    // 后端把"用户做错了"（队列空了/没握手）放在 detail 里，原样显示，
    // 不要翻译成"服务器错误"——那会让用户去查网络，方向完全错了。
    const detail = (data && data.detail) || res.statusText;
    throw new Error(detail);
  }
  return data;
}
