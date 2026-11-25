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

// ---------- 就地输入/确认弹框（替代 prompt / confirm） ----------
/** WebView/部分内嵌浏览器不支持 prompt() / confirm()，导致公网部署下直接卡死。
    统一用自绘 modal：当传入 input / password / value / placeholder 时展示输入框，
    否则为纯确认弹框（不出现空文本框）。确定返回 string，取消返回 null。 */
function askInline(opt) {
  const o = Object.assign({ title: '请输入', hint: '', value: '', placeholder: '', input: false, okText: '确定', cancelText: '取消', password: false, maxlen: 60 }, opt || {});
  const needInput = !!(o.input || o.password || o.value || o.placeholder);
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
        <button type="button" class="modal-x" aria-label="关闭">×</button>
        <h3>${esc(o.title)}</h3>
        ${o.hint ? `<div class="hint" style="margin-top:6px">${esc(o.hint)}</div>` : ''}
        ${needInput ? `<div class="pv-form">
          <div class="pv-line">
            <input id="__askInlineInput" type="${o.password ? 'password' : 'text'}" maxlength="${o.maxlen}"
              value="${esc(o.value)}" placeholder="${esc(o.placeholder)}" autocomplete="off" spellcheck="false"
              style="flex:1;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:#fff;font-size:15px">
          </div>
        </div>` : ''}
        <div class="row-btns mt-4">
          <button type="button" class="btn btn-ghost" id="__askInlineCancel">${esc(o.cancelText)}</button>
          <span class="hint flex-1 pv-kbd-hint">${needInput ? 'Enter 确认 · Esc 取消' : 'Esc 取消'}</span>
          <button type="button" class="btn btn-primary" id="__askInlineOk">${esc(o.okText)}</button>
        </div>
      </div>`;
    document.body.appendChild(mask);
    const input = mask.querySelector('#__askInlineInput');
    const done = v => { if (mask._done) return; mask._done = true; if (untrap) untrap(); mask.remove(); resolve(v); };
    const ok = () => done(input ? input.value : '');
    mask.querySelector('#__askInlineOk').onclick = ok;
    mask.querySelector('#__askInlineCancel').onclick = () => done(null);
    mask.querySelector('.modal-x').onclick = () => done(null);
    mask.addEventListener('mousedown', e => { if (e.target === mask) done(null); });
    if (input) {
      input.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
          if (e.isComposing || e.keyCode === 229) return;
          e.preventDefault(); ok();
        } else if (e.key === 'Escape') { e.preventDefault(); done(null); }
        e.stopPropagation();
      });
    }
    const untrap = trapFocus(mask.querySelector('.modal'), () => done(null));
    if (input) { input.focus(); input.select(); }
    else { mask.querySelector('#__askInlineCancel').focus(); }
  });
}
async function askPass() {
  const p = await askInline({
    title: '管理员口令',
    hint: '静态口令（T2S_DEMO_PASS）= 管理员后备通道，ERP 账号登录也可用。',
    okText: '验证并继续', password: true,
  });
  return p;
}

// ---------- 登录（ERP 账号联动） ----------
/** 登录弹窗：默认 ERP 账号密码；可切换「管理员口令」。成功返回凭证（会话令牌/口令）。 */
function loginModal(prefPass) {
  return new Promise(resolve => {
    const old = document.getElementById('__t2sLogin');
    if (old) old.remove();
    const mask = document.createElement('div');
    mask.id = '__t2sLogin';
    mask.className = 'mask show';
    mask.setAttribute('role', 'dialog');
    mask.setAttribute('aria-modal', 'true');
    mask.innerHTML = `
      <div class="modal" style="max-width:420px">
        <button class="modal-x" aria-label="关闭">×</button>
        <h3>登录 Talk2Stock</h3>
        <div class="hint" style="margin-top:6px" id="__lgHint"></div>
        <div class="pv-form" id="__lgErp">
          <div class="pv-line"><input id="__lgUser" type="text" placeholder="ERP 账号（如 test1）"
            autocomplete="username" spellcheck="false"></div>
          <div class="pv-line"><input id="__lgPwd" type="password" placeholder="ERP 密码"
            autocomplete="current-password"></div>
        </div>
        <div class="pv-form" id="__lgPassRow" hidden>
          <div class="pv-line"><input id="__lgPass" type="password" placeholder="管理员口令"
            autocomplete="off"></div>
        </div>
        <div class="row-btns mt-4">
          <button class="btn btn-ghost" id="__lgMode">用管理员口令登录</button>
          <span class="hint flex-1">Enter 确认</span>
          <button class="btn btn-primary" id="__lgOk">登录</button>
        </div>
      </div>`;
    document.body.appendChild(mask);
    let mode = prefPass ? 'pass' : 'erp';
    const $id = s => mask.querySelector(s);
    const user = $id('#__lgUser'), pwd = $id('#__lgPwd'), pass = $id('#__lgPass');
    const paint = () => {
      $id('#__lgErp').hidden = mode !== 'erp';
      $id('#__lgPassRow').hidden = mode !== 'pass';
      $id('#__lgHint').textContent = mode === 'erp'
        ? '使用你的 ERP 账号登录（账号体系与 ERP 联动）。'
        : '静态口令 = 管理员后备通道。';
      $id('#__lgMode').textContent = mode === 'erp' ? '用管理员口令登录' : '用 ERP 账号登录';
      (mode === 'erp' ? user : pass).focus();
    };
    const done = v => { if (mask._done) return; mask._done = true; if (untrap) untrap(); mask.remove(); resolve(v); };
    const submit = async () => {
      if (mode === 'pass') { done({ mode: 'pass', pass: pass.value }); return; }
      if (!user.value.trim() || !pwd.value) { toast('请输入账号和密码', 'warn'); return; }
      const ok = $id('#__lgOk');
      ok.disabled = true; ok.classList.add('loading'); ok.textContent = '登录中…';
      try {
        const r = await fetch('/api/auth/login', { method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ login_name: user.value.trim(), password: pwd.value }) });
        const j = await r.json();
        if (j.ok && j.token) {
          window.__t2sMe = { role: j.role, login: j.login, username: j.username };
          toast('欢迎，' + (j.username || j.login), 'ok');
          done({ mode: 'token', token: j.token });
        } else { toast(j.message || '登录失败', 'bad'); }
      } catch (e) { toast('登录请求失败：' + e.message, 'bad'); }
      finally { ok.disabled = false; ok.classList.remove('loading'); ok.textContent = '登录'; }
    };
    $id('#__lgOk').onclick = submit;
    $id('#__lgMode').onclick = () => { mode = mode === 'erp' ? 'pass' : 'erp'; paint(); };
    $id('.modal-x').onclick = () => done(null);
    mask.addEventListener('mousedown', e => { if (e.target === mask) done(null); });
    [user, pwd, pass].forEach(inp => inp.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); submit(); }
      else if (e.key === 'Escape') { e.preventDefault(); done(null); }
      e.stopPropagation();
    }));
    const untrap = trapFocus(mask.querySelector('.modal'), () => done(null));
    paint();
  });
}

async function askLogin() {
  while (true) {
    const cred = await loginModal(!!localStorage.getItem('t2s_tried_erp'));
    if (!cred) return null;
    if (cred.mode === 'pass') return cred.pass;
    if (cred.mode === 'token') {
      localStorage.setItem('t2s_tried_erp', '1');
      return cred.token;
    }
  }
}

// ---------- 账号面板（手机端「账号」Tab 入口；桌面侧边另有常驻徽章） ----------
async function accountSheet() {
  let me = null;
  try { me = await api('/api/auth/me'); } catch (e) { /* 未登录时 api 已弹登录框 */ }
  const old = document.getElementById('__t2sAcct');
  if (old) old.remove();
  const mask = document.createElement('div');
  mask.id = '__t2sAcct';
  mask.className = 'mask show';
  mask.setAttribute('role', 'dialog');
  mask.setAttribute('aria-modal', 'true');
  const admin = !!(me && me.role === 'admin');
  mask.innerHTML = `
    <div class="modal" style="max-width:400px">
      <button class="modal-x" aria-label="关闭">×</button>
      <h3>账号</h3>
      ${me ? (me.local
        ? '<div class="hint" style="margin-top:10px">本地模式（未开启鉴权）</div>'
        : `<div class="acct-row">
             <div class="acct-name">${esc(me.username || me.login || '')}</div>
             <span class="me-badge ${admin ? 'admin' : 'member'}">${admin ? '管理员' : '成员'}</span>
           </div>
           <div class="hint">登录名 ${esc(me.login || '')} · ERP 账号</div>`)
        : '<div class="hint" style="margin-top:10px">未登录</div>'}
      <div class="row-btns mt-4">
        ${me && !me.local ? '<button class="btn btn-ghost" id="__acctOut">退出登录</button>' : ''}
        <span class="flex-1"></span>
        <button class="btn btn-primary" id="__acctClose">关闭</button>
      </div>
    </div>`;
  document.body.appendChild(mask);
  const done = () => {
    if (mask._done) return;
    mask._done = true;
    if (untrap) untrap();
    mask.remove();
  };
  const untrap = trapFocus(mask.querySelector('.modal'), done);
  mask.querySelector('.modal-x').onclick = done;
  mask.querySelector('#__acctClose').onclick = done;
  mask.addEventListener('mousedown', e => { if (e.target === mask) done(); });
  const out = mask.querySelector('#__acctOut');
  if (out) out.onclick = () => {
    done();
    endSession();
  };
}

// 登出：先让服务端吊销该登录名的令牌代数（旧令牌立即失效），
// 网络失败也照常清本地，避免卡在「退不出去」。
function endSession() {
  const finish = () => {
    localStorage.removeItem('t2s_pass');
    localStorage.removeItem('t2s_tried_erp');
    window.__t2sMe = null;
    location.reload();
  };
  api('/api/auth/logout', { method: 'POST', body: {} }).catch(() => {}).then(finish);
}

// ---------- 当前登录者徽章 ----------
function renderUserChip() {
  api('/api/auth/me').then(me => {
    const el = $('#__navUser');
    if (!el) return;
    if (me.local) { el.innerHTML = '<span class="me-badge admin">本地模式</span>'; return; }
    el.innerHTML = `<span class="me-name">${esc(me.username || me.login || '')}</span>` +
      `<span class="me-badge ${me.role === 'admin' ? 'admin' : 'member'}">${me.role === 'admin' ? '管理员' : '成员'}</span>` +
      `<a id="__navLogout" href="javascript:void 0" title="退出登录">退出</a>`;
    const lo = $('#__navLogout');
    if (lo) lo.onclick = () => endSession();
  }).catch(() => { /* 未登录时不展示，等 401 触发登录弹窗 */ });
}

// ---------- 网络 ----------
async function api(path, opts) {
  const o = Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts || {});
  if (!o.headers) o.headers = {};
  const cred = localStorage.getItem('t2s_pass');
  if (cred) o.headers['X-T2S-Pass'] = cred;
  if (o.body && typeof o.body !== 'string') o.body = JSON.stringify(o.body);
  let r;
  try {
    r = await fetch(path, o);
  } catch (e) {
    throw new Error('连接不上本地服务，请确认 server.py 还在运行');
  }
  if (r.status === 401) {
    const c = await askLogin();
    if (c) { localStorage.setItem('t2s_pass', c); renderUserChip(); return api(path, opts); }
    throw new Error('需要登录');
  }
  const txt = await r.text();
  let j;
  try { j = JSON.parse(txt); }
  catch (e) { throw new Error('服务返回异常：' + txt.slice(0, 120)); }
  if (r.status === 403 && j && j.need_admin) {
    throw new Error('该页面仅管理员可用');
  }
  return j;
}

// 操作人标识：已登录时服务端以登录名为准（这里不再弹窗）；
// 本地未登录模式下才手填一次（只存本地，用于基线实验归因，写进 audit.jsonl）
async function ensureOperator() {
  let op = localStorage.getItem('t2s_operator');
  if (!op && !localStorage.getItem('t2s_pass')) {
    const v = await askInline({
      title: '你的名字或编号',
      hint: '只存本机，用于实验记录归因（写入审计）。可留空。登录后自动使用登录名。',
      input: true, placeholder: '例如 张三 或 001', okText: '保存', maxlen: 40,
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
  // initial：多单预览层重渲染卡片时，把已传附件原样带回（paths 存在调用方状态里）
  const initial = Array.isArray(opts.initial) ? opts.initial : [];
  const paths = initial.map(x => x.path).filter(Boolean);
  const names = initial.map(x => x.name || (x.path || '').split('/').pop());

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

// ---------- 预览确认公共辅助（单单 / 批量统一复用） ----------
const pvMatNorm = s => String(s || '').replace(/[()（）]/g, '').replace(/[×✕ｘ]/g, 'x')
  .trim().toLowerCase().replace(/\s/g, '');

function pvDepotTag(depotVal) {
  const v = (depotVal || '').trim();
  if (!v) return '<span class="tag tag-muted">默认仓</span>';
  return findExact(MD.depots || [], v)
    ? '<span class="tag tag-ok">已有</span>'
    : '<span class="tag tag-new">将新建</span>';
}

function pvItemTag(it) {
  if (!it || !(it.name || '').trim()) return '<span class="muted">未填</span>';
  const n = pvMatNorm(it.name);
  const hit = (MD.materials || []).find(x =>
    pvMatNorm(x.name) === n || pvMatNorm(x.name + (x.model || '') + (x.standard || '')) === n);
  return hit ? '<span class="tag tag-ok">已有</span>' : '<span class="tag tag-new">将新建</span>';
}

// 候选 chip：商品候选是 {id,name,model,standard,unit}（同名档案靠型号区分），
// 往来单位候选仍是纯名字字符串——两种都渲染；商品 chip 带 data-cand-mid，
// 点选后 material_id 直达档案，不再靠名字匹配
function pvCandChips(cands, pick) {
  if (!(cands || []).length) return '';
  return `<div class="mt-1"><span class="hint">是指：</span>${cands.map(c => {
    const o = (c && typeof c === 'object');
    const label = o ? [c.name, c.model, c.standard].filter(Boolean).join(' · ') : c;
    const mid = o && c.id ? ` data-cand-mid="${c.id}"` : '';
    return `<span class="tag tag-blue" style="cursor:pointer" data-pick="${esc(pick)}" data-cand="${esc(o ? c.name : c)}"${mid}>${esc(label)}</span>`;
  }).join(' ')}</div>`;
}

function pvRenderRows(items, previewItems, pickPrefix, priceTitle) {
  if (!(items || []).length) {
    return '<tr class="pv-empty-row"><td colspan="6" class="muted center">没有明细</td></tr>';
  }
  return items.map((it, i) => {
    const pv = (previewItems || [])[i] || {};
    const matchTag = (pv.match_kind === 'fuzzy' || pv.match_kind === 'picked') && pv.matched_name
      ? ` <span class="tag ${pv.match_kind === 'picked' ? 'tag-ok' : 'tag-fuzzy'}">匹配→${esc(pv.matched_name)}${pv.matched_model ? ' ' + esc(pv.matched_model) : ''}</span>`
      : '';
    return `<tr>
      <td class="pv-c-name">
        <div class="pv-name-row"><div class="ac-wrap" style="flex:1"><input class="pv-name" data-i="${i}" value="${esc(it.name)}" placeholder="商品名称" autocomplete="off"></div></div>
        <div class="pv-sub">${pvItemTag(it)}${matchTag}${pvCandChips(pv.candidates, pickPrefix + i)}</div>
      </td>
      <td class="pv-c-qty" data-label="数量"><input class="pv-qty num" data-i="${i}" type="number" inputmode="decimal" step="any" value="${it.qty ?? ''}" placeholder="0"></td>
      <td class="pv-c-unit" data-label="单位"><input class="pv-unit" data-i="${i}" value="${esc(it.unit || '')}" placeholder="个"></td>
      <td class="pv-c-price" data-label="单价">
        <div class="pv-price-cell">
          <input class="pv-price num${pv.price_missing ? ' price-zero' : ''}" data-i="${i}" type="number" inputmode="decimal" step="any" value="${it.price ?? ''}" placeholder="0.00">
          ${pv.price_missing ? `<span class="tag tag-new" title="${esc(priceTitle)}">单价?</span>` : ''}
        </div>
      </td>
      <td class="pv-c-amt num" data-label="小计"><b class="pv-amt" data-i="${i}">${money(num(it.qty) * num(it.price))}</b></td>
      <td class="pv-c-del"><button type="button" class="xdel" data-del="${i}" title="删除该行" aria-label="删除第 ${i + 1} 行">×</button></td>
    </tr>`;
  }).join('');
}

function pvTableHtml(rowsHtml, tbodyAttrs) {
  return `
    <div class="pv-items-wrap tbl-wrap" tabindex="0" role="region" aria-label="单据商品明细">
      <table class="xtable">
        <thead><tr>
          <th class="pv-th-name">商品</th>
          <th class="pv-th-qty num">数量</th>
          <th class="pv-th-unit">单位</th>
          <th class="pv-th-price num">单价</th>
          <th class="pv-th-amt num">金额</th>
          <th class="pv-th-del" aria-label="操作"></th>
        </tr></thead>
        <tbody ${tbodyAttrs}>${rowsHtml}</tbody>
      </table>
    </div>`;
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
    suggestions: (((extra.check || {}).patches_suggested) || [])
      .filter(s => (s.bill_index || 0) === 0).map(s => Object.assign({}, s)),
    dirty: false,
    done: false,               // 确认流程已走完，关闭时不再触发 onCancel
  };
  let detachKey = null;        // 当前弹层的键盘监听卸载器（重渲染时先卸旧的，防残留）
  let detachTrap = null;       // 焦点陷阱卸载器

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

  const itemTag = i => pvItemTag((st.draft.items || [])[i]);

  function partyTag() {
    const d = st.draft;
    if (!(d.party || '').trim()) return '';
    return findExact(partyPool(d.biz), d.party)
      ? '<span class="tag tag-ok">已有</span>'
      : '<span class="tag tag-new">将新建</span>';
  }

  const payNoteText = (payAmt, total) => {
    if (payAmt === null || payAmt === undefined || payAmt === '') return '全款结清';
    const pa = num(payAmt), debt = Math.round((total - pa) * 100) / 100;
    return debt > 0 ? `欠款 ￥${money(debt)}` : '全款结清';
  };

  function render() {
    if (detachKey) { detachKey(); detachKey = null; }
    if (detachTrap) { detachTrap(); detachTrap = null; }
    const prevMask = $('#__mask');
    if (prevMask) prevMask.remove();

    const p = st.preview, d = st.draft;
    const errs = p.errors || [];
    const warns = p.warnings || [];
    const toCreate = p.to_create || [];
    const ai = p.ai || {};
    const aiFlag = (ai.confidence && ai.confidence !== 'high') || (ai.note || '').trim()
      || (ai.problems || []).length;

    const rows = pvRenderRows(d.items || [], p.items || [], 'item:', '原话里可能没有说价格');
    const depotOpts = (MD.depots || []).map(x => `<option value="${esc(x.name)}">`).join('');
    const effTime = (d.oper_time || p.oper_time || '').trim();
    const dtVal = effTime.replace(' ', 'T').slice(0, 16);

    const html = `
    <div class="mask show" id="__mask" role="dialog" aria-modal="true" aria-labelledby="__pvTitle">
      <div class="modal pv-modal">
        <div class="pv-modal-hd">
          <h2 id="__pvTitle">确认单据</h2>
          <select id="__biz" class="biz-sel" aria-label="业务类型">${Object.entries(BIZ).map(([k, v]) =>
            `<option value="${k}"${k === d.biz ? ' selected' : ''}>${v.label}</option>`).join('')}</select>
          <button type="button" class="modal-x m-close" id="__modalX" title="关闭（Esc）" aria-label="关闭">×</button>
        </div>

        <div class="pv-modal-body">
          <div id="__pErrs">${errs.length ? `<div class="alert alert-error"><b>无法提交：</b><br>${errs.map(esc).join('<br>')}</div>` : ''}</div>

          ${aiFlag ? `
          <div class="alert ${ai.confidence === 'low' ? 'alert-warn' : 'alert-info'}">
            <b>AI 提示（置信度：${esc(ai.confidence || '—')}）：</b>${esc(ai.note || '')}
            ${(ai.problems || []).length ? '<br>解析问题：' + ai.problems.map(esc).join('；') : ''}
          </div>` : ''}

          ${(p.duplicate && p.duplicate.hit) ? `
          <div class="alert alert-warn">
            <b>⚠ 疑似重复录入：</b><br>
            ${(p.duplicate.matches || []).map(m => `近 ${p.duplicate.window_minutes} 分钟已有 <b>${esc(m.number)}</b>（${esc(m.time)}，${esc(m.party)}，${(m.hits || []).map(h => esc(h.name) + '×' + h.qty).join('、')}）`).join('<br>')}
            <br>若这是同一笔业务，请先核对上面的单号，避免重复入库/出库。
          </div>` : ''}

          ${toCreate.length ? `
          <div class="alert alert-warn">
            <b>⚠ 以下资料系统里还没有，写入时会自动帮你建好：</b>
            <ul style="margin:8px 0 0 20px;padding:0">${toCreate.map(x => `<li>${esc(x)}</li>`).join('')}</ul>
            <div class="hint mt-2">不用先去「基础资料」里新增，确认后一并创建。</div>
          </div>` : ''}

          ${warns.length ? `<div class="alert alert-info"><b>提醒：</b><br>${warns.map(esc).join('<br>')}</div>` : ''}

          ${(st.suggestions && st.suggestions.length) ? `
          <div class="sugg-bar">
            <span class="sugg-label">💡 校验建议（来自历史证据，点击采纳）：</span>
            <div class="sugg-list">${st.suggestions.map((sg, si) => {
              const label = sg.field === 'depot'
                ? `仓库 → ${sg.value}`
                : `第${(sg.item_index || 0) + 1}行单价 → ￥${sg.value}`;
              return `<button type="button" class="sugg-chip" data-pv-sugg="${si}" title="${esc(sg.reason || '')}">✓ ${esc(label)}${sg.reason ? ' · ' + esc(sg.reason) : ''}</button>`;
            }).join('')}</div>
          </div>` : ''}

          <div class="pv-meta-grid">
            <div class="pv-field pv-f-party">
              <div class="pv-field-hd">
                <label id="__partyLbl" for="__party">${esc((p.party && p.party.type) || (BIZ[d.biz] && BIZ[d.biz].party) || '往来单位')}</label>
                <span id="__partyTag">${partyTag()}</span>
              </div>
              <div class="ac-wrap"><input id="__party" value="${esc(d.party || '')}" placeholder="名称，没有的会自动新建" autocomplete="off"></div>
              <div id="__partyCand">${d.party && p.party && !p.party.matched ? pvCandChips(p.party.candidates, 'party') : ''}</div>
            </div>
            <div class="pv-field pv-f-depot">
              <div class="pv-field-hd">
                <label for="__depot">仓库</label>
                <span id="__depotTag">${pvDepotTag(d.depot)}</span>
              </div>
              <input id="__depot" list="__depotList" value="${esc(d.depot || '')}" placeholder="留空用默认仓（${esc((p.depot && p.depot.name) || '默认仓')}）">
              <datalist id="__depotList">${depotOpts}</datalist>
            </div>
            <div class="pv-field pv-f-time">
              <div class="pv-field-hd">
                <label for="__otime">日期</label>
                ${effTime ? '' : '<span class="pv-field-note">留空用当前时间</span>'}
              </div>
              <input id="__otime" type="datetime-local" value="${esc(dtVal)}">
            </div>
          </div>
          <div class="pv-meta-grid-2">
            <div class="pv-field">
              <div class="pv-field-hd">
                <label for="__payAmt">${d.biz && d.biz.includes('sale') ? '实收金额' : '实付金额'}</label>
                <span class="pv-field-note" id="__payNote">${payNoteText(d.pay_amount, curTotal())}</span>
              </div>
              <input id="__payAmt" type="number" step="any" min="0" value="${d.pay_amount ?? ''}" placeholder="留空按全款结清">
            </div>
            <div class="pv-field">
              <div class="pv-field-hd"><label for="__remark">备注</label></div>
              <input id="__remark" type="text" value="${esc(d.remark || '')}" placeholder="选填备注">
            </div>
          </div>

          ${pvTableHtml(rows, 'id="__pvBody"')}

          <div class="pv-items-foot">
            <button type="button" class="btn btn-sm btn-ghost" id="__addRow">＋ 加一行</button>
            <div class="total-row">合计：<span class="amount" id="__pvTotal">￥${money(curTotal())}</span></div>
          </div>

          <div class="pv-dirty hidden" id="__dirtyBar">✏ 内容已修改：先「刷新预览」重新校验档案匹配 / 库存 / 合计，再确认写入。</div>

          ${(st.attach && st.attach.length) ? `
          <div class="field mt-3">
            <label>附件（${st.attach.length} 份，已上传到 ERP）</label>
            <div class="attach-list mt-1">
              ${st.attach.map(x => `<div class="attach-item"><span class="ok">✓</span><span class="nm">${esc(x.name)}</span></div>`).join('')}
            </div>
          </div>` : ''}
        </div>

        <div class="pv-modal-ft">
          <button type="button" class="btn btn-ghost" id="__cancel">再改改</button>
          <span class="hint pv-kbd-hint" style="margin:0;flex:1">Esc 关闭 · ⌘/Ctrl+Enter 确认 · 表格里可直接改数</span>
          <button type="button" class="btn btn-primary btn-lg" id="__ok" ${errs.length ? 'disabled' : ''}>确认写入 ERP</button>
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

    // 校验建议一键采纳（采纳后写回草稿并标记需刷新预览重签令牌）
    $$('[data-pv-sugg]', mask).forEach(btn => {
      btn.onclick = () => {
        const si = +btn.dataset.pvSugg;
        const sg = st.suggestions[si];
        if (!sg) return;
        if (sg.field === 'depot') st.draft.depot = String(sg.value || '');
        else if (sg.field === 'price' && st.draft.items && st.draft.items[sg.item_index || 0]) {
          st.draft.items[sg.item_index || 0].price = Number(sg.value);
        }
        st.suggestions.splice(si, 1);
        st.dirty = true;
        render();
        setDirty();
        refreshOkState();
        toast('已采纳建议，请点「刷新预览 ↻」后确认写入', 'ok');
      };
    });

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
      setDirty();
      refreshOkState();
      toast('已删除一行，刷新预览后生效', 'warn');
    });

    $('#__addRow').onclick = () => {
      (st.draft.items = st.draft.items || []).push({ name: '', qty: 0, unit: '个', price: 0 });
      st.dirty = true;
      render();
      setDirty();
      refreshOkState();
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
      const dt = $('#__depotTag');
      if (dt) dt.innerHTML = pvDepotTag(st.draft.depot);
      setDirty();
    });
    $('#__otime').addEventListener('change', () => {
      const v = $('#__otime').value || '';
      st.draft.oper_time = v ? (v.replace('T', ' ') + (v.length === 16 ? ':00' : '')) : '';
      setDirty();
    });
    const payEl = $('#__payAmt');
    if (payEl) payEl.addEventListener('input', () => {
      const raw = payEl.value.trim();
      st.draft.pay_amount = raw === '' ? null : num(raw);
      const pn = $('#__payNote');
      if (pn) pn.textContent = payNoteText(st.draft.pay_amount, curTotal());
      setDirty(); refreshOkState();
    });
    const remEl = $('#__remark');
    if (remEl) remEl.addEventListener('input', () => {
      st.draft.remark = remEl.value.trim();
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
    let downOnMask = false;
    mask.addEventListener('mousedown', e => { downOnMask = e.target === mask; });
    mask.addEventListener('mouseup', e => { if (downOnMask && e.target === mask) close(); downOnMask = false; });

    // 键盘：⌘/Ctrl+Enter = 点主按钮（改过数=刷新预览，没改=写入）；Esc 由 trapFocus 统一处理
    const onKey = e => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); $('#__ok').click(); }
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

// ---------- 图片录入：客户端压缩（视觉 token 按图片 tile 计费，先省再传） ----------
/** 图片文件 → 压缩后的 data URL（最长边 maxSide，JPEG q）。压缩失败回退原文件，
 *  由调用方自行决定直传（服务端有 8MB 上限）。 */
function compressImage(file, maxSide = 1600, q = 0.85) {
  return new Promise(resolve => {
    try {
      const img = new Image();
      const url = URL.createObjectURL(file);
      const done = v => { try { URL.revokeObjectURL(url); } catch (e) {} resolve(v); };
      img.onload = () => {
        try {
          const scale = Math.min(1, maxSide / Math.max(img.width, img.height));
          const w = Math.max(1, Math.round(img.width * scale));
          const h = Math.max(1, Math.round(img.height * scale));
          const cv = document.createElement('canvas');
          cv.width = w; cv.height = h;
          cv.getContext('2d').drawImage(img, 0, 0, w, h);
          const du = cv.toDataURL('image/jpeg', q);
          done(du && du.length > 30 ? { dataUrl: du, file } : { dataUrl: null, file });
        } catch (e) { done({ dataUrl: null, file }); }   // HEIC 等解不动的格式直传原文件
      };
      img.onerror = () => done({ dataUrl: null, file });
      img.src = url;
    } catch (e) { resolve({ dataUrl: null, file }); }
  });
}

function dataURLtoBlob(dataUrl) {
  const [head, b64] = String(dataUrl).split(',');
  const mime = (head.match(/data:([^;]+)/) || [, 'image/jpeg'])[1];
  const bin = atob(b64);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  return new Blob([arr], { type: mime });
}

// ---------- 批量写入（分块顺序提交，进度可回报） ----------
/** items: [{draft, confirm_token}]。分块调用 /api/commit/batch：服务端逐单顺序写
 *  （主数据补建必须对下一张可见），前端分块拿到中间进度；onResult(gi, res) 的 gi
 *  是 items 全局下标。 */
async function commitBatch(items, opts) {
  const o = Object.assign({ source: '', rawText: '', chunkSize: 5, onResult: null, onProgress: null }, opts || {});
  const total = items.length;
  let done = 0, okCount = 0;
  const results = new Array(total);
  for (let s = 0; s < total; s += o.chunkSize) {
    const chunk = items.slice(s, s + o.chunkSize);
    const r = await api('/api/commit/batch', { method: 'POST', body: {
      items: chunk.map(x => ({ draft: x.draft, confirm_token: x.confirm_token })),
      source: o.source, raw_text: o.rawText, operator: await ensureOperator(),
    } });
    if (!r.results) throw new Error(r.message || '批量写入失败');
    (r.results || []).forEach(res => {
      const gi = s + res.index;
      results[gi] = res;
      done++;
      if (res.ok) okCount++;
      if (o.onResult) o.onResult(gi, res);
    });
    if (o.onProgress) o.onProgress(done, total, okCount);
  }
  return { results, okCount, total };
}

// ---------- 多单批量预览层（AI 多单解析 / 表格导入共用） ----------
/** bills: [{draft, preview, confirm_token, ai, attach?}]；extra: {source, rawText, onClose(bills)}。
 *  每单一个确认令牌：勾选整批 → /api/commit/batch 逐单过闸门，篡改哪单拒哪单；
 *  改过的单自动先 /api/resolve 换新令牌再写入。逐单可弃可改，互不阻塞。 */
function showMultiPreview(bills, extra) {
  const old = document.getElementById('__mmask');
  if (old) old.remove();
  extra = extra || {};

  // 已写入单据可以一键生成反向草稿（退货/冲销）：原单不动，新增反向单走同一闸门
  const REVERSIBLE = {
    purchase_in: '采购退货单', sale_out: '销售退货单',
    purchase_return: '采购入库单', sale_return: '销售出库单',
  };

  const suggAll = (extra.check && extra.check.patches_suggested) || [];
  const st = {
    source: extra.source || 'ai',
    rawText: extra.rawText || '',
    done: false,
    bills: (bills || []).map((b, i) => ({
      idx: i,
      draft: JSON.parse(JSON.stringify(b.draft || {})),
      preview: JSON.parse(JSON.stringify(b.preview || {})),
      confirm_token: b.confirm_token || '',
      ai: b.ai || (b.preview && b.preview.ai) || {},
      attach: (b.attach || []).slice(),
      suggestions: (b.suggestions || suggAll.filter(p => Number(p.bill_index || 0) === i)).slice(),
      collapsed: i > 0,
      dirty: false,
      state: 'open',        // open | committed | failed
      res: null,
    })),
  };

  const statusOf = b => {
    if (b.state === 'committed') return 'committed';
    if (b.state === 'failed') return 'failed';
    if ((b.preview.errors || []).length) return 'blocked';
    if ((b.preview.warnings || []).length) return 'warned';
    return 'ready';
  };
  const STATUS_ZH = { ready: '可写入', warned: '需注意', blocked: '无法提交', committed: '已写入', failed: '写入失败' };
  const billTotal = b => (b.draft.items || []).reduce((t, it) => t + num(it.qty) * num(it.price), 0);

  // ---------- 渲染 ----------
  let detachKey = null;

  function cardBody(b) {
    const p = b.preview, d = b.draft;
    const stat = statusOf(b);
    const errs = p.errors || [], warns = p.warnings || [], toCreate = p.to_create || [];
    const ai = b.ai || {};
    const aiFlag = (ai.confidence && ai.confidence !== 'high') || (ai.note || '').trim() || (ai.problems || []).length;
    const rows = pvRenderRows(d.items || [], p.items || [], `${b.idx}:item:`, '原内容里可能没有价格');
    const effTime = (d.oper_time || p.oper_time || '').trim();
    const dtVal = effTime.replace(' ', 'T').slice(0, 16);
    const partyTag = () => {
      if (!(d.party || '').trim()) return '';
      return findExact(partyPool(d.biz), d.party)
        ? '<span class="tag tag-ok">已有</span>' : '<span class="tag tag-new">将新建</span>';
    };
    const suggs = b.suggestions || [];
    const suggHtml = suggs.length ? `
      <div class="sugg-bar">
        <b>💡 校验参考建议（未自动改草稿，点选后套用）：</b>
        <div class="sugg-list">${suggs.map((s, si) => {
          const fl = s.field === 'price' ? '单价' : (s.field === 'depot' ? '仓库' : s.field);
          const val = s.field === 'price' ? '￥' + s.value : s.value;
          return `<button type="button" class="sugg-chip" data-applysugg="${b.idx}:${si}">${esc(fl)}建议 ${esc(String(val))}${s.reason ? ' · ' + esc(s.reason) : ''} <b>一键采纳</b></button>`;
        }).join('')}</div>
      </div>` : '';
    const commitDisabled = b.state === 'committed' || stat === 'blocked';
    const commitLabel = b.state === 'committed' ? '✓ 已写入' : '确认此单';
    return `
      <div data-errs="${b.idx}">${errs.length ? `<div class="alert alert-error"><b>无法提交：</b><br>${errs.map(esc).join('<br>')}</div>` : ''}</div>
      ${aiFlag ? `<div class="alert ${ai.confidence === 'low' ? 'alert-warn' : 'alert-info'}"><b>AI 提示（置信度：${esc(ai.confidence || '—')}）：</b>${esc(ai.note || '')}${(ai.problems || []).length ? '<br>解析问题：' + ai.problems.map(esc).join('；') : ''}</div>` : ''}
      ${suggHtml}
      ${(p.duplicate && p.duplicate.hit) ? `<div class="alert alert-warn"><b>⚠ 疑似重复录入：</b><br>${(p.duplicate.matches || []).map(m => `近 ${p.duplicate.window_minutes} 分钟已有 <b>${esc(m.number)}</b>（${esc(m.time)}，${esc(m.party)}，${(m.hits || []).map(h => esc(h.name) + '×' + h.qty).join('、')}）`).join('<br>')}<br>若这是同一笔业务，请先核对上面的单号，避免重复入库/出库。</div>` : ''}
      ${toCreate.length ? `<div class="alert alert-warn"><b>⚠ 将自动新建：</b>${toCreate.map(esc).join('；')}</div>` : ''}
      ${warns.length ? `<div class="alert alert-info"><b>提醒：</b><br>${warns.map(esc).join('<br>')}</div>` : ''}
      <div data-res="${b.idx}">${b.state === 'committed' && b.res
        ? `<div class="alert alert-ok"><b>✓ 已写入：</b>${esc(b.res.number || '')} ${esc(b.res.message || '')}${REVERSIBLE[d.biz] ? `<div class="mt-2"><button type="button" class="btn btn-ghost btn-sm" data-reverse="${b.idx}">生成${REVERSIBLE[d.biz]}（冲销本单）</button></div>` : ''}</div>`
        : (b.state === 'failed' && b.res
          ? `<div class="alert alert-error"><b>写入失败：</b>${esc(b.res.message || '请检查后重试')}${b.res.need_confirm ? '（令牌失效，点重试会先刷新确认令牌）' : ''}<div class="mt-2"><button type="button" class="btn btn-ghost btn-sm" data-retry="${b.idx}">重试本单</button></div></div>`
          : '')}</div>
      <div class="pv-meta-grid">
        <div class="pv-field pv-f-party">
          <div class="pv-field-hd">
            <label for="__mparty_${b.idx}">${esc((p.party && p.party.type) || (BIZ[d.biz] && BIZ[d.biz].party) || '往来单位')}</label>
            <span class="pv-partytag">${partyTag()}</span>
          </div>
          <div class="ac-wrap"><input id="__mparty_${b.idx}" class="pv-party" value="${esc(d.party || '')}" placeholder="名称，没有的会自动新建" autocomplete="off"></div>
          <div class="pv-partycand">${d.party && p.party && !p.party.matched ? pvCandChips(p.party.candidates, `${b.idx}:party`) : ''}</div>
        </div>
        <div class="pv-field pv-f-depot">
          <div class="pv-field-hd">
            <label for="__mdepot_${b.idx}">仓库</label>
            <span class="pv-depottag">${pvDepotTag(d.depot)}</span>
          </div>
          <input id="__mdepot_${b.idx}" class="pv-depot" list="__depotListM" value="${esc(d.depot || '')}" placeholder="留空用默认仓（${esc((p.depot && p.depot.name) || '默认仓')}）">
        </div>
        <div class="pv-field pv-f-time">
          <div class="pv-field-hd">
            <label for="__motime_${b.idx}">日期</label>
            ${effTime ? '' : '<span class="pv-field-note">留空用当前时间</span>'}
          </div>
          <input id="__motime_${b.idx}" class="pv-otime" type="datetime-local" value="${esc(dtVal)}">
        </div>
      </div>
      <div class="pv-meta-grid-2">
        <div class="pv-field">
          <div class="pv-field-hd">
            <label for="__mpay_${b.idx}">${d.biz && d.biz.includes('sale') ? '实收金额' : '实付金额'}</label>
            <span class="pv-field-note pv-paynote">${(d.pay_amount === null || d.pay_amount === undefined || d.pay_amount === '') ? '全款结清' : ((Math.round((billTotal(b) - num(d.pay_amount)) * 100) / 100 > 0) ? `欠款 ￥${money(Math.round((billTotal(b) - num(d.pay_amount)) * 100) / 100)}` : '全款结清')}</span>
          </div>
          <input id="__mpay_${b.idx}" class="pv-payamt" type="number" step="any" min="0" value="${d.pay_amount ?? ''}" placeholder="留空按全款结清">
        </div>
        <div class="pv-field">
          <div class="pv-field-hd"><label for="__mrem_${b.idx}">备注</label></div>
          <input id="__mrem_${b.idx}" class="pv-remark" type="text" value="${esc(d.remark || '')}" placeholder="选填备注">
        </div>
      </div>
      ${pvTableHtml(rows, `class="m-pvbody" data-i="${b.idx}"`)}
      <div class="pv-items-foot">
        <button type="button" class="btn btn-sm btn-ghost" data-addrow="${b.idx}">＋ 加一行</button>
        <div class="total-row">本单合计：<span class="amount" data-total="${b.idx}">￥${money(billTotal(b))}</span></div>
      </div>
      <div class="pv-dirty${b.dirty ? '' : ' hidden'}" data-dirty="${b.idx}">✏ 内容已修改：写入前会自动刷新预览换新令牌。</div>
      <details class="m-att-fold"${b.attach.length ? ' open' : ''}>
        <summary>📎 附件（选填 · 发票 / 合同${b.attach.length ? ` · 已选 ${b.attach.length} 份` : ''}）</summary>
        <div class="attach-zone" data-att="${b.idx}"></div>
      </details>
      <div class="m-card-actions">
        <button type="button" class="btn" data-refresh="${b.idx}">↻ 刷新预览</button>
        <span class="hint pv-kbd-hint" style="flex:1;margin:0">表格里可直接改数</span>
        <button type="button" class="btn btn-primary" data-commitone="${b.idx}"${commitDisabled ? ' disabled' : ''}>${commitLabel}</button>
      </div>`;
  }

  function renderCard(i) {
    const b = st.bills[i];
    let el = $(`.m-card[data-i="${i}"]`);
    if (!el) {
      // 壳由这里按需创建（骨架里只有空的 #__mlist 容器）：没有壳就先建再填，
      // 否则弹层永远是空壳（浏览器实测抓到的 bug，单测覆盖不到 DOM 装配层）
      el = document.createElement('div');
      el.className = 'm-card';
      el.dataset.i = i;
      const list = $('#__mlist');
      (list || document.body).appendChild(el);
    }
    const stat = statusOf(b);
    el.className = `m-card ${stat}` + (b.collapsed ? '' : ' open');
    const one = (b.preview.errors || [])[0];
    const sel = $(`.m-sel[data-sel="${i}"]`);
    const lockSel = stat === 'blocked' || stat === 'committed';
    const isChecked = lockSel ? (stat === 'committed') : (sel ? sel.checked : true);
    const bizLabel = (BIZ[b.draft.biz] || {}).label || b.draft.biz || '';
    const itemCnt = (b.draft.items || []).length;
    el.innerHTML = `
      <div class="m-card-hd" data-toggle="${i}" role="button" tabindex="0" aria-expanded="${!b.collapsed}">
        <label class="m-sel-wrap" aria-label="选择第 ${i + 1} 单">
          <input class="m-sel" data-sel="${i}" type="checkbox" ${lockSel ? 'disabled' : ''} ${isChecked ? 'checked' : ''}>
        </label>
        <div class="m-hd-info">
          <div class="m-hd-row1">
            <span class="m-idx">#${i + 1}</span>
            <span class="m-party" data-hdparty="${i}">${esc(b.draft.party || '未填往来单位')}</span>
            <span class="m-biz-pill">${esc(bizLabel)}</span>
          </div>
          <div class="m-hd-row2">
            <span class="m-items-cnt">${itemCnt} 项商品</span>
            <span class="m-status ${stat}">${STATUS_ZH[stat]}${stat === 'blocked' && one ? ' · ' + esc(one) : ''}</span>
          </div>
        </div>
        <div class="m-hd-right">
          <b class="m-total" data-hdtotal="${i}">￥${money(billTotal(b))}</b>
          <span class="caret" aria-hidden="true">▾</span>
        </div>
      </div>
      <div class="m-body" style="${b.collapsed ? 'display:none' : ''}">${cardBody(b)}</div>`;

    // 附件管理器：以 st.attach 为初始状态（卡片重渲染不丢已传附件）
    const zone = $(`.attach-zone[data-att="${i}"]`, el);
    if (zone) {
      b._att = AttachmentManager(zone, { biz: 'bill', max: 4, initial: b.attach });
      b._getAttach = () => {
        const ps = b._att ? b._att.getPaths() : b.attach.map(a => a.path);
        b.attach = ps.map(p => ({ path: p, name: p.split('/').pop() }));
        return b.attach;
      };
    }
    bindCard(i, el);
  }

  function syncToggleAllBtn() {
    const tbtn = $('#__mtoggleAll');
    if (!tbtn) return;
    const allOpen = st.bills.length > 0 && st.bills.every(b => !b.collapsed);
    tbtn.textContent = allOpen ? '全部收起' : '展开全部';
    tbtn.disabled = st.bills.length <= 1;
  }

  function renderAll() {
    const dl = $('#__depotListM');
    if (dl) dl.innerHTML = (MD.depots || []).map(x => `<option value="${esc(x.name)}">`).join('');
    st.bills.forEach((_, i) => renderCard(i));
    updateBars();
  }

  function updateBars() {
    const n = st.bills.length;
    const cnt = { ready: 0, warned: 0, blocked: 0, committed: 0, failed: 0 };
    st.bills.forEach(b => { cnt[statusOf(b)]++; });
    const selectable = st.bills.filter(b => {
      const s = $(`.m-sel[data-sel="${b.idx}"]`);
      return s && !s.disabled;
    });
    const selectedBills = selectable.filter(b => $(`.m-sel[data-sel="${b.idx}"]`).checked);
    const selCount = selectedBills.length;
    const selTotal = selectedBills.reduce((t, b) => t + billTotal(b), 0);

    const sb = $('#__msum');
    if (sb) {
      const parts = [
        `<span class="m-stat total">共 <b>${n}</b> 张</span>`,
        `<span class="m-stat ok">可写入 <b>${cnt.ready}</b></span>`,
      ];
      if (cnt.warned) parts.push(`<span class="m-stat warn">需注意 <b>${cnt.warned}</b></span>`);
      if (cnt.blocked) parts.push(`<span class="m-stat bad">无法提交 <b>${cnt.blocked}</b></span>`);
      parts.push(`<span class="m-stat ${cnt.committed ? 'ok' : ''}">已写入 <b>${cnt.committed}</b></span>`);
      if (cnt.failed) parts.push(`<span class="m-stat bad">失败 <b>${cnt.failed}</b></span>`);
      sb.innerHTML = parts.join('');
    }

    const selAll = $('#__mselall');
    if (selAll) {
      selAll.disabled = selectable.length === 0;
      selAll.checked = selectable.length > 0 && selCount === selectable.length;
      selAll.indeterminate = selCount > 0 && selCount < selectable.length;
    }

    const selSum = $('#__mselSummary');
    if (selSum) {
      if (cnt.committed === n && n > 0) {
        selSum.innerHTML = '<span>全部单据已写入 ✓</span>';
      } else if (selCount > 0) {
        selSum.innerHTML = `已选 <b>${selCount}</b> 单 · 合计 <b class="m-grand-total">￥${money(selTotal)}</b>`;
      } else {
        selSum.innerHTML = '<span>未勾选待写入单据</span>';
      }
      selSum.classList.toggle('hidden', !!st.busy);
    }

    const pw = $('#__mprogWrap');
    if (pw) pw.classList.toggle('hidden', !st.busy);

    syncToggleAllBtn();

    const btn = $('#__mbatch');
    if (btn) {
      btn.disabled = selCount === 0 || st.busy;
      btn.textContent = st.busy ? '写入中…' : `确认写入勾选的 ${selCount} 单`;
    }
  }

  function bindCard(i, el) {
    const b = st.bills[i];
    const markDirty = () => {
      b.dirty = true;
      const db = $(`[data-dirty="${b.idx}"]`, el);
      if (db) db.classList.remove('hidden');
    };
    const refreshPartyTag = () => {
      const d = b.draft, t = $('.pv-partytag', el);
      if (t) t.innerHTML = (!(d.party || '').trim()) ? ''
        : (findExact(partyPool(d.biz), d.party) ? '<span class="tag tag-ok">已有</span>' : '<span class="tag tag-new">将新建</span>');
    };
    const syncHeaderParty = () => {
      const hp = $(`[data-hdparty="${i}"]`, el);
      if (hp) hp.textContent = b.draft.party || '未填往来单位';
    };
    const toggleCard = () => {
      b.collapsed = !b.collapsed;
      const body = $('.m-body', el);
      body.style.display = b.collapsed ? 'none' : '';
      el.classList.toggle('open', !b.collapsed);
      const hd = $('.m-card-hd', el);
      if (hd) hd.setAttribute('aria-expanded', String(!b.collapsed));
      syncToggleAllBtn();
    };
    // 折叠
    const hd = $('.m-card-hd', el);
    hd.onclick = e => {
      if (e.target.closest('.m-sel-wrap') || e.target.closest('.m-sel')) return;
      toggleCard();
    };
    hd.onkeydown = e => {
      if (e.target !== hd) return;
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleCard(); }
    };
    // 勾选
    $('.m-sel', el).onchange = updateBars;
    // 输入绑定
    const bindScope = () => {
      const party = $('.pv-party', el);
      if (party) party.addEventListener('input', () => {
        b.draft.party = party.value.trim();
        refreshPartyTag();
        syncHeaderParty();
        markDirty();
      });
      const depot = $('.pv-depot', el);
      if (depot) depot.addEventListener('input', () => {
        b.draft.depot = depot.value.trim();
        const dt = $('.pv-depottag', el);
        if (dt) dt.innerHTML = pvDepotTag(b.draft.depot);
        markDirty();
      });
      const ot = $('.pv-otime', el);
      if (ot) ot.addEventListener('change', () => {
        const v = ot.value || '';
        b.draft.oper_time = v ? (v.replace('T', ' ') + (v.length === 16 ? ':00' : '')) : '';
        markDirty();
      });
      const payEl = $('.pv-payamt', el);
      if (payEl) payEl.addEventListener('input', () => {
        const raw = payEl.value.trim();
        b.draft.pay_amount = raw === '' ? null : num(raw);
        const pn = $('.pv-paynote', el);
        if (pn) {
          const debt = b.draft.pay_amount === null ? 0 : Math.round((billTotal(b) - num(b.draft.pay_amount)) * 100) / 100;
          pn.textContent = (b.draft.pay_amount === null || debt <= 0) ? '全款结清' : `欠款 ￥${money(debt)}`;
        }
        markDirty();
      });
      const remEl = $('.pv-remark', el);
      if (remEl) remEl.addEventListener('input', () => {
        b.draft.remark = remEl.value.trim();
        markDirty();
      });
      const body = $('.m-pvbody', el);
      if (body) {
        body.addEventListener('input', e => {
          const t = e.target;
          const ri = +t.dataset.i;
          const it = (b.draft.items || [])[ri];
          if (!it) return;
          if (t.classList.contains('pv-qty')) it.qty = num(t.value);
          else if (t.classList.contains('pv-price')) it.price = num(t.value);
          else if (t.classList.contains('pv-unit')) it.unit = t.value.trim() || '个';
          else return;   // pv-name 走 autocomplete
          const amt = $(`.pv-amt[data-i="${ri}"]`, body);
          if (amt) amt.textContent = money(num(it.qty) * num(it.price));
          const totalText = '￥' + money(billTotal(b));
          const tot = $(`[data-total="${b.idx}"]`, el);
          if (tot) tot.textContent = totalText;
          const hdTot = $(`[data-hdtotal="${b.idx}"]`, el);
          if (hdTot) hdTot.textContent = totalText;
          markDirty();
          updateBars();
        });
        body.addEventListener('click', e => {
          const del = e.target.closest('[data-del]');
          if (del) {
            const ri = +del.dataset.del;
            const items = b.draft.items || [];
            if (items.length > 1) items.splice(ri, 1);
            else items[0] = { name: '', qty: 0, unit: '个', price: 0 };
            b.dirty = true;
            renderCard(i);
            updateBars();
            return;
          }
          const chip = e.target.closest('[data-pick]');
          if (chip) pickCandidate(i, chip);
        });
        const add = $(`[data-addrow="${i}"]`, el);
        if (add) add.onclick = () => {
          (b.draft.items = b.draft.items || []).push({ name: '', qty: 0, unit: '个', price: 0 });
          b.dirty = true;
          renderCard(i);
          updateBars();
          const inps = $$('.pv-name', $(`.m-card[data-i="${i}"]`));
          if (inps.length) inps[inps.length - 1].focus();
        };
      }
      // 往来单位候选点选
      const partyCand = $('.pv-partycand', el);
      if (partyCand) {
        partyCand.addEventListener('click', e => {
          const chip = e.target.closest('[data-pick]');
          if (chip) pickCandidate(i, chip);
        });
      }
      // 商品自动补全（点选锁档案，手改解除——与单单确认卡同规则）
      $$('.pv-name', el).forEach(inp => {
        let pickedNow = false;
        autocomplete(inp, () => MD.materials,
          it2 => {
            const ri = +inp.dataset.i;
            const it = b.draft.items[ri];
            if (!it) return;
            pickedNow = true;
            if (it2.id) it.material_id = it2.id;
            if (it2.unit && !it.unit) {
              it.unit = it2.unit;
              const u = $(`.pv-unit[data-i="${ri}"]`, el);
              if (u) u.value = it2.unit;
            }
          },
          () => {
            const ri = +inp.dataset.i;
            const it = b.draft.items[ri];
            if (!it) return;
            it.name = inp.value.trim();
            if (!pickedNow) delete it.material_id;
            pickedNow = false;
            markDirty();
          });
      });
      if (party) autocomplete(party, () => partyPool(b.draft.biz), null, () => {
        b.draft.party = party.value.trim();
        refreshPartyTag();
        syncHeaderParty();
        markDirty();
      });
    };
    bindScope();

    $(`[data-refresh="${i}"]`, el).onclick = () => refreshBill(i);
    $(`[data-commitone="${i}"]`, el).onclick = () => commitOne(i);
    $$(`[data-applysugg]`, el).forEach(btn => {
      btn.onclick = () => {
        const parts = (btn.dataset.applysugg || '').split(':');
        const bi = +parts[0], si = +parts[1];
        const sb = st.bills[bi];
        const sg = sb && sb.suggestions && sb.suggestions[si];
        if (!sg) return;
        if (sg.field === 'depot') {
          sb.draft.depot = String(sg.value || '');
        } else if (sg.field === 'price') {
          const ii = Number(sg.item_index || 0);
          if (sb.draft.items && sb.draft.items[ii]) sb.draft.items[ii].price = num(sg.value);
        }
        sb.suggestions.splice(si, 1);
        sb.dirty = true;
        refreshBill(bi, true);
      };
    });
    const rv = $(`[data-reverse="${i}"]`, el);
    if (rv) rv.onclick = () => reverseBill(i);
    const rt = $(`[data-retry="${i}"]`, el);
    if (rt) rt.onclick = () => commitOne(i);
  }

  async function pickCandidate(cardIdx, chip) {
    const b = st.bills[cardIdx];
    const pick = chip.dataset.pick.split(':');   // [cardIdx, 'party'|'item', rowIdx?]
    const cand = chip.dataset.cand;
    if (pick[1] === 'party') b.draft.party = cand;
    else {
      const it = b.draft.items && b.draft.items[+pick[2]];
      if (!it) return;
      it.name = cand;
      if (chip.dataset.candMid) it.material_id = +chip.dataset.candMid;
    }
    chip.textContent = '…';
    await refreshBill(cardIdx, true);
  }

  async function refreshBill(i, silent) {
    const b = st.bills[i];
    try {
      const r = await api('/api/resolve', { method: 'POST', body: { draft: b.draft, source: st.source } });
      if (!r.ok) { toast(r.message || '刷新预览失败', 'bad'); return false; }
      if (r.draft) b.draft = r.draft;
      b.preview = r.preview;
      b.confirm_token = r.preview.confirm_token;
      b.dirty = false;
      if (b.state === 'failed') b.state = 'open';
      renderCard(i);
      updateBars();
      if (!silent) toast(`第 ${i + 1} 单预览已刷新`, 'ok');
      return true;
    } catch (e) { toast(e.message, 'bad'); return false; }
  }

  async function commitOne(i) {
    const b = st.bills[i];
    if (b.state === 'committed') return;
    // 失败单的令牌可能已被闸门消费：重试前必须换新令牌，否则必然再被拒
    if ((b.dirty || b.state === 'failed') && !(await refreshBill(i, true))) return;
    const btn = $(`[data-commitone="${i}"]`);
    if (btn) { btn.disabled = true; btn.textContent = '写入中…'; }
    try {
      const d = Object.assign({}, b.draft);
      const att = b._getAttach ? b._getAttach() : b.attach;
      if (att.length) d.file_name = att.map(x => x.path).join(',');
      const res = await api('/api/commit', { method: 'POST', body: {
        draft: d, confirm_token: b.confirm_token,
        source: st.source, raw_text: st.rawText, operator: await ensureOperator(),
      } });
      if (res.ok) {
        b.state = 'committed'; b.res = res;
        toast(`✓ 第 ${i + 1} 单已写入：${res.message || ''}`, 'ok');
      } else {
        b.state = 'failed'; b.res = res;
        toast(res.message || '写入失败', 'bad');
      }
    } catch (e) {
      b.state = 'failed'; b.res = { ok: false, message: e.message };
      toast(e.message, 'bad');
    }
    renderCard(i);
    updateBars();
  }

  async function reverseBill(i) {
    const b = st.bills[i];
    try {
      const r = await api('/api/bill/reverse', { method: 'POST', body: {
        draft: b.draft, bill_no: (b.res && b.res.number) || '', reason: '退货/冲销',
      } });
      if (!r.ok) { toast(r.message || '生成反向草稿失败', 'bad'); return; }
      const idx = st.bills.length;
      st.bills.push({
        idx,
        draft: r.draft,
        preview: r.preview,
        confirm_token: r.preview.confirm_token,
        ai: { confidence: 'high', note: r.message || '反向单据（原单不动，新增冲销单）', problems: [] },
        attach: [], collapsed: false, dirty: false, state: 'open', res: null,
      });
      renderCard(idx);
      updateBars();
      toast(r.message || '已生成反向草稿，确认后写入', 'ok');
    } catch (e) { toast(e.message, 'bad'); }
  }

  async function commitSelected() {
    if (st.busy) return;
    const sel = st.bills.filter(b => {
      const s = $(`.m-sel[data-sel="${b.idx}"]`);
      return s && s.checked && b.state !== 'committed';
    });
    if (!sel.length) { toast('请先勾选要写入的单据', 'warn'); return; }
    st.busy = true;
    const prog = $('#__mprog'), progFill = prog ? prog.querySelector('i') : null;
    const progTxt = $('#__mprogTxt');
    if (progFill) progFill.style.width = '0%';
    if (progTxt) progTxt.textContent = '准备写入…';
    updateBars();
    // 脏单/失败单先刷新换新令牌（旧令牌绑定旧内容或已被消费，直接提交会被闸门拒）
    for (const b of sel) {
      if (b.dirty || b.state === 'failed') {
        if (progTxt) progTxt.textContent = `刷新第 ${b.idx + 1} 单预览…`;
        const ok = await refreshBill(b.idx, true);
        if (!ok) {
          st.busy = false;
          if (progTxt) progTxt.textContent = '';
          updateBars();
          return;
        }
      }
    }
    const items = sel.map(b => {
      const d = Object.assign({}, b.draft);
      const att = b._getAttach ? b._getAttach() : b.attach;
      if (att.length) d.file_name = att.map(x => x.path).join(',');
      return { draft: d, confirm_token: b.confirm_token, _bill: b };
    });
    try {
      const r = await commitBatch(items, {
        source: st.source, rawText: st.rawText,
        onResult: (gi, res) => {
          const b = items[gi]._bill;
          b.state = res.ok ? 'committed' : 'failed';
          b.res = res;
          renderCard(b.idx);
        },
        onProgress: (done, total) => {
          if (progFill) progFill.style.width = Math.round(done / total * 100) + '%';
          if (progTxt) progTxt.textContent = `写入中 ${done}/${total}`;
        },
      });
      toast(`批量写入完成：成功 ${r.okCount} / ${r.total}` + (r.okCount < r.total ? '（失败单已标红，可修改后重试）' : ''), r.okCount === r.total ? 'ok' : 'warn');
    } catch (e) {
      toast(e.message, 'bad');
    }
    st.busy = false;
    if (progTxt) progTxt.textContent = '';
    updateBars();
  }

  async function close() {
    const pending = st.bills.filter(b => b.state === 'open').length;
    if (pending && !st.done) {
      // 原生 confirm 在公网 WebView 里不存在（整页卡死，公网部署实测教训）——用自绘弹窗
      // askInline 取消=resolve(null)、确定=resolve(输入值)——判定用 v !== null（空输入点确定也算确认）
      const ok = await askInline({
        title: '还有 ' + pending + ' 张单没写入，确定关闭？',
        hint: '已写入的不受影响；未写入的草稿保留在会话里。', okText: '关闭',
      }).then(v => v !== null).catch(() => false);
      if (!ok) return;
    }
    if (detachKey) { detachKey(); detachKey = null; }
    const mask = $('#__mmask');
    if (mask) mask.remove();
    if (extra.onClose) extra.onClose(st.bills.map(({ _att, _getAttach, ...rest }) => {
      const att = _getAttach ? _getAttach() : rest.attach;
      return Object.assign(rest, { attach: att });
    }));
  }

  // ---------- 骨架 ----------
  const mask = document.createElement('div');
  mask.id = '__mmask';
  mask.className = 'multi-mask';
  mask.setAttribute('role', 'dialog');
  mask.setAttribute('aria-modal', 'true');
  mask.setAttribute('aria-labelledby', '__mtitle');
  mask.innerHTML = `
    <div class="multi-wrap">
      <div class="m-head">
        <div class="m-head-main">
          <h2 id="__mtitle">批量确认</h2>
          <div class="m-count" id="__msum"></div>
        </div>
        <button type="button" class="modal-x m-close" title="关闭（Esc）" aria-label="关闭">×</button>
      </div>
      <div class="m-sumbar">
        <label class="m-selall"><input type="checkbox" id="__mselall" checked><span>全选可写入</span></label>
        <span class="m-sumbar-hint">每单独立过闸门，改数自动刷新令牌</span>
        <button type="button" class="btn btn-sm btn-ghost m-toggle-all" id="__mtoggleAll">展开全部</button>
      </div>
      <div class="m-list" id="__mlist"></div>
      <datalist id="__depotListM"></datalist>
      <div class="m-sticky">
        <div class="m-sticky-info">
          <div class="m-sel-summary" id="__mselSummary"></div>
          <div class="m-prog-wrap hidden" id="__mprogWrap">
            <div class="m-prog" id="__mprog"><i></i></div>
            <span class="hint-c" id="__mprogTxt"></span>
          </div>
        </div>
        <div class="m-sticky-actions">
          <span class="hint-c m-esc-hint">Esc 关闭</span>
          <button type="button" class="btn btn-primary btn-lg" id="__mbatch">确认写入</button>
        </div>
      </div>
    </div>`;
  document.body.appendChild(mask);
  mask.querySelector('.m-close').onclick = close;
  let downOnMask = false;
  mask.addEventListener('mousedown', e => { downOnMask = e.target === mask; });
  mask.addEventListener('mouseup', e => { if (downOnMask && e.target === mask) close(); downOnMask = false; });
  $('#__mselall').onchange = e => {
    const on = e.target.checked;
    $$('.m-sel').forEach(s => { if (!s.disabled) s.checked = on; });
    updateBars();
  };
  $('#__mtoggleAll').onclick = () => {
    const shouldExpand = !st.bills.every(b => !b.collapsed);
    st.bills.forEach(b => { b.collapsed = !shouldExpand; });
    renderAll();
  };
  $('#__mbatch').onclick = commitSelected;
  detachKey = trapFocus(mask.querySelector('.multi-wrap'), () => {
    if (!$('#__askInline')) close();
  });
  renderAll();
  // 已有/将新建标签依赖主数据池——问一问等页面首次打开时 MD 还是空的，这里补拉
  // 并重渲染（不阻塞首帧；bill.html 已提前 loadMaster，这里只兜底）
  if (!MD.loaded) loadMaster().then(renderAll).catch(() => {});
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
  { sec: '主入口', ico: '❝', t: 'ERP Agent', d: '查账随口问；录单直接说，草稿确认后写入', go: () => '/ask' },
  { sec: '主入口', ico: '▦', t: '表格导入', d: 'Excel / CSV 批量导入（助手内工具页）', go: () => '/new?mode=import' },
  { sec: '主入口', ico: '✎', t: '对话录单', d: '说一句话，AI 拆成草稿', go: () => '/new?mode=ai' },
  { sec: '主入口', ico: '▤', t: '表单录单', d: '逐字段精确控制，不依赖 AI', go: () => '/new?mode=form' },
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
      list.innerHTML = '<div class="cmdk-empty">没有匹配的命令<br><span style="font-size:12.5px">试试「看板」「录单」「ERP Agent」</span></div>';
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
  // 手机端没有物理键盘：快捷提示无意义，且会盖住底部 Tab —— 直接不生成
  if (window.matchMedia && window.matchMedia('(max-width: 900px)').matches) return;
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
// 入口收敛：ERP Agent 是唯一主入口；录单页降级为助手内的工具（/new?mode=…）
const NAV_ITEMS = [
  ['/ask', 'ERP Agent', '❝', 'Agent'],
  ['/dashboard', '经营看板', '◫', '看板'],
  ['/new', '录单工具', '▤', '录单'],
  ['/settings', '系统设置', '⚙', '设置'],
];
const PAGE_TITLE = { '/ask': 'ERP Agent', '/dashboard': '经营看板', '/new': '录单工具', '/settings': '系统设置', '/': '工作台' };
const ERP_URL = location.protocol + '//' + location.hostname + ':8080';

function shell(active) {
  document.body.classList.add('has-shell');
  const nav = document.createElement('aside');
  nav.className = 'shell-nav';
  nav.id = '__shellNav';
  nav.setAttribute('aria-label', '主导航');
  nav.innerHTML = `
    <div class="chat-side-top">
      <div class="side-brand">
        <span class="agent-mark"><img src="/avatars/agent-cat-v3.jpg" alt=""></span>
        <div class="side-brand-txt"><strong>Talk2Stock</strong><small>ERP AGENT</small></div>
      </div>
      <button type="button" class="chat-side-fold" id="__shellFold" title="收起侧栏" aria-label="收起侧栏">«</button>
    </div>
    <div class="nav-sec">主要入口</div>
    ${NAV_ITEMS.map(([h, t, i]) =>
      `<a class="nav-item${h === active ? ' on' : ''}" href="${h}"><span class="ico">${i}</span>${t}</a>`).join('')}
    <div class="nav-sec">快捷工具</div>
    <a class="nav-item" href="${ERP_URL}" target="_blank" rel="noopener"><span class="ico">▦</span>原版 ERP ↗</a>
    <a class="nav-item" id="__navCmd" href="javascript:void 0" title="快捷键 Ctrl/⌘ + K"><span class="ico">⌘</span>命令面板</a>
    <div class="nav-foot">
      <div class="nav-status"><span class="nav-dot" id="__navDot"></span><span id="__navStat">连接检查中…</span></div>
      <div class="nav-foot-row">
        <div id="__navUser" class="me-chip"></div>
        <button type="button" class="chat-side-acct" id="__navAcct" title="查看账号">账号</button>
      </div>
    </div>`;
  document.body.prepend(nav);

  const mask = document.createElement('div');
  mask.className = 'chat-side-mask';
  mask.id = '__shellMask';
  mask.hidden = true;
  document.body.insertBefore(mask, nav.nextSibling);

  const main = document.createElement('div');
  main.className = 'shell-main';
  while (document.body.children.length > 2) main.appendChild(document.body.children[2]);

  const hdr = document.createElement('header');
  hdr.className = 'shell-header';
  hdr.innerHTML = `
    <div class="agent-brand">
      <button type="button" class="agent-menu" id="__shellMenu" title="展开导航" aria-label="展开导航" aria-expanded="false">☰</button>
      <span class="agent-mark shell-hdr-mark"><img src="/avatars/agent-cat-v3.jpg" alt=""></span>
      <strong>${esc(PAGE_TITLE[active] || 'Talk2Stock')}</strong>
      <span class="agent-presence" id="__hdrPresence">正在连接 ERP</span>
    </div>
    <div class="agent-header-actions">
      <a href="/ask" class="hdr-ask-pill" title="前往 ERP Agent 对话"><span class="ha-ico">❝</span><span class="ha-txt">问 Agent</span></a>
      <button type="button" class="header-cmd-btn" id="__hdrCmd" title="命令面板 (⌘/Ctrl+K)">⌘K</button>
    </div>`;
  main.prepend(hdr);
  document.body.appendChild(main);

  // 手机底部 Tab（统一双端主导航：Agent · 看板 · 录单 · 设置 · 账号）
  const tab = document.createElement('div');
  tab.className = 'tabbar';
  tab.innerHTML = NAV_ITEMS.map(([h, t, i, s]) =>
    `<a href="${h}" class="${h === active ? 'on' : ''}"><span class="ico">${i}</span>${s || t}</a>`).join('')
    + '<a href="javascript:void 0" id="__tabAcct"><span class="ico">◉</span>账号</a>';
  document.body.appendChild(tab);
  const tabAcct = $('#__tabAcct');
  if (tabAcct) tabAcct.onclick = () => accountSheet();
  const navAcct = $('#__navAcct');
  if (navAcct) navAcct.onclick = () => { closeDrawer(); accountSheet(); };

  const navCmd = $('#__navCmd');
  if (navCmd) navCmd.onclick = () => { closeDrawer(); commandPalette(); };
  const hdrCmd = $('#__hdrCmd');
  if (hdrCmd) hdrCmd.onclick = () => commandPalette();

  const closeDrawer = () => {
    nav.classList.remove('is-open');
    mask.hidden = true;
    const m = $('#__shellMenu');
    if (m) m.setAttribute('aria-expanded', 'false');
  };
  const openDrawer = () => {
    nav.classList.add('is-open');
    mask.hidden = false;
    const m = $('#__shellMenu');
    if (m) m.setAttribute('aria-expanded', 'true');
  };
  const menuBtn = $('#__shellMenu');
  if (menuBtn) {
    menuBtn.onclick = () => {
      if (matchMedia('(max-width:900px)').matches) {
        nav.classList.contains('is-open') ? closeDrawer() : openDrawer();
        return;
      }
      const collapsed = document.body.classList.toggle('side-collapsed');
      try { localStorage.setItem('t2s_side_collapsed', collapsed ? '1' : '0'); } catch (_) {}
    };
  }
  const foldBtn = $('#__shellFold');
  if (foldBtn) {
    foldBtn.onclick = () => {
      if (matchMedia('(max-width:900px)').matches) { closeDrawer(); return; }
      document.body.classList.add('side-collapsed');
      try { localStorage.setItem('t2s_side_collapsed', '1'); } catch (_) {}
    };
  }
  mask.onclick = closeDrawer;
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });
  try { if (localStorage.getItem('t2s_side_collapsed') === '1') document.body.classList.add('side-collapsed'); } catch (_) {}

  // ERP 连接状态（同步左侧边栏与顶栏）
  api('/api/master').then(() => {
    const d = $('#__navDot'), s = $('#__navStat'), p = $('#__hdrPresence');
    if (d) d.className = 'nav-dot ok';
    if (s) s.textContent = 'ERP 已连接';
    if (p) { p.textContent = 'ERP 在线'; p.classList.add('is-online'); }
  }).catch(() => {
    const d = $('#__navDot'), s = $('#__navStat'), p = $('#__hdrPresence');
    if (d) d.className = 'nav-dot bad';
    if (s) s.textContent = 'ERP 未连接';
    if (p) { p.textContent = 'ERP 待连接'; p.classList.add('is-offline'); }
  });

  // 登录者徽章（401 时 api() 会先弹登录框，成功后再回来刷新）
  renderUserChip();

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
