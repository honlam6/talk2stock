/* Talk2Stock · 主对话内的表格导入工具。
   文件解析、列映射、行修复各负其责；只产出草稿，不直接写入 ERP。 */
(() => {
'use strict';
const FIELDS = [['party','往来单位'],['item_name','商品 / 规格'],['qty','数量'],['unit','单位'],['price','单价'],['amount','金额'],['depot','仓库'],['oper_time','日期'],['pay_amount','实付'],['tax_rate','税率'],['remark','备注'],['bill_no','原单号']];
const VALUE = v => String(v == null ? '' : v).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
function letter(i) { let s = '', n = i + 1; while (n) { const r = (n - 1) % 26; s = String.fromCharCode(65 + r) + s; n = Math.floor((n - 1) / 26); } return s; }
function formApi(path, fd) {
  const headers = {}, pass = localStorage.getItem('t2s_pass'); if (pass) headers['X-T2S-Pass'] = pass;
  return fetch(path, {method:'POST',headers,body:fd}).then(async r => {
    if (r.status === 401 && window.askLogin) { const c = await askLogin(); if (c) return formApi(path,fd); }
    const text = await r.text(); try { return JSON.parse(text); } catch (_) { throw new Error('服务返回异常：' + text.slice(0,80)); }
  });
}
function open(options) {
  options = options || {};
  const old = document.getElementById('__importDialog'); if (old) old.remove();
  const mask = document.createElement('div'); mask.className = 'mask show'; mask.id = '__importDialog';
  mask.innerHTML = '<section class="modal import-dialog" role="dialog" aria-modal="true" aria-labelledby="importTitle"><button class="modal-x" id="importClose" aria-label="关闭">×</button><h2 id="importTitle"><span class="bar"></span>导入表格</h2><div class="imp-steps"><span class="imp-step on" data-step="0">选择文件</span><span class="imp-step" data-step="1">确认字段</span><span class="imp-step" data-step="2">检查草稿</span></div><div id="importBody"></div></section>';
  document.body.appendChild(mask);
  const body = mask.querySelector('#importBody');
  const st = {ref:'',sheet:'',sheets:[],mapping:null,result:null,repair:null,busy:false,closed:false};
  let detachTrap = null;
  function close() { if (st.busy) { toast('当前步骤正在处理，请稍候','warn'); return; } st.closed = true; if (detachTrap) detachTrap(); mask.remove(); }
  detachTrap = trapFocus(mask,close); mask.querySelector('#importClose').onclick = close;
  mask.addEventListener('click',e => { if (e.target === mask) close(); });
  function fail(e) { const msg = typeof e === 'string' ? e : (e.message || '处理未完成'); body.insertAdjacentHTML('afterbegin','<div class="alert alert-error" data-import-error>' + VALUE(msg) + '</div>'); }
  function step(n) { mask.querySelectorAll('[data-step]').forEach(x => x.classList.toggle('on',+x.dataset.step === n)); mask.querySelectorAll('[data-step]').forEach(x => x.classList.toggle('done',+x.dataset.step < n)); }
  function choose() {
    step(0); body.innerHTML = '<div class="imp-drop" id="importDrop" tabindex="0" role="button"><div class="ico">▦</div><b>选择 Excel 或 CSV</b><div class="hint">.xlsx / .xlsm / .csv / .tsv</div><input type="file" id="importFile" accept=".xlsx,.xlsm,.csv,.tsv" hidden></div><div class="hint" style="margin-top:12px;text-align:center">先识别列与业务，再由你核对每一张草稿。</div>';
    const input = body.querySelector('#importFile'), drop = body.querySelector('#importDrop');
    drop.onclick = () => input.click(); drop.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } };
    input.onchange = () => { if (input.files[0]) upload(input.files[0]); };
    ['dragover','dragleave','drop'].forEach(type => drop.addEventListener(type,e => { e.preventDefault(); drop.classList.toggle('drag',type === 'dragover'); if (type === 'drop' && e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]); }));
  }
  async function upload(file) {
    if (st.busy) return; st.busy = true; const drop = body.querySelector('#importDrop'); if (drop) drop.innerHTML = '<div class="ico">▦</div><b>正在读取 ' + VALUE(file.name) + '</b><div class="hint">文件仅在本次导入中使用</div>';
    try {
      const fd = new FormData(); fd.append('file',file); const r = await formApi('/api/sheet/inspect',fd); if (!r.ok) throw new Error(r.message || '文件读取失败');
      st.ref = r.ref; st.sheets = r.sheets || [];
      if (!st.sheets.length) throw new Error('没有读到工作表');
      if (st.sheets.length === 1) { st.sheet = st.sheets[0].name; await mapSheet(); }
      else {
        body.innerHTML = '<div class="hint" style="margin-bottom:12px">选择要导入的工作表</div>' + st.sheets.map(s => '<button class="btn" data-sheet="' + VALUE(s.name) + '" style="margin:4px">' + VALUE(s.name) + ' · ' + s.total_rows + ' 行</button>').join('');
        body.querySelectorAll('[data-sheet]').forEach(b => b.onclick = async () => { st.sheet = b.dataset.sheet; b.disabled = true; b.textContent = '正在识别字段…'; await mapSheet(); });
      }
    } catch (e) { choose(); fail(e); } finally { st.busy = false; }
  }
  async function mapSheet() {
    st.busy = true; body.innerHTML = '<div class="empty"><div class="skel skel-text w-80"></div><p>正在识别表头与字段</p></div>';
    try {
      const r = await api('/api/sheet/map',{method:'POST',body:{ref:st.ref,sheet:st.sheet}});
      st.mapping = r.ok ? r.mapping : {header_row:1,mode:'per_row',group_col:'',file_biz:'purchase_in',biz_col:'',cols:{},date_format:'auto'};
      st.result = r.result || null; st.repair = r.repair || null;
      if (r.ok && r.result) { renderRows(); return; }
      renderMapping(r.ok ? '' : (r.message || '请手动选择每列对应的字段'));
    } catch (e) { choose(); fail(e); } finally { st.busy = false; }
  }
  function fieldCols() {
    // 后端返回 {列字母: 字段}；旧版导入 UI 的展示状态是反向映射，不能混用。
    const out = {}; Object.entries(st.mapping.cols || {}).forEach(([k,v]) => {
      const col = /^\d+$/.test(k) ? letter(+k) : k;
      if (!out[col]) out[col] = v;
    }); return out;
  }
  function renderMapping(note) {
    step(1); const m = st.mapping || {}, s = st.sheets.find(x => x.name === st.sheet) || {preview:[],total_cols:0}, cols = fieldCols(), ncol = s.total_cols || 0;
    const opts = (selected,blank=true) => (blank ? '<option value="">不使用</option>':'') + Array.from({length:ncol},(_,i) => { const l = letter(i), h = ((s.preview[Math.max(0,(m.header_row || 1) - 1)] || [])[i] || ''); return '<option value="' + l + '"' + (selected === l ? ' selected':'') + '>' + l + '列 · ' + VALUE(String(h).slice(0,20)) + '</option>'; }).join('');
    body.innerHTML = (note ? '<div class="alert alert-warn">' + VALUE(note) + '</div>':'') + '<div class="hint" style="margin-bottom:10px">' + VALUE(st.sheet) + ' · 确认列对应关系，下面的行会按它整理。</div><div class="tbl-wrap tbl-scroll"><table class="map-table"><tbody><tr><th>表头行</th><td><input id="mapHeader" type="number" min="1" value="' + (m.header_row || 1) + '"></td><th>业务类型</th><td><select id="mapBiz">' + Object.entries(BIZ).map(([k,v]) => '<option value="' + k + '"' + (k === (m.file_biz || 'purchase_in') ? ' selected':'') + '>' + VALUE(v.label) + '</option>').join('') + '</select></td></tr><tr><th>拆单方式</th><td><select id="mapMode"><option value="per_row"' + (m.mode !== 'group' ? ' selected':'') + '>每行一张单</option><option value="group"' + (m.mode === 'group' ? ' selected':'') + '>按单号分组</option></select></td><th>单号列</th><td><select id="mapGroup">' + opts(m.group_col) + '</select></td></tr><tr><th>类型列</th><td><select id="mapBizCol">' + opts(m.biz_col) + '</select></td><th>类型取值</th><td><input id="mapBizValues" value="' + VALUE(Object.entries(m.biz_values || {}).map(([k,v]) => k + '=' + v).join(', ')) + '" placeholder="入库=purchase_in"></td></tr>' + FIELDS.map(([f,zh]) => '<tr><th>' + zh + '</th><td colspan="3"><select data-field="' + f + '">' + opts(cols[f]) + '</select></td></tr>').join('') + '</tbody></table></div><div class="row-btns"><button class="btn btn-ghost" id="mapBack">换文件</button><button class="btn btn-primary" id="mapApply">检查数据 →</button></div>';
    body.querySelector('#mapBack').onclick = choose;
    body.querySelector('#mapApply').onclick = async () => {
      if (st.busy) return; const mcols = {}; body.querySelectorAll('[data-field]').forEach(x => { if (x.value) mcols[x.value] = x.dataset.field; });
      const bizValues = {}; body.querySelector('#mapBizValues').value.split(/[,，]/).forEach(pair => { const [k,v] = pair.split('=').map(x => x.trim()); if (k && v) bizValues[k] = v; });
      st.mapping = {header_row:Math.max(1,parseInt(body.querySelector('#mapHeader').value,10) || 1),mode:body.querySelector('#mapMode').value,group_col:body.querySelector('#mapGroup').value,file_biz:body.querySelector('#mapBiz').value,biz_col:body.querySelector('#mapBizCol').value,biz_values:bizValues,cols:mcols,date_format:'auto'};
      st.busy = true; const b = body.querySelector('#mapApply'); b.disabled = true; b.textContent = '正在检查…';
      try { const r = await api('/api/sheet/transform',{method:'POST',body:{ref:st.ref,sheet:st.sheet,mapping:st.mapping}}); if (!r.ok) throw new Error(r.message || '数据变换失败'); st.result = r.result; st.repair = null; renderRows(); }
      catch (e) { fail(e); b.disabled = false; b.textContent = '重新检查'; }
      finally { st.busy = false; }
    };
  }
  function problemAfter(row,fix) {
    const d = row.draft || {}, list = row.problems || [];
    if (fix.field === 'party' && d.party) return list.filter(x => !/往来单位.*含数字/.test(x));
    if (fix.field === 'item_name' && (d.items || []).every(i => String(i.name || '').trim())) return list.filter(x => !/有一行商品没有名称|商品名为空/.test(x));
    return list;
  }
  function repairText() {
    const r = st.repair || {}; if (r.skipped) return r.reason ? '<div class="hint">' + VALUE(r.reason) + '</div>':'';
    const applied = (r.applied || []).map(f => '已修正第 ' + (f.row_index + 1) + ' 行：' + VALUE(f.value)).join('<br>');
    const suggestions = (r.suggestions || []).map((f,i) => '<div class="hint" style="margin:6px 0">建议将“' + VALUE(f.old || '空') + '”改为“' + VALUE(f.value) + '” <button class="btn btn-sm" data-adopt="' + i + '"' + (f._adopted ? ' disabled':'') + '>' + (f._adopted ? '已采纳':'采纳') + '</button></div>').join('');
    return (applied || suggestions) ? '<details class="check-trace" open><summary>智能修复 · ' + (r.applied || []).length + ' 项已应用</summary>' + applied + suggestions + '</details>':'';
  }
  function applyRepairCleanup() {
    // map 自带修复环时，applied 字段已写回 result；行内 problems 需同步剔除
    // 已解除的条目，否则已修好的行仍显示「待处理」（与 bill.html 同口径）。
    ((st.repair || {}).applied || []).forEach(f => {
      const d = ((st.result || {}).drafts || [])[f.row_index];
      if (d) d.problems = problemAfter(d, f);
    });
  }
  function renderRows() {
    step(2); applyRepairCleanup(); const ds = ((st.result || {}).drafts || []).map((x,i) => Object.assign({},x,{_index:i})), errors = ((st.result || {}).row_errors || []).map(x => ({row_no:x.row_no,draft:null,problems:x.errors || [],_index:-1}));
    const rows = ds.concat(errors).sort((a,b) => a.row_no - b.row_no), bad = rows.filter(x => (x.problems || []).length).length;
    body.innerHTML = '<div class="imp-summary"><span>共 <b class="num">' + rows.length + '</b> 行</span><span class="row-ok">可起草 <b class="num">' + ds.filter(x => !(x.problems || []).length).length + '</b></span><span class="row-warn">待处理 <b class="num">' + bad + '</b></span></div>' + (bad ? '<button class="btn btn-sm btn-ghost" id="repairRows">智能修复待处理行</button>':'') + '<div id="repairInfo">' + repairText() + '</div><div class="tbl-wrap tbl-scroll" style="max-height:42vh"><table class="row-table"><thead><tr><th>行</th><th>状态</th><th>类型 / 往来单位</th><th>明细</th><th>需处理</th></tr></thead><tbody>' + (rows.map(r => { const d = r.draft || {}, items = (d.items || []).map(i => VALUE(i.name) + ' × ' + i.qty + VALUE(i.unit || '')).join('；'); return '<tr><td>' + r.row_no + '</td><td>' + ((r.problems || []).length ? '<span class="row-bad">待处理</span>':'<span class="row-ok">可起草</span>') + '</td><td>' + VALUE((BIZ[d.biz] || {}).label || '—') + '<br><small>' + VALUE(d.party || '—') + '</small></td><td>' + (items || '—') + '</td><td class="row-bad">' + VALUE((r.problems || []).slice(0,2).join('；')) + '</td></tr>'; }).join('') || '<tr><td colspan="5" class="center">没有读到业务行</td></tr>') + '</tbody></table></div><div class="row-btns"><button class="btn btn-ghost" id="rowsBack">返回字段</button><button class="btn btn-primary" id="rowsDone">生成 ' + ds.filter(x => !(x.problems || []).length).length + ' 张草稿 →</button></div>';
    const rb = body.querySelector('#repairRows'); if (rb) rb.onclick = repairRows;
    body.querySelector('#rowsBack').onclick = () => renderMapping('');
    body.querySelector('#rowsDone').onclick = resolve;
    body.querySelectorAll('[data-adopt]').forEach(b => b.onclick = () => adopt(+b.dataset.adopt,b));
  }
  async function repairRows() {
    if (st.busy) return; st.busy = true; const b = body.querySelector('#repairRows'); b.disabled = true; b.textContent = '正在查证并修复…';
    try { const r = await api('/api/sheet/repair',{method:'POST',body:{ref:st.ref,sheet:st.sheet,mapping:st.mapping}}); if (!r.ok) throw new Error(r.message || '修复未完成'); st.result = r.result; st.repair = r.repair || {}; renderRows(); }
    catch (e) { fail(e); b.disabled = false; b.textContent = '重试智能修复'; }
    finally { st.busy = false; }
  }
  async function adopt(i,btn) {
    const f = ((st.repair || {}).suggestions || [])[i], row = ((st.result || {}).drafts || [])[f && f.row_index]; if (!f || !row) return;
    if (f.field === 'party') row.draft.party = String(f.value || '');
    else { const item = (row.draft.items || [])[+(f.item_index || 0)]; if (!item) return; item.name = String(f.value || ''); if (f.material_id) item.material_id = f.material_id; }
    f.old = f.old || (f.field === 'party' ? row.draft.party : item.name); f._adopted = true; row.problems = problemAfter(row,f); btn.disabled = true; renderRows();
  }
  async function resolve() {
    if (st.busy) return; const ready = ((st.result || {}).drafts || []).filter(x => !(x.problems || []).length);
    if (!ready.length) { fail('当前没有可起草的行，请修正字段或处理标出的数据'); return; }
    st.busy = true; const b = body.querySelector('#rowsDone'); b.disabled = true; b.textContent = '正在匹配档案…';
    try { const r = await api('/api/sheet/resolve',{method:'POST',body:{drafts:ready.map(x => ({row_no:x.row_no,draft:x.draft}))}}); if (!r.ok) throw new Error(r.message || '草稿生成失败'); st.closed = true; if (detachTrap) detachTrap(); mask.remove(); if (options.onDrafts) options.onDrafts({batch_id:r.batch_id || '',bills:r.bills || [],raw_text:'表格导入 ' + st.sheet,meta:{source:'import'}}); }
    catch (e) { fail(e); b.disabled = false; b.textContent = '重试生成草稿'; }
    finally { st.busy = false; }
  }
  choose();
}
window.ImportDialog = {open};
})();
