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

// ---------- 就地输入弹框（替代 prompt） ----------
/** WebView/部分内嵌浏览器不支持 prompt()，导致公网部署下取口令/操作人直接卡死。
    统一用自绘 modal：Enter 确认、Esc 取消、输入框自动聚焦。返回 string 或 null（取消）。 */
function askInline(opt) {
  const o = Object.assign({ title: '请输入', hint: '', value: '', okText: '确定', cancelText: '取消', password: false, maxlen: 60 }, opt || {});
  return new Promise(resolve => {
    const old = document.getElementById('__askInline');
    if (old) old.remove();
    const mask = document.createElement('div');
    mask.id = '__askInline';
    mask.className = 'mask show';
    mask.setAttribute('role', 'dialog');
    mask.setAttribute('aria-modal', 'true');
    mask.innerHTML = `
      <div class="modal" style="max-width:440px">
        <button class="modal-x" aria-label="关闭">×</button>
        <h3>${esc(o.title)}</h3>
        ${o.hint ? `<div class="hint" style="margin-top:6px">${esc(o.hint)}</div>` : ''}
        <div class="pv-form">
          <div class="pv-line">
            <input id="__askInlineInput" type="${o.password ? 'password' : 'text'}" maxlength="${o.maxlen}"
              value="${esc(o.value)}" autocomplete="off" spellcheck="false"
              style="flex:1;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:#fff;font-size:15px">
          </div>
        </div>
        <div class="row-btns mt-4">
          <button class="btn btn-ghost" id="__askInlineCancel">${esc(o.cancelText)}</button>
          <span class="hint flex-1">Enter 确认 · Esc 取消</span>
          <button class="btn btn-primary" id="__askInlineOk">${esc(o.okText)}</button>
        </div>
      </div>`;
    document.body.appendChild(mask);
    const input = mask.querySelector('#__askInlineInput');
    const done = v => { if (mask._done) return; mask._done = true; if (untrap) untrap(); mask.remove(); resolve(v); };
    const ok = () => done(input.value);
    mask.querySelector('#__askInlineOk').onclick = ok;
    mask.querySelector('#__askInlineCancel').onclick = () => done(null);
    mask.querySelector('.modal-x').onclick = () => done(null);
    mask.addEventListener('mousedown', e => { if (e.target === mask) done(null); });
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); ok(); }
      else if (e.key === 'Escape') { e.preventDefault(); done(null); }
      e.stopPropagation();
    });
    const untrap = trapFocus(mask.querySelector('.modal'), () => done(null));
    input.focus();
    input.select();
  });
}
async function askPass() {
  const p = await askInline({
    title: '需要访问口令',
    hint: '该服务开启了访问口令（T2S_DEMO_PASS）。输入后只存在本机浏览器。',
    okText: '验证并继续', password: true,
  });
  return p;
}

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
    const p = await askPass();
    if (p) { localStorage.setItem('t2s_pass', p); return api(path, opts); }
    throw new Error('需要访问口令');
  }
  const txt = await r.text();
  let j;
  try { j = JSON.parse(txt); }
  catch (e) { throw new Error('服务返回异常：' + txt.slice(0, 120)); }
  return j;
}

// 操作人标识：只存本地，用于基线实验归因（写进 audit.jsonl 的 operator 字段）
async function ensureOperator() {
  let op = localStorage.getItem('t2s_operator');
  if (!op) {
    const v = await askInline({
      title: '你的名字或编号',
      hint: '只存本机，用于实验记录归因（写入审计）。可留空。',
      okText: '保存', maxlen: 40,
    });
    op = (v || '').trim().slice(0, 40);
    if (op) localStorage.setItem('t2s_operator', op);
  }
  return op || '';
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

/* ---------- 通知（右下角堆叠 · 可关闭 · 令牌着色） ----------
   替代旧版顶部单条 toast；旧调用签名 toast(msg, kind) 完全兼容。
   kind: 'bad' | 'warn' | 'ok'（默认 ok）。 */
const TOAST_ICON = { ok: '✓', warn: '⚠', bad: '✕' };
function toastBox() {
  let b = $('.toasts');
  if (!b) {
    b = document.createElement('div');
    b.className = 'toasts';
    b.setAttribute('role', 'status');
    b.setAttribute('aria-live', 'polite');
    document.body.appendChild(b);
  }
  return b;
}
function toast(msg, kind) {
  const k = kind === 'bad' ? 'bad' : kind === 'warn' ? 'warn' : 'ok';
  const box = toastBox();
  const el = document.createElement('div');
  el.className = 'toast-n ' + k;
  el.innerHTML = `<span class="t-ico">${TOAST_ICON[k]}</span><span class="t-body"></span>
    <button class="t-x" aria-label="关闭这条通知">×</button>`;
  el.querySelector('.t-body').textContent = msg;
  const close = () => {
    if (el._gone) return;
    el._gone = true;
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 220);
  };
  el.querySelector('.t-x').onclick = close;
  box.appendChild(el);
  // 同时最多 4 条，超出从最旧的开始收
  while (box.children.length > 4) box.firstElementChild.remove();
  clearTimeout(el._h);
  el._h = setTimeout(close, k === 'bad' ? 6000 : 3400);
  return el;
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
    let html = items.map((x, i) => {
      const sub = [x.model, x.unit].filter(Boolean).join(' · ');   // 同名档案靠型号区分
      return `<div class="ac-item${i === idx ? ' sel' : ''}" data-i="${i}">${esc(x.name)}` +
      (sub ? `<span class="sub">${esc(sub)}</span>` : '') + `</div>`;
    }).join('');
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
    input.blur();          // 选定后收起焦点，避免焦点回归时列表再次弹出
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
/** 可选项不占地方：没传附件时折叠成一行，传了才展开完整列表。
 *  拖拽监听绑在 zoneEl 上只挂一次（旧实现每次 render 重复挂，drop 会触发多次）。 */
function AttachmentManager(zoneEl, opts) {
  opts = opts || {};
  const biz = opts.biz || 'bill';
  const max = opts.max || 4;            // 原版限制单单据最多 4 份
  const paths = [];                     // 已上传到 ERP 的相对路径串
  const names = [];                     // 对应文件名（用于展示）

  function pickFile() { const f = $('#__attFile', zoneEl); if (f) f.click(); }

  function bindDrop() {
    if (zoneEl._dropBound) return;
    zoneEl._dropBound = true;
    zoneEl.addEventListener('dragover', e => { e.preventDefault(); zoneEl.classList.add('drag'); });
    zoneEl.addEventListener('dragleave', () => zoneEl.classList.remove('drag'));
    zoneEl.addEventListener('drop', e => {
      e.preventDefault(); zoneEl.classList.remove('drag');
      if (e.dataTransfer.files.length) upload(e.dataTransfer.files);
    });
  }

  function render() {
    bindDrop();
    if (!paths.length) {
      // 折叠态：一行搞定，不抢输入区版面
      zoneEl.innerHTML = `
        <div class="attach-drop attach-collapsed" id="__attDrop" title="点击或拖入文件">
          <span class="clip">📎</span>
          <button type="button" class="btn btn-sm btn-ghost" id="__attBtn">＋ 附件</button>
          <span>发票 / 单据扫描件 / 合同（选填，最多 ${max} 份，图片或 PDF）</span>
          <input type="file" id="__attFile" multiple accept="image/*,application/pdf" style="display:none">
        </div>`;
    } else {
      zoneEl.innerHTML = `
        <div class="attach-drop" id="__attDrop">
          <button type="button" class="btn btn-sm btn-ghost" id="__attBtn">＋ 继续上传</button>
          <span>原发票 / 单据扫描件 / 合同（最多 ${max} 份，图片或 PDF）</span>
          <input type="file" id="__attFile" multiple accept="image/*,application/pdf"
                 style="display:none">
        </div>
        <div class="attach-list" id="__attList"></div>
        <div class="attach-limit">已上传 ${paths.length}/${max} 份</div>`;
      renderList();
    }
    $('#__attBtn', zoneEl).onclick = () => pickFile();
    $('#__attDrop', zoneEl).onclick = e => { if (e.target.id === '__attDrop') pickFile(); };
    $('#__attFile', zoneEl).onchange = () => {
      if (zoneEl.querySelector('#__attFile').files.length) upload(zoneEl.querySelector('#__attFile').files);
    };
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
      paths.splice(+x.dataset.i, 1); names.splice(+x.dataset.i, 1); render();
    });
  }

  async function upload(fileList) {
    if (paths.length + fileList.length > max) {
      toast(`最多 ${max} 份附件`, 'warn'); return;
    }
    if (!paths.length) render();            // 从折叠态展开，让占位条有地方放
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
        let h = {};
        const p2 = localStorage.getItem('t2s_pass');
        if (p2) h['X-T2S-Pass'] = p2;
        let r = await fetch('/api/upload', { method: 'POST', body: fd, headers: h });
        if (r.status === 401) {
          const p = await askPass();
          if (!p) { item.querySelector('.sz').textContent = '失败'; item.querySelector('.up').textContent = '✗'; continue; }
          localStorage.setItem('t2s_pass', p);
          h = { 'X-T2S-Pass': p };
          r = await fetch('/api/upload', { method: 'POST', body: fd, headers: h });
        }
        const j = await r.json();
        if (!j.ok) { item.querySelector('.sz').textContent = '失败'; item.querySelector('.up').textContent = '✗'; toast(j.message || '上传失败', 'bad'); continue; }
        paths.push(j.path); names.push(j.name || f.name);
      } catch (e) {
        toast(e.message, 'bad');
      }
    }
    render();
  }

  render();

  return {
    getPaths: () => paths.slice(),
    isEmpty: () => paths.length === 0,
    reset: () => { paths.length = 0; names.length = 0; render(); },
  };
}

// ---------- 预览确认弹层（三个页面共用，支持就地改数） ----------
/** preview 须含 confirm_token（由 /api/parse 或 /api/resolve 签发）；
 *  draft 为可编辑草稿（就地改数量/单价/仓库/往来单位/日期/业务类型）；
 *  extra = {source: 'ai'|'form', rawText: 用户原话（AI 入口传，审计用）,
 *           onCancel: 弹层被取消关闭时回调（收到当前草稿，供「修正模式」用）}
 *  就地改数后闸门不受影响：改动只落在草稿上，提交前必须重走 /api/resolve
 *  换取与新内容绑定的新令牌（旧令牌对旧内容签发，直接提交会被闸门拒绝）。 */
function showPreview(preview, draft, onConfirm, attachPaths, extra) {
  const old = $('#__mask');
  if (old) old.remove();
  extra = extra || {};

  const st = {
    preview: preview || {},
    draft: JSON.parse(JSON.stringify(draft || {})),
    attach: (attachPaths || []).slice(),
    dirty: false,
    done: false,               // 确认流程已走完，关闭时不再触发 onCancel
  };
  let detachKey = null;        // 当前弹层的键盘监听卸载器（重渲染时先卸旧的，防残留）
  let detachTrap = null;       // 焦点陷阱卸载器

  // 候选 chip：商品候选是 {id,name,model,standard,unit}（同名档案靠型号区分），
  // 往来单位候选仍是纯名字字符串——两种都渲染；商品 chip 带 data-cand-mid，
  // 点选后 material_id 直达档案，不再靠名字匹配
  const candChips = (cands, pick) => (cands || []).length
    ? `<div class="mt-1"><span class="hint">是指：</span>${cands.map(c => {
        const o = (c && typeof c === 'object');
        const label = o ? [c.name, c.model, c.standard].filter(Boolean).join(' · ') : c;
        const mid = o && c.id ? ` data-cand-mid="${c.id}"` : '';
        return `<span class="tag tag-blue" style="cursor:pointer" data-pick="${esc(pick)}" data-cand="${esc(o ? c.name : c)}"${mid}>${esc(label)}</span>`;
      }).join(' ')}</div>`
    : '';

  // 与服务端 draft.validate 同口径的轻校验，编辑后即时给出反馈
  function clientErrors() {
    const d = st.draft, errs = [];
    if (!(d.party || '').trim()) errs.push('缺少往来单位名称');
    const items = d.items || [];
    if (!items.length) errs.push('至少需要一项商品明细');
    items.forEach((it, i) => {
      if (!(it.name || '').trim()) errs.push(`第${i + 1}行缺商品名称`);
      if (num(it.qty) <= 0) errs.push(`第${i + 1}行数量必须大于 0`);
      if (num(it.price) < 0) errs.push(`第${i + 1}行单价不能为负`);
    });
    return errs;
  }

  const curTotal = () => (st.draft.items || [])
    .reduce((t, it) => t + num(it.qty) * num(it.price), 0);

  // 与服务端 _norm_text/_mat_composite 同口径：名字+型号合成串比较，
  // 「螺丝 M8×20」要认出是已有档案「螺丝 / 型号M8×20」，不能误报「将新建」
  const matNorm = s => String(s || '').replace(/[()（）]/g, '').replace(/[×✕ｘ]/g, 'x')
    .trim().toLowerCase().replace(/\s/g, '');

  function itemTag(i) {
    const it = (st.draft.items || [])[i];
    if (!it || !(it.name || '').trim()) return '<span class="muted">未填</span>';
    const n = matNorm(it.name);
    const hit = (MD.materials || []).find(x =>
      matNorm(x.name) === n || matNorm(x.name + (x.model || '') + (x.standard || '')) === n);
    return hit
      ? '<span class="tag tag-ok">已有</span>'
      : '<span class="tag tag-new">将新建</span>';
  }

  function partyTag() {
    const d = st.draft;
    if (!(d.party || '').trim()) return '';
    return findExact(partyPool(d.biz), d.party)
      ? '<span class="tag tag-ok">已有</span>'
      : '<span class="tag tag-new">将新建</span>';
  }

  function render() {
    if (detachKey) detachKey();               // 重渲染前卸掉上一轮的键盘监听
    const p = st.preview, d = st.draft;
    const errs = p.errors || [];
    const warns = p.warnings || [];
    const toCreate = p.to_create || [];
    const ai = p.ai || {};
    const aiFlag = (ai.confidence && ai.confidence !== 'high') || (ai.note || '').trim()
      || (ai.problems || []).length;
    const pitem = i => (p.items || [])[i] || {};

    const rows = (d.items || []).map((it, i) => {
      const pv = pitem(i);
      return `<tr>
        <td>
          <div class="pv-name-row"><div class="ac-wrap" style="flex:1"><input class="pv-name" data-i="${i}" value="${esc(it.name)}" placeholder="商品名称" autocomplete="off"></div></div>
          <div class="pv-sub">${itemTag(i)}${(pv.match_kind === 'fuzzy' || pv.match_kind === 'picked') && pv.matched_name ? ` <span class="tag ${pv.match_kind === 'picked' ? 'tag-ok' : 'tag-fuzzy'}">匹配→${esc(pv.matched_name)}${pv.matched_model ? ' ' + esc(pv.matched_model) : ''}</span>` : ''}${candChips(pv.candidates, 'item:' + i)}</div>
        </td>
        <td><input class="pv-qty num" data-i="${i}" type="number" step="any" value="${it.qty ?? ''}" placeholder="0"></td>
        <td><input class="pv-unit" data-i="${i}" value="${esc(it.unit || '')}" placeholder="个"></td>
        <td><input class="pv-price num${pv.price_missing ? ' price-zero' : ''}" data-i="${i}" type="number" step="any" value="${it.price ?? ''}" placeholder="0.00">${pv.price_missing ? ' <span class="tag tag-new" title="原话里可能没有说价格">单价?</span>' : ''}</td>
        <td class="num"><b class="pv-amt" data-i="${i}">${money(num(it.qty) * num(it.price))}</b></td>
        <td><button class="xdel" data-del="${i}" title="删除该行">×</button></td>
      </tr>`;
    }).join('');

    const depotOpts = (MD.depots || []).map(x => `<option value="${esc(x.name)}">`).join('');
    const dtVal = (d.oper_time || '').replace(' ', 'T').slice(0, 16);

    const html = `
    <div class="mask show" id="__mask">
      <div class="modal">
        <button class="modal-x" id="__modalX" title="关闭（Esc）" aria-label="关闭">×</button>
        <h2><span class="bar"></span>确认单据　
          <select id="__biz" class="biz-sel">${Object.entries(BIZ).map(([k, v]) =>
            `<option value="${k}"${k === d.biz ? ' selected' : ''}>${v.label}</option>`).join('')}</select>
        </h2>

        <div id="__pErrs">${errs.length ? `<div class="alert alert-error"><b>无法提交：</b><br>${errs.map(esc).join('<br>')}</div>` : ''}</div>

        ${aiFlag ? `
        <div class="alert ${ai.confidence === 'low' ? 'alert-warn' : 'alert-info'}">
          <b>AI 提示（置信度：${esc(ai.confidence || '—')}）：</b>${esc(ai.note || '')}
          ${(ai.problems || []).length ? '<br>解析问题：' + ai.problems.map(esc).join('；') : ''}
        </div>` : ''}

        ${toCreate.length ? `
        <div class="alert alert-warn">
          <b>⚠ 以下资料系统里还没有，写入时会自动帮你建好：</b>
          <ul style="margin:8px 0 0 20px;padding:0">${toCreate.map(x => `<li>${esc(x)}</li>`).join('')}</ul>
          <div class="hint mt-2">不用先去「基础资料」里新增，确认后一并创建。</div>
        </div>` : ''}

        ${warns.length ? `<div class="alert alert-info"><b>提醒：</b><br>${warns.map(esc).join('<br>')}</div>` : ''}

        <div class="pv-form">
          <div class="pv-line">
            <label id="__partyLbl">${esc((p.party && p.party.type) || (BIZ[d.biz] && BIZ[d.biz].party) || '往来单位')}</label>
            <div class="ac-wrap pv-grow"><input id="__party" value="${esc(d.party || '')}" placeholder="名称，没有的会自动新建" autocomplete="off"></div>
            <span id="__partyTag">${partyTag()}</span>
          </div>
          <div id="__partyCand">${d.party && p.party && !p.party.matched ? candChips(p.party.candidates, 'party') : ''}</div>
          <div class="pv-line">
            <label>仓库</label>
            <div class="pv-grow"><input id="__depot" list="__depotList" value="${esc(d.depot || '')}" placeholder="留空用默认仓"></div>
            <datalist id="__depotList">${depotOpts}</datalist>
            ${p.depot && p.depot.will_create && d.depot ? '<span class="tag tag-new">将新建</span>' : '<span class="tag tag-ok">已有</span>'}
          </div>
          <div class="pv-line">
            <label>日期</label>
            <div class="pv-grow">
              <input id="__otime" type="datetime-local" value="${esc(dtVal)}">
              ${(d.oper_time || '').trim() ? '' : '<div class="hint" style="margin:0">留空 = 按当前时间写入</div>'}
            </div>
          </div>
        </div>

        <div class="tbl-wrap mt-3">
          <table class="xtable">
            <thead><tr><th>商品</th><th class="num" style="width:86px">数量</th><th style="width:76px">单位</th><th class="num" style="width:110px">单价</th><th class="num" style="width:100px">金额</th><th style="width:40px"></th></tr></thead>
            <tbody id="__pvBody">${rows || '<tr><td colspan="6" class="muted center">没有明细</td></tr>'}</tbody>
          </table>
        </div>
        <button class="btn btn-sm btn-ghost mt-1" id="__addRow">＋ 加一行</button>

        <div class="total-row">合计：<span class="amount" id="__pvTotal">￥${money(curTotal())}</span></div>

        <div class="pv-dirty hidden" id="__dirtyBar">✏ 内容已修改：先「刷新预览」重新校验档案匹配 / 库存 / 合计，再确认写入。</div>

        ${(st.attach && st.attach.length) ? `
        <div class="field mt-3">
          <label>附件（${st.attach.length} 份，已上传到 ERP）</label>
          <div class="attach-list mt-1">
            ${st.attach.map(x => `<div class="attach-item"><span class="ok">✓</span><span class="nm">${esc(x.name)}</span></div>`).join('')}
          </div>
        </div>` : ''}

        <div class="row-btns mt-4">
          <button class="btn btn-ghost" id="__cancel">再改改</button>
          <span class="hint" style="flex:1">Esc 关闭 · ⌘/Ctrl+Enter 确认 · 表格里可直接改数</span>
          <button class="btn btn-primary btn-lg" id="__ok" ${errs.length ? 'disabled' : ''}>确认写入 ERP</button>
        </div>
      </div>
    </div>`;

    document.body.insertAdjacentHTML('beforeend', html);
    bind();
  }

  function syncRow(i) {
    const it = (st.draft.items || [])[i];
    if (!it) return;
    const amt = $(`.pv-amt[data-i="${i}"]`);
    if (amt) amt.textContent = money(num(it.qty) * num(it.price));
    $$('tr', $('#__pvBody')).forEach((tr, r) => {
      if (r !== i) return;
      const sub = $('.pv-sub', tr);
      if (sub) {
        const pv = (st.preview.items || [])[r] || {};
        sub.innerHTML = itemTag(r) +
          (pv.match_kind === 'fuzzy' && pv.matched_name ? ` <span class="tag tag-fuzzy">匹配→${esc(pv.matched_name)}</span>` : '');
      }
    });
    $('#__pvTotal').textContent = '￥' + money(curTotal());
  }

  function setDirty() {
    st.dirty = true;
    const bar = $('#__dirtyBar');
    if (bar) bar.classList.remove('hidden');
    const ok = $('#__ok');
    if (ok && !ok.disabled) ok.textContent = '刷新预览 ↻';
  }

  function refreshOkState() {
    const ok = $('#__ok');
    if (!ok) return;
    const errs = clientErrors();
    ok.disabled = errs.length > 0;
    ok.title = errs.length ? errs.join('；') : '';
    if (!errs.length && st.dirty) ok.textContent = '刷新预览 ↻';
    else if (!st.dirty) ok.textContent = '确认写入 ERP';
    $('#__pErrs').innerHTML = errs.length
      ? `<div class="alert alert-error"><b>请先修正：</b><br>${errs.map(esc).join('<br>')}</div>` : '';
  }

  function bindAutocompletes() {
    autocomplete($('#__party'), () => partyPool(st.draft.biz), null, () => {
      st.draft.party = $('#__party').value.trim();
      $('#__partyTag').innerHTML = partyTag();
      setDirty(); refreshOkState();
    });
    $$('.pv-name').forEach(inp => {
      // pickedNow：autocomplete 的 mousedown 会先 onPick 再 onType（同一次事件），
      // 靠这个标志区分「点选档案」与「手改名字」——后者必须清掉 material_id
      let pickedNow = false;
      autocomplete(inp, () => MD.materials,
        it2 => {
          const i = +inp.dataset.i;
          const it = st.draft.items[i];
          if (!it) return;
          pickedNow = true;
          if (it2.id) it.material_id = it2.id;   // 选中即锁定档案（同名规格不再靠名字猜）
          if (it2.unit && !it.unit) {
            it.unit = it2.unit;
            const u = $(`.pv-unit[data-i="${i}"]`);
            if (u) u.value = it2.unit;
          }
        },
        () => {
          const i = +inp.dataset.i;
          const it = st.draft.items[i];
          if (!it) return;
          it.name = inp.value.trim();
          if (!pickedNow) delete it.material_id;  // 手改名字＝放弃锁定的档案
          pickedNow = false;
          syncRow(i); setDirty(); refreshOkState();
        });
    });
  }

  function bind() {
    const mask = $('#__mask');
    const close = () => {
      if (detachKey) detachKey();
      detachKey = null;
      if (detachTrap) detachTrap();
      detachTrap = null;
      mask.remove();
      if (!st.done && extra.onCancel) extra.onCancel(st.draft);
    };

    // 焦点陷阱：Tab 只在弹层内循环；Esc 复用 close
    if (detachTrap) detachTrap();
    detachTrap = trapFocus(mask, close);

    // 就地编辑：字段输入 → 写回草稿（先改数，提交前统一刷新预览重签令牌）
    $('#__pvBody').addEventListener('input', e => {
      const t = e.target;
      const i = +t.dataset.i;
      const it = (st.draft.items || [])[i];
      if (!it) return;
      if (t.classList.contains('pv-qty')) it.qty = num(t.value);
      else if (t.classList.contains('pv-price')) it.price = num(t.value);
      else if (t.classList.contains('pv-unit')) it.unit = t.value.trim() || '个';
      else return;   // pv-name 由 autocomplete 的 onType 回调处理
      syncRow(i); setDirty(); refreshOkState();
    });

    $('#__pvBody').addEventListener('click', e => {
      const del = e.target.closest('[data-del]');
      if (!del) return;
      const i = +del.dataset.del;
      const items = st.draft.items || [];
      if (items.length > 1) items.splice(i, 1);
      else { items[0] = { name: '', qty: 0, unit: '个', price: 0 }; }
      st.dirty = true;
      render();
      toast('已删除一行，刷新预览后生效', 'warn');
    });

    $('#__addRow').onclick = () => {
      (st.draft.items = st.draft.items || []).push({ name: '', qty: 0, unit: '个', price: 0 });
      render();
      const inps = $$('.pv-name');
      if (inps.length) inps[inps.length - 1].focus();
    };

    $('#__party').addEventListener('input', () => {
      st.draft.party = $('#__party').value.trim();
      $('#__partyTag').innerHTML = partyTag();
      setDirty(); refreshOkState();
    });
    $('#__depot').addEventListener('input', () => {
      st.draft.depot = $('#__depot').value.trim();
      setDirty();
    });
    $('#__otime').addEventListener('change', () => {
      const v = $('#__otime').value || '';
      if (v) st.draft.oper_time = v.replace('T', ' ') + (v.length === 16 ? ':00' : '');
      setDirty();
    });
    $('#__biz').addEventListener('change', () => {
      st.draft.biz = $('#__biz').value;
      $('#__partyLbl').textContent = (BIZ[st.draft.biz] || {}).party || '往来单位';
      $('#__partyTag').innerHTML = partyTag();
      setDirty(); refreshOkState();
    });

    bindAutocompletes();

    $('#__modalX').onclick = close;
    $('#__cancel').onclick = close;
    mask.onclick = e => { if (e.target === mask) close(); };

    // 键盘：Esc 关闭；⌘/Ctrl+Enter = 点主按钮（改过数=刷新预览，没改=写入）
    const onKey = e => {
      if (e.key === 'Escape') { close(); }
      else if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); $('#__ok').click(); }
    };
    document.addEventListener('keydown', onKey);
    detachKey = () => document.removeEventListener('keydown', onKey);

    // 候选点选：改草稿 → 重新只读预览（旧令牌作废，新预览带新令牌）
    mask.querySelectorAll('[data-pick]').forEach(chip => {
      chip.onclick = async () => {
        const cand = chip.dataset.cand;
        if (chip.dataset.pick === 'party') st.draft.party = cand;
        else {
          const i = parseInt(chip.dataset.pick.split(':')[1], 10);
          const it = st.draft.items && st.draft.items[i];
          if (!it) return;
          it.name = cand;
          // 同名不同规格：material_id 锁定点选的档案，重新预览/写入都不再靠名字猜
          if (chip.dataset.candMid) it.material_id = +chip.dataset.candMid;
        }
        chip.textContent = '…';
        try {
          const r = await api('/api/resolve', { method: 'POST', body: { draft: st.draft, source: extra.source || 'form' } });
          if (!r.ok) { toast(r.message || '重新预览失败', 'bad'); return; }
          st.done = true;              // 已进入新预览，不算取消，不触发 onCancel
          close();
          showPreview(r.preview, r.draft, onConfirm, st.attach, extra);
        } catch (e) { toast(e.message, 'bad'); }
      };
    });

    // 主按钮：没改过 = 直接带令牌写入；改过 = 先刷新预览（重签令牌），再人工确认
    $('#__ok').onclick = async () => {
      const btn = $('#__ok');
      if (st.dirty) {
        btn.disabled = true; btn.textContent = '刷新预览中…';
        try {
          const r = await api('/api/resolve', { method: 'POST', body: { draft: st.draft, source: extra.source || 'form' } });
          if (!r.ok) {
            btn.disabled = false; btn.textContent = '刷新预览 ↻';
            toast(r.message || '刷新预览失败', 'bad');
            return;
          }
          st.done = true;              // 刷新是延续，不是取消
          close();
          showPreview(r.preview, r.draft, onConfirm, st.attach, Object.assign({}, extra, { refreshed: true }));
          toast('预览已按修改刷新，请再次确认', 'warn');
        } catch (e) {
          btn.disabled = false; btn.textContent = '刷新预览 ↻';
          toast(e.message, 'bad');
        }
        return;
      }
      btn.disabled = true;
      btn.textContent = '写入中…';
      try {
        const d = Object.assign({}, st.draft);
        if (st.attach.length) {
          d.file_name = st.attach.map(x => x.path).join(',');
        }
        const res = await api('/api/commit', { method: 'POST', body: {
          draft: d,
          confirm_token: st.preview.confirm_token || '',
          source: extra.source || '',
          raw_text: extra.rawText || '',
          operator: await ensureOperator(),
        } });
        st.done = true;
        close();
        if (onConfirm) onConfirm(res, d);   // 把实际写入的草稿（含就地改数）回传给页面
      } catch (e) {
        btn.disabled = false;
        btn.textContent = '确认写入 ERP';
        toast(e.message, 'bad');
      }
    };

    if (extra.refreshed) $('#__ok').classList.add('btn-pulse');
    refreshOkState();
  }

  render();
}

// ---------- 焦点陷阱（21st.dev Dialog 模式核心） ----------
/** 打开 mask 时调用，返回卸载函数。Tab 只在容器内循环，Esc 由调用方自行处理。 */
function trapFocus(container, onEscape) {
  const SEL = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  const first = container.querySelector('.modal-x') || container.querySelector(SEL);
  if (first) first.focus();
  const onKey = e => {
    if (e.key === 'Escape') { if (onEscape) { e.preventDefault(); onEscape(); } return; }
    if (e.key !== 'Tab') return;
    const items = Array.from(container.querySelectorAll(SEL)).filter(el => el.offsetParent !== null);
    if (!items.length) return;
    const f = items[0], l = items[items.length - 1];
    if (e.shiftKey && document.activeElement === f) { e.preventDefault(); l.focus(); }
    else if (!e.shiftKey && document.activeElement === l) { e.preventDefault(); f.focus(); }
  };
  container.addEventListener('keydown', onKey);
  return () => container.removeEventListener('keydown', onKey);
}

// ---------- 命令面板（⌘K / Ctrl+K） ----------
/** 只做导航与填入输入框，绝不触发解析/提交（沿用用户「少一步」偏好）。 */
const CMD_ITEMS = [
  { sec: '录单', ico: '✎', t: '对话录单', d: '说一句话，AI 拆成草稿', go: () => '/new?mode=ai' },
  { sec: '录单', ico: '▤', t: '表单录单', d: '逐字段精确控制，不依赖 AI', go: () => '/new?mode=form' },
  { sec: '经营', ico: '❝', t: '问一问', d: '库存 / 采销 / 欠款，随口问', go: () => '/ask' },
  { sec: '经营', ico: '◫', t: '经营看板', d: '趋势 / 账龄 / 预警，一页看完', go: () => '/dashboard' },
  { sec: '系统', ico: '⚙', t: '设置', d: 'ERP 连接与 AI 模型档案', go: () => '/settings' },
  { sec: '系统', ico: '▦', t: '原版 ERP', d: '新标签页打开 jshERP 本体', go: () => { window.open(ERP_URL, '_blank', 'noopener'); return null; } },
];

function commandPalette() {
  const old = $('#__cmdk');
  if (old) { old.remove(); return; }

  const mask = document.createElement('div');
  mask.id = '__cmdk';
  mask.className = 'cmdk-mask';
  mask.setAttribute('role', 'dialog');
  mask.setAttribute('aria-modal', 'true');
  mask.setAttribute('aria-label', '命令面板');
  mask.innerHTML = `
    <div class="cmdk">
      <div class="cmdk-input-row">
        <span class="k-ico">⌘</span>
        <input id="__cmdkQ" placeholder="跳转到页面、或执行命令…" autocomplete="off" spellcheck="false">
        <kbd>ESC</kbd>
      </div>
      <div class="cmdk-list" id="__cmdkList" role="listbox"></div>
      <div class="cmdk-foot">
        <span><kbd>↑</kbd><kbd>↓</kbd> 移动</span>
        <span><kbd>↵</kbd> 执行</span>
        <span><kbd>Tab</kbd> 在面板内循环</span>
        <span style="margin-left:auto">Talk2Stock 命令面板</span>
      </div>
    </div>`;
  document.body.appendChild(mask);

  const input = $('#__cmdkQ', mask), list = $('#__cmdkList', mask);
  let shown = CMD_ITEMS.map(it => Object.assign({}, it));
  let sel = 0;

  const fuzzy = (q, item) => {
    if (!q) return true;
    return (item.t + ' ' + item.d + ' ' + item.sec).toLowerCase().includes(q.toLowerCase());
  };

  function render(resetSel) {
    const q = input.value.trim();
    shown = CMD_ITEMS.map(it => Object.assign({}, it)).filter(it => fuzzy(q, it));
    if (resetSel !== false) sel = 0;
    if (sel >= shown.length) sel = Math.max(0, shown.length - 1);
    if (!shown.length) {
      list.innerHTML = '<div class="cmdk-empty">没有匹配的命令<br><span style="font-size:12.5px">试试「看板」「录单」「问一问」</span></div>';
      return;
    }
    let html = '', lastSec = '';
    shown.forEach((it, i) => {
      if (it.sec !== lastSec) { html += `<div class="cmdk-sec">${esc(it.sec)}</div>`; lastSec = it.sec; }
      html += `<div class="cmdk-item" role="option" aria-selected="${i === sel}" data-i="${i}">
        <span class="k-ico">${it.ico}</span>
        <span class="k-body"><span class="k-t">${esc(it.t)}</span><span class="k-d">${esc(it.d)}</span></span>
        <span class="k-hint">↵</span>
      </div>`;
    });
    list.innerHTML = html;
    const on = list.querySelector('[aria-selected="true"]');
    if (on) on.scrollIntoView({ block: 'nearest' });
  }

  function run(i) {
    const it = shown[i];
    if (!it) return;
    close();
    const r = it.go();
    if (r) location.href = r;
  }

  function close() {
    if (untrap) untrap();
    document.removeEventListener('keydown', onGlobal, true);
    mask.remove();
  }

  const onGlobal = e => {
    if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) { e.preventDefault(); close(); }
  };

  input.addEventListener('input', render);          // 输入变化 → 选中项回到第一项
  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown' || (e.key === 'n' && e.ctrlKey)) {
      e.preventDefault(); if (shown.length) { sel = (sel + 1) % shown.length; render(false); }
    } else if (e.key === 'ArrowUp' || (e.key === 'p' && e.ctrlKey)) {
      e.preventDefault(); if (shown.length) { sel = (sel - 1 + shown.length) % shown.length; render(false); }
    } else if (e.key === 'Enter') {
      e.preventDefault(); run(sel);
    } else if (e.key === 'Escape') {
      e.preventDefault(); close();
    }
  });
  list.addEventListener('click', e => {
    const el = e.target.closest('.cmdk-item');
    if (el) run(+el.dataset.i);
  });
  mask.addEventListener('mousedown', e => { if (e.target === mask) close(); });

  const untrap = trapFocus(mask.querySelector('.cmdk'), close);
  document.addEventListener('keydown', onGlobal, true);
  render();
  input.focus();
}

// 全局快捷键：⌘K / Ctrl+K。输入框内不抢（textarea/input 里 Ctrl+K 仍生效，便于随时唤起）
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K') && !$('#__cmdk')) {
    e.preventDefault();
    commandPalette();
  }
});

// 首次访问给一次轻提示，4.5s 后自动消失（sessionStorage 记住，不重复骚扰）
(function cmdkTip() {
  try {
    if (sessionStorage.getItem('t2s_cmdk_tip')) return;
    sessionStorage.setItem('t2s_cmdk_tip', '1');
  } catch (e) { return; }
  setTimeout(() => {
    if ($('#__cmdk')) return;
    const isMac = /Mac|iPhone|iPad/.test(navigator.platform || '');
    const t = document.createElement('div');
    t.className = 'cmdk-tip';
    t.innerHTML = `按 <kbd>${isMac ? '⌘' : 'Ctrl'}</kbd><kbd>K</kbd> 打开命令面板，快速跳转`;
    document.body.appendChild(t);
    setTimeout(() => { t.classList.add('leaving'); setTimeout(() => t.remove(), 500); }, 4500);
  }, 1200);
})();

// ---------- 侧边导航骨架 ----------
const NAV_ITEMS = [
  ['/', '工作台', '⌂'],
  ['/new', '录单', '✎'],
  ['/ask', '问一问', '❝'],
  ['/dashboard', '经营看板', '◫'],
  ['/settings', '设置', '⚙'],
];
const ERP_URL = location.protocol + '//' + location.hostname + ':8080';

function shell(active) {
  document.body.classList.add('has-shell');
  const nav = document.createElement('nav');
  nav.className = 'shell-nav';
  nav.innerHTML = `
    <div class="shell-brand">
      <div class="seal">账</div>
      <div><div class="t1">Talk2Stock</div><div class="t2">ERP AGENT</div></div>
    </div>
    <div class="nav-sec">主菜单</div>
    ${NAV_ITEMS.map(([h, t, i]) =>
      `<a class="nav-item${h === active ? ' on' : ''}" href="${h}"><span class="ico">${i}</span>${t}</a>`).join('')}
    <div class="nav-sec">系统</div>
    <a class="nav-item" href="${ERP_URL}" target="_blank" rel="noopener"><span class="ico">▦</span>原版 ERP</a>
    <a class="nav-item" id="__navCmd" href="javascript:void 0" title="快捷键 Ctrl/⌘ + K"><span class="ico">⌘</span>命令面板</a>
    <div class="nav-foot">
      <div><span class="nav-dot" id="__navDot"></span><span id="__navStat">连接检查中…</span></div>
      <div>说一句话 · 变成一张 <b>ERP 单据</b></div>
    </div>`;
  document.body.prepend(nav);

  const main = document.createElement('div');
  main.className = 'shell-main';
  while (document.body.children.length > 1) main.appendChild(document.body.children[1]);
  document.body.appendChild(main);

  // 手机底部 Tab
  const tab = document.createElement('div');
  tab.className = 'tabbar';
  tab.innerHTML = NAV_ITEMS.slice(0, 5).map(([h, t, i]) =>
    `<a href="${h}" class="${h === active ? 'on' : ''}"><span class="ico">${i}</span>${t}</a>`).join('');
  document.body.appendChild(tab);

  const navCmd = $('#__navCmd');
  if (navCmd) navCmd.onclick = () => commandPalette();

  // ERP 连接状态（静默）
  api('/api/master').then(() => {
    const d = $('#__navDot'), s = $('#__navStat');
    if (d) d.className = 'nav-dot ok';
    if (s) s.textContent = 'ERP 已连接';
  }).catch(() => {
    const d = $('#__navDot'), s = $('#__navStat');
    if (d) d.className = 'nav-dot bad';
    if (s) s.textContent = 'ERP 未连接';
  });

  return main;
}

// 页面头（各页可选调用）
function pageHead(crumb, title, sub) {
  return `<div class="page-head">
    <div class="crumb">${esc(crumb)}</div>
    <h1>${esc(title)}</h1>
    ${sub ? `<div class="sub">${esc(sub)}</div>` : ''}
  </div>`;
}
