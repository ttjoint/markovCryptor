// 展示层格式化：时间、头像。

export function fmtTime(at, showDate) {
  if (!at) return "";                      // 没有时间就不编一个
  const d = new Date(at * 1000);
  if (isNaN(d.getTime())) return "";
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  if (!showDate) return `${hh}:${mm}`;
  const mo = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${mo}-${dd} ${hh}:${mm}`;
}

export function avatarFor(uin, size = 100) {
  return uin ? `https://q1.qlogo.cn/g?b=qq&nk=${uin}&s=${size}` : "";
}

export function setAvatar(el, url, name) {
  if (!el) return;
  const initial = (name || "?").trim().slice(0, 1) || "?";
  if (!url) {
    el.textContent = initial;
    delete el.dataset.url;
    return;
  }
  if (el.dataset.url === url) return;         // 已经是这张了，别重设
  el.dataset.url = url;
  el.textContent = "";
  const img = document.createElement("img");
  img.alt = "";
  img.loading = "lazy";
  img.referrerPolicy = "no-referrer";
  img.onerror = () => {
    // CDN 拿不到（离线、被墙、号码异常）就退回首字，界面仍然可读。
    el.textContent = initial;
    delete el.dataset.url;
  };
  img.src = url;
  el.appendChild(img);
}
