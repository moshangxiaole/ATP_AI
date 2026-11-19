"""DOM 快照脚本：Web / Electron 两个驱动共用，避免口径漂移。

设计要点：
1. text 只取「本元素直属文本」，不要 innerText —— 否则父容器会把整页文本都算进来，
   长文本既会被定位器稳定性规则过滤，又会淹没真正的叶子节点。
2. clickable 除了 onclick/button/a/role，再加上 cursor:pointer ——
   现代框架（React/Vue）的点击行通常没有 onclick 属性，只能靠光标样式判断。
3. SVG 内部元素（g/path/rect…）会继承 cursor:pointer 且没有文案，属于纯噪音，
   直接丢弃，避免把 90 个节点预算耗光。
"""

SVG_NOISE = {"svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline",
             "polygon", "defs", "use", "tspan", "clippath", "mask", "pattern",
             "lineargradient", "radialgradient", "stop", "symbol"}

JS_DUMP = """
() => {
  const SVG = new Set(['svg','g','path','rect','circle','ellipse','line','polyline',
                       'polygon','defs','use','tspan','clippath','mask','pattern',
                       'lineargradient','radialgradient','stop','symbol']);
  const out = [];
  const ownText = (el) => {
    let s = '';
    for (const n of el.childNodes) {
      if (n.nodeType === 3) s += n.nodeValue;
    }
    return s.replace(/\\s+/g, ' ').trim();
  };
  const walk = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width > 1 && r.height > 1 && r.top < window.innerHeight && r.bottom > 0) {
      const tag = el.tagName.toLowerCase();
      const value = (tag === 'input' || tag === 'textarea') ? (el.value || '') : '';
      const text = (ownText(el) || value).slice(0, 40);
      const editable = el.isContentEditable || tag === 'input' || tag === 'textarea';
      let desc = el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
                 el.getAttribute('title') || el.getAttribute('data-placeholder') || '';
      // 可编辑区域经常既无文案也无 id（contenteditable 的富文本输入框就是典型），
      // 如果因此被过滤掉，规划器就"看不见"输入框，只能用搜索框乱试。给它一个语义标签。
      if (!desc && editable && el.getAttribute('type') !== 'hidden') {
        desc = '可编辑输入区';
      }
      const id = el.id || '';
      const isSvg = SVG.has(tag);
      if (!isSvg || text || desc || id) {
        let cursor = '';
        try { cursor = getComputedStyle(el).cursor || ''; } catch (e) {}
        out.push({
          tag,
          id,
          text,
          value,
          desc,
          clickable: !!(el.onclick || tag === 'button' || tag === 'a' ||
                        el.getAttribute('role') === 'button' ||
                        el.getAttribute('onclick') || editable ||
                        (!isSvg && cursor === 'pointer')),
          bounds: [Math.round(r.left), Math.round(r.top),
                   Math.round(r.right), Math.round(r.bottom)]
        });
      }
    }
    for (const c of el.children) walk(c);
  };
  if (document.body) walk(document.body);
  return out;
}
"""
