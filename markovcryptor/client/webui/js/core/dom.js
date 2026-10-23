// DOM 小工具。

export const $ = (id) => document.getElementById(id);

export function toast(msg) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

/**
 * 按 id 克隆一个 <template> 的第一个元素。
 *
 * 模板的内容不在文档流里，cloneNode 出来的是**未挂载**节点，
 * 调用方填完再 append。找不到模板时返回 null 并报错 —— 静默返回
 * undefined 会让调用方在后面某行莫名崩掉。
 */
export function clone(templateId) {
  const tpl = document.getElementById(templateId);
  if (!tpl || !tpl.content) {
    console.error(`<template id="${templateId}"> 不存在`);
    return null;
  }
  return tpl.content.firstElementChild.cloneNode(true);
}
