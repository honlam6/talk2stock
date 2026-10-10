/* Talk2Stock 对话式进销存智能体 · 公共 JS（无依赖，离线可用） */

const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

const BIZ = {
  purchase_in:     { label: '采购入库', party: '供应商', dir: 'in'  },
  sale_out:        { label: '销售出库', party: '客户',   dir: 'out' },
  purchase_return: { label: '采购退货', party: '供应商', dir: 'out' },
  sale_return:     { label: '销售退货', party: '客户',   dir: 'in'  },
  other_in:        { label: '其他入库', party: '往来单位', dir: 'in'  },
  other_out:       { label: '其他出库', party: '往来单位', dir: 'out' },
};

// ---------- 网络 ----------
async function api(path, opts) {
  const o = Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts || {});
  if (!o.headers) o.headers = {};
  const pass = localStorage.getItem('t2s_pass');
  if (pass) o.headers['X-T2S-Pass'] = pass;
  if (o.body && typeof o.body !== 'string') o.body = JSON.stringify(o.body);
  let r;
  try {
    r = await fetch(path, o);
  } catch (e) {
    throw new Error('连接不上本地服务，请确认 server.py 还在运行');
  }
  if (r.status === 401) {
    const p = prompt('该服务已开启访问口令（T2S_DEMO_PASS），请输入：', '');
    if (p) { localStorage.setItem('t2s_pass', p); return api(path, opts); }
    throw new Error('需要访问口令');
  }
  const txt = await r.text();
  let j;
  try { j = JSON.parse(txt); }
  catch (e) { throw new Error('服务返回异常：' + txt.slice(0, 120)); }
  return j;
}

// ---------- 工具 ----------
const money = n => (Number(n) || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const num = n => { const v = parseFloat(n); return isNaN(v) ? 0 : v; };
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function today() {
  const d = new Date(), p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
function nowStamp() {
  const d = new Date(), p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// ---------- 提示条 ----------
function toast(msg, kind) {
  let t = $('#__toast');
  if (!t) {
    t = document.createElement('div');
    t.id = '__toast';
    t.className = 'toast';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.style.cssText = 'position:fixed;left:50%;top:24px;transform:translateX(-50%);z-index:9999;' +
    'padding:12px 24px;border-radius:8px;font-size:16px;box-shadow:0 4px 16px rgba(0,0,0,.2);' +
    'color:#fff;max-width:88vw;text-align:center;' +
    'background:' + (kind === 'bad' ? '#ff4d4f' : kind === 'warn' ? '#fa8c16' : '#52c41a');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.remove(), kind === 'bad' ? 5000 : 2600);
}

// ---------- 主数据 ----------
const MD = { suppliers: [], customers: [], materials: [], depots: [], accounts: [], units: [], loaded: false };

async function loadMaster(force) {
  if (MD.loaded && !force) return MD;
  const d = await api('/api/master');
  if (!d.ok) throw new Error(d.message || '主数据加载失败');
  ['suppliers', 'customers', 'materials', 'depots', 'accounts', 'units'].forEach(k => {
    MD[k] = (d[k] || []).map(x => (typeof x === 'string' ? { name: x } : x));
  });
  MD.counts = d.counts || {};
  MD.loaded = true;
  return MD;
}

const partyPool = biz => (BIZ[biz] && BIZ[biz].party === '客户') ? MD.customers : MD.suppliers;

function findExact(pool, name) {
  const n = String(name || '').trim().toLowerCase().replace(/\s/g, '');
  if (!n) return null;
  return pool.find(x => String(x.name || '').trim().toLowerCase().replace(/\s/g, '') === n) || null;
}

// ---------- 自动补全 ----------
/** 绑定自动补全。getPool 返回 [{name,...}]，onPick(item) 选中回调，onType(value,exists) 输入回调 */
function autocomplete(input, getPool, onPick, onType) {
  const wrap = input.closest('.ac-wrap') || (() => {
    const w = document.createElement('div');
    w.className = 'ac-wrap';
    input.parentNode.insertBefore(w, input);
    w.appendChild(input);
    return w;
  })();
  let list = $('.ac-list', wrap);
  if (!list) {
    list = document.createElement('div');
    list.className = 'ac-list';
    wrap.appendChild(list);
  }
  let idx = -1, items = [];

  const close = () => { list.classList.remove('show'); idx = -1; };

  const render = () => {
    const v = input.value.trim().toLowerCase();
    const pool = getPool() || [];
    items = (v
      ? pool.filter(x => String(x.name || '').toLowerCase().includes(v))
      : pool).slice(0, 40);
    const exists = !!findExact(pool, input.value);
    if (!items.length && !v) { close(); return; }
    let html = items.map((x, i) =>
      `<div class="ac-item${i === idx ? ' sel' : ''}" data-i="${i}">${esc(x.name)}` +
      (x.unit ? `<span class="sub">${esc(x.unit)}</span>` : '') + `</div>`).join('');
    if (v && !exists) {
      html += `<div class="ac-item mk-new" data-new="1">＋ 使用「${esc(input.value.trim())}」（系统中没有，提交时自动新建）</div>`;
    }
    list.innerHTML = html;
    list.classList.toggle('show', !!html);
  };

  input.addEventListener('input', () => {
    idx = -1;
    render();
    if (onType) onType(input.value, !!findExact(getPool() || [], input.value));
  });
  input.addEventListener('focus', render);
  input.addEventListener('blur', () => setTimeout(close, 180));

  list.addEventListener('mousedown', e => {
    const el = e.target.closest('.ac-item');
    if (!el) return;
    e.preventDefault();
    if (!el.dataset.new) {
      const it = items[+el.dataset.i];
      input.value = it.name;
      if (onPick) onPick(it);
    }
    close();
    if (onType) onType(input.value, !!findExact(getPool() || [], input.value));
  });

  input.addEventListener('keydown', e => {
    if (!list.classList.contains('show')) return;
    const n = list.children.length;
    if (e.key === 'ArrowDown') { e.preventDefault(); idx = (idx + 1) % n; render(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); idx = (idx - 1 + n) % n; render(); }
    else if (e.key === 'Enter' && idx >= 0) {
      e.preventDefault();
      list.children[idx].dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    } else if (e.key === 'Escape') close();
  });
}

// ---------- 附件上传管理器（对应原版 jshERP 单据附件） ----------
function AttachmentManager(zoneEl, opts) {
  opts = opts || {};
  const biz = opts.biz || 'bill';
  const max = opts.max || 4;            // 原版限制单单据最多 4 份
  const paths = [];                     // 已上传到 ERP 的相对路径串
  const names = [];                     // 对应文件名（用于展示）

  function render() {
    zoneEl.innerHTML = `
      <div class="attach-drop" id="__attDrop">
        <button type="button" class="btn btn-sm btn-ghost" id="__attBtn">＋ 上传附件</button>
        <span>原发票 / 单据扫描件 / 合同（最多 ${max} 份，图片或 PDF）</span>
        <input type="file" id="__attFile" multiple accept="image/*,application/pdf"
               style="display:none">
      </div>
      <div class="attach-list" id="__attList"></div>
      <div class="attach-limit">已上传 ${paths.length}/${max} 份</div>`;
    const fileInput = $('#__attFile', zoneEl);
    $('#__attBtn', zoneEl).onclick = () => fileInput.click();
    $('#__attDrop', zoneEl).onclick = e => { if (e.target.id === '__attDrop') fileInput.click(); };
    fileInput.onchange = () => { if (fileInput.files.length) upload(fileInput.files); };
    // 拖拽
    zoneEl.addEventListener('dragover', e => { e.preventDefault(); zoneEl.classList.add('drag'); });
    zoneEl.addEventListener('dragleave', () => zoneEl.classList.remove('drag'));
    zoneEl.addEventListener('drop', e => {
      e.preventDefault(); zoneEl.classList.remove('drag');
      if (e.dataTransfer.files.length) upload(e.dataTransfer.files);
    });
    renderList();
  }

  function renderList() {
    const list = $('#__attList', zoneEl);
    if (!list) return;
    list.innerHTML = paths.map((p, i) => `
      <div class="attach-item">
        <span class="ok">✓</span>
        <span class="nm" title="${esc(names[i])}">${esc(names[i])}</span>
        <span class="x" data-i="${i}">×</span>
      </div>`).join('');
    list.querySelectorAll('.x').forEach(x => x.onclick = () => {
      paths.splice(+x.dataset.i, 1); names.splice(+x.dataset.i, 1); renderList();
      const lim = $('.attach-limit', zoneEl);
      if (lim) lim.textContent = `已上传 ${paths.length}/${max} 份`;
    });
  }

  async function upload(fileList) {
    if (paths.length + fileList.length > max) {
      toast(`最多 ${max} 份附件`, 'warn'); return;
    }
    for (const f of Array.from(fileList)) {
      if (paths.length >= max) { toast(`最多 ${max} 份附件`, 'warn'); break; }
      const fd = new FormData();
      fd.append('file', f);
      fd.append('biz', biz);
      const item = document.createElement('div');
      item.className = 'attach-item';
      item.innerHTML = `<span class="up">↑</span><span class="nm">${esc(f.name)}</span><span class="sz">上传中…</span>`;
      const list = $('#__attList', zoneEl);
      if (list) list.appendChild(item);
      try {
        const h = {};
        const p2 = localStorage.getItem('t2s_pass');
        if (p2) h['X-T2S-Pass'] = p2;
        const r = await fetch('/api/upload', { method: 'POST', body: fd, headers: h });
        const j = await r.json();
        if (!j.ok) { item.querySelector('.sz').textContent = '失败'; item.querySelector('.up').textContent = '✗'; toast(j.message || '上传失败', 'bad'); continue; }
        paths.push(j.path); names.push(j.name || f.name);
        item.querySelector('.up').textContent = '✓'; item.querySelector('.up').className = 'ok';
        item.querySelector('.sz').textContent = '';
      } catch (e) {
        item.querySelector('.sz').textContent = '失败'; item.querySelector('.up').textContent = '✗';
        toast(e.message, 'bad');
      }
    }
    renderList();
    const lim = $('.attach-limit', zoneEl);
    if (lim) lim.textContent = `已上传 ${paths.length}/${max} 份`;
  }

  render();

  return {
    getPaths: () => paths.slice(),
    isEmpty: () => paths.length === 0,
    reset: () => { paths.length = 0; names.length = 0; render(); },
  };
}

// ---------- 预览确认弹层（三个页面共用） ----------
/** preview 须含 confirm_token（由 /api/parse 或 /api/resolve 签发）；
 *  extra = {source: 'ai'|'form', rawText: 用户原话（AI 入口传，审计用）} */
function showPreview(preview, draft, onConfirm, attachPaths, extra) {
  const old = $('#__mask');
  if (old) old.remove();
  extra = extra || {};

  const p = preview || {};
  const toCreate = p.to_create || [];
  const warns = p.warnings || [];
  const errs = p.errors || [];
  const ai = p.ai || {};
  const aiFlag = (ai.confidence && ai.confidence !== 'high') || (ai.note || '').trim()
    || (ai.problems || []).length;

  const candChips = (cands, pick) => (cands || []).length
    ? `<div style="margin-top:6px"><span class="hint">是指：</span>${cands.map(c =>
        `<span class="tag tag-blue" style="cursor:pointer" data-pick="${esc(pick)}" data-cand="${esc(c)}">${esc(c)}</span>`).join(' ')}</div>`
    : '';

  const rows = (p.items || []).map((it, i) => `
    <tr>
      <td>${esc(it.name)} ${it.will_create
        ? '<span class="tag tag-new">将新建</span>'
        : (it.match_kind === 'fuzzy' ? `<span class="tag tag-fuzzy">匹配→${esc(it.matched_name)}</span>` : '<span class="tag tag-ok">已有</span>')}
        ${candChips(it.candidates, 'item:' + i)}</td>
      <td class="num">${num(it.qty)}</td>
      <td>${esc(it.unit)}</td>
      <td class="num${it.price_missing ? ' price-zero' : ''}">${money(it.price)}${it.price_missing ? ' <span class="tag tag-new" title="原话里可能没有说价格">单价?</span>' : ''}</td>
      <td class="num"><b>${money(it.amount)}</b></td>
    </tr>`).join('');

  const html = `
  <div class="mask show" id="__mask">
    <div class="modal">
      <h2><span class="bar"></span>确认单据　<span class="tag tag-blue">${esc(p.biz_label || '')}</span></h2>

      ${errs.length ? `<div class="alert alert-error"><b>无法提交：</b><br>${errs.map(esc).join('<br>')}</div>` : ''}

      ${aiFlag ? `
      <div class="alert ${ai.confidence === 'low' ? 'alert-warn' : 'alert-info'}">
        <b>AI 提示（置信度：${esc(ai.confidence || '—')}）：</b>${esc(ai.note || '')}
        ${(ai.problems || []).length ? '<br>解析问题：' + ai.problems.map(esc).join('；') : ''}
      </div>` : ''}

      ${toCreate.length ? `
      <div class="alert alert-warn">
        <b>⚠ 以下资料系统里还没有，写入时会自动帮你建好：</b>
        <ul style="margin:8px 0 0 20px;padding:0">${toCreate.map(x => `<li>${esc(x)}</li>`).join('')}</ul>
        <div class="hint" style="margin-top:8px">不用先去「基础资料」里新增，确认后一并创建。</div>
      </div>` : ''}

      ${warns.length ? `<div class="alert alert-info"><b>提醒：</b><br>${warns.map(esc).join('<br>')}</div>` : ''}

      <div class="field">
        <div><b>${esc((p.party && p.party.type) || '往来单位')}：</b>${esc((p.party && p.party.name) || '—')}
          ${p.party && p.party.will_create ? '<span class="tag tag-new">将新建</span>'
            : (p.party && p.party.match_kind === 'fuzzy' ? `<span class="tag tag-fuzzy">匹配→${esc(p.party.matched_name)}</span>` : '<span class="tag tag-ok">已有</span>')}
          ${p.party && !p.party.matched ? candChips(p.party.candidates, 'party') : ''}
        </div>
        <div style="margin-top:6px"><b>仓库：</b>${esc((p.depot && p.depot.name) || '—')}
          ${p.depot && p.depot.will_create ? '<span class="tag tag-new">将新建</span>' : '<span class="tag tag-ok">已有</span>'}
        </div>
        <div style="margin-top:6px"><b>日期：</b>${esc(p.oper_time || '')}</div>
      </div>

      <div class="tbl-wrap" style="margin-top:14px">
        <table class="xtable">
          <thead><tr><th>商品</th><th class="num">数量</th><th>单位</th><th class="num">单价</th><th class="num">金额</th></tr></thead>
          <tbody>${rows || '<tr><td colspan="5" class="muted center">没有明细</td></tr>'}</tbody>
        </table>
      </div>

      <div class="total-row">合计：<span class="amount">￥${money(p.total)}</span></div>

      ${(attachPaths && attachPaths.length) ? `
      <div class="field" style="margin-top:14px">
        <label>附件（${attachPaths.length} 份，已上传到 ERP）</label>
        <div class="attach-list" style="margin-top:6px">
          ${attachPaths.map(x => `<div class="attach-item"><span class="ok">✓</span><span class="nm">${esc(x.name)}</span></div>`).join('')}
        </div>
      </div>` : ''}

      <div class="row-btns" style="margin-top:18px">
        <button class="btn btn-ghost" id="__cancel">再改改</button>
        <button class="btn btn-primary btn-lg" id="__ok" ${errs.length ? 'disabled' : ''}>确认写入 ERP</button>
      </div>
    </div>
  </div>`;

  document.body.insertAdjacentHTML('beforeend', html);
  const mask = $('#__mask');
  const close = () => mask.remove();

  $('#__cancel').onclick = close;
  mask.onclick = e => { if (e.target === mask) close(); };

  // 候选点选：改草稿 → 重新只读预览（旧令牌作废，新预览带新令牌）
  mask.querySelectorAll('[data-pick]').forEach(chip => {
    chip.onclick = async () => {
      const cand = chip.dataset.cand;
      const d2 = JSON.parse(JSON.stringify(draft));
      if (chip.dataset.pick === 'party') d2.party = cand;
      else {
        const i = parseInt(chip.dataset.pick.split(':')[1], 10);
        if (d2.items && d2.items[i]) d2.items[i].name = cand;
      }
      chip.textContent = '…';
      try {
        const r = await api('/api/resolve', { method: 'POST', body: { draft: d2, source: extra.source || 'form' } });
        if (!r.ok) { toast(r.message || '重新预览失败', 'bad'); return; }
        close();
        showPreview(r.preview, r.draft, onConfirm, attachPaths, extra);
      } catch (e) { toast(e.message, 'bad'); }
    };
  });

  $('#__ok').onclick = async () => {
    const btn = $('#__ok');
    btn.disabled = true;
    btn.textContent = '写入中…';
    try {
      // 把已上传的附件路径合并进 draft（逗号串，对应 DepotHead.fileName）
      const d = Object.assign({}, draft);
      if (attachPaths && attachPaths.length) {
        d.file_name = attachPaths.map(x => x.path).join(',');
      }
      const res = await api('/api/commit', { method: 'POST', body: {
        draft: d,
        confirm_token: p.confirm_token || '',
        source: extra.source || '',
        raw_text: extra.rawText || '',
      } });
      close();
      if (onConfirm) onConfirm(res);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = '确认写入 ERP';
      toast(e.message, 'bad');
    }
  };
}

// ---------- 顶栏导航 ----------
function topbar(active, title, sub) {
  const tabs = [['/', '首页'], ['/ai', '对话录单'], ['/form', '表单录单'], ['/ask', '问一问'], ['/dashboard', '经营看板']];
  return `
  <div class="topbar">
    <div class="logo">T2S</div>
    <div>
      <h1>${esc(title)}</h1>
      <div class="sub">${esc(sub || '')}</div>
    </div>
    <div class="spacer"></div>
    <div class="brand-mark">Talk2Stock<span>ERP Agent</span></div>
    <div class="navtabs">
      ${tabs.map(([h, t]) => `<a href="${h}" class="${h === active ? 'on' : ''}">${t}</a>`).join('')}
    </div>
  </div>`;
}
