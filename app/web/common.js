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
        const h = {};
        const p2 = localStorage.getItem('t2s_pass');
        if (p2) h['X-T2S-Pass'] = p2;
        const r = await fetch('/api/upload', { method: 'POST', body: fd, headers: h });
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

  const candChips = (cands, pick) => (cands || []).length
    ? `<div style="margin-top:6px"><span class="hint">是指：</span>${cands.map(c =>
        `<span class="tag tag-blue" style="cursor:pointer" data-pick="${esc(pick)}" data-cand="${esc(c)}">${esc(c)}</span>`).join(' ')}</div>`
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

  function itemTag(i) {
    const it = (st.draft.items || [])[i];
    if (!it || !(it.name || '').trim()) return '<span class="muted">未填</span>';
    return findExact(MD.materials, it.name)
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
          <div class="pv-sub">${itemTag(i)}${pv.match_kind === 'fuzzy' && pv.matched_name ? ` <span class="tag tag-fuzzy">匹配→${esc(pv.matched_name)}</span>` : ''}${candChips(pv.candidates, 'item:' + i)}</div>
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
          <div class="hint" style="margin-top:8px">不用先去「基础资料」里新增，确认后一并创建。</div>
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

        <div class="tbl-wrap" style="margin-top:12px">
          <table class="xtable">
            <thead><tr><th>商品</th><th class="num" style="width:86px">数量</th><th style="width:76px">单位</th><th class="num" style="width:110px">单价</th><th class="num" style="width:100px">金额</th><th style="width:40px"></th></tr></thead>
            <tbody id="__pvBody">${rows || '<tr><td colspan="6" class="muted center">没有明细</td></tr>'}</tbody>
          </table>
        </div>
        <button class="btn btn-sm btn-ghost" id="__addRow" style="margin-top:6px">＋ 加一行</button>

        <div class="total-row">合计：<span class="amount" id="__pvTotal">￥${money(curTotal())}</span></div>

        <div class="pv-dirty hidden" id="__dirtyBar">✏ 内容已修改：先「刷新预览」重新校验档案匹配 / 库存 / 合计，再确认写入。</div>

        ${(st.attach && st.attach.length) ? `
        <div class="field" style="margin-top:14px">
          <label>附件（${st.attach.length} 份，已上传到 ERP）</label>
          <div class="attach-list" style="margin-top:6px">
            ${st.attach.map(x => `<div class="attach-item"><span class="ok">✓</span><span class="nm">${esc(x.name)}</span></div>`).join('')}
          </div>
        </div>` : ''}

        <div class="row-btns" style="margin-top:18px">
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
      autocomplete(inp, () => MD.materials,
        it2 => {
          const i = +inp.dataset.i;
          if (it2.unit && st.draft.items[i] && !st.draft.items[i].unit) {
            st.draft.items[i].unit = it2.unit;
            const u = $(`.pv-unit[data-i="${i}"]`);
            if (u) u.value = it2.unit;
          }
        },
        () => {
          const i = +inp.dataset.i;
          if (st.draft.items[i]) st.draft.items[i].name = inp.value.trim();
          syncRow(i); setDirty(); refreshOkState();
        });
    });
  }

  function bind() {
    const mask = $('#__mask');
    const close = () => {
      if (detachKey) detachKey();
      detachKey = null;
      mask.remove();
      if (!st.done && extra.onCancel) extra.onCancel(st.draft);
    };

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
          if (st.draft.items && st.draft.items[i]) st.draft.items[i].name = cand;
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
