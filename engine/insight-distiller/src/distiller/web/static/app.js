/* 共享工具。原生 JS，零依赖、零构建。 */

const api = {
  async get(path) {
    const r = await fetch('/api' + path);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  },
  async post(path, body) {
    const r = await fetch('/api' + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  },
  async del(path) {
    const r = await fetch('/api' + path, { method: 'DELETE' });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  },
};

/* 一切插入 DOM 的库内文本都必须过这里。
   标题/批注可能含 < > &，直接拼进 innerHTML 会破坏页面结构。 */
function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function el(html) {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

function densityHtml(d) {
  if (!d || d.score === null) return '<span class="density none">未评分</span>';
  return `<span class="density ${d.level}" title="${esc(d.verdict)}">
    <span class="bar"><i style="width:${d.pct}%"></i></span>${d.score}</span>`;
}

function fmtCost(v) {
  return '$' + (v || 0).toFixed(4);
}

/* 顶栏的待重温数与花费。所有页面共用，失败静默——
   它只是辅助信息，不该因为它报错干扰主功能。 */
async function loadTopbar() {
  try {
    const s = await api.get('/stats');
    const due = s.review?.due || 0;
    const badge = document.getElementById('nav-due');
    if (badge && due > 0) {
      badge.textContent = due;
      badge.classList.remove('hidden');
    }
    const cost = document.getElementById('cost');
    if (cost) {
      cost.textContent = fmtCost(s.month_cost) + ' / $' + s.budget;
      if (s.budget && s.month_cost > s.budget * 0.8) cost.style.color = 'var(--low)';
    }
  } catch (_) { /* 静默 */ }
}

document.addEventListener('DOMContentLoaded', loadTopbar);
