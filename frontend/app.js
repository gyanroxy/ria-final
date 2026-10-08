/* ============================================================
   RIA — invoice calling agent dashboard
   Talks to backend/server.py: /api/session, /api/calls, /api/settings, /api/dialer
   ============================================================ */

const S = {screen:'dashboard', calls:[], dialer:{}, settings:null, auth:null,
  period:'today', tab:'all', q:'', ptp:'open', imp:null, company:''};

const NAV = [
  {k:'dashboard', t:'Dashboard',       ic:'⌂'},
  {k:'calls',     t:'Calls',           ic:'☏'},
  {k:'ptp',       t:'Promises to pay', ic:'₹'},
  {k:'settings',  t:'Settings',        ic:'⚙'}
];
const TITLES = {
  dashboard:['Dashboard','How the invoice calls are going.'],
  calls:['Calls','Import the invoice list, then press Start calling. RIA calls the customers one after another, in the order of your list.'],
  ptp:['Promises to pay','Every promise-to-pay date RIA captured, in one column.'],
  settings:['Settings','What RIA runs on. Change these in backend/.env and restart the backend.']
};

/* outcome → [label, colour, chip class] */
const OUTCOMES = {
  'PROMISE-TO-PAY':['Promise to pay','#157F4C','c-green'],
  'ALREADY-PAID':  ['Already paid','#12A9B8','c-teal'],
  'CALLBACK':      ['Call back','#1E52C9','c-blue'],
  'DISPUTE':       ['Dispute','#B07C00','c-gold'],
  'REQUEST':       ['Wants statement / copy','#7A4BC4','c-blue'],
  'REFUSED':       ['Refused','#C8102E','c-red'],
  'OPT-OUT':       ['Opted out','#2B3852','c-grey'],
  'WRONG-PERSON':  ['Wrong person','#53617C','c-grey'],
  'NO-ANSWER':     ['Not lifted','#8593AD','c-grey'],
  'BUSY':          ['Busy','#B9C4D6','c-grey'],
  'DISCONNECTED':  ['Hung up','#AEBBD3','c-grey'],
  'FAILED':        ['Call failed','#E07A8B','c-red'],
  'CANCELLED':     ['Cancelled','#D5DCE8','c-grey']
};
const ACTIVE = ['queued','dialing','live'];
const NOT_REACHED = ['NO-ANSWER','BUSY','FAILED'];
const LANGS = {te:'Telugu', hi:'Hindi', en:'English', telugu:'Telugu', hindi:'Hindi', english:'English'};

/* ---------- helpers ---------- */
const $ = s => document.querySelector(s);
const esc = s => String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const chip = (t, cls) => '<span class="chip '+cls+'">'+esc(t)+'</span>';
function inr(n){
  const s = Math.round(Math.abs(n||0)).toString();
  let last3 = s.slice(-3), rest = s.slice(0,-3);
  if(rest) last3 = ',' + last3;
  rest = rest.replace(/\B(?=(\d{2})+(?!\d))/g, ',');
  return '₹' + (n<0?'-':'') + rest + last3;
}
function showToast(m){
  const t = document.createElement('div'); t.className='toast'; t.textContent=m;
  $('#toastHost').appendChild(t);
  setTimeout(()=>t.remove(), 3200);
}

/* Dates: the database stores UTC timestamps; calls and promises are in India time */
const IST = 'Asia/Kolkata';
const isoIST = d => new Intl.DateTimeFormat('en-CA',{timeZone:IST}).format(d);
const todayISO = () => isoIST(new Date());
const addDays = (iso, n) => { const d=new Date(iso+'T00:00:00Z'); d.setUTCDate(d.getUTCDate()+n); return d.toISOString().slice(0,10); };
const ts = s => new Date(String(s).replace(' ','T')+'Z');
const dayOf = s => s ? isoIST(ts(s)) : '';
const fmtTime = s => s ? ts(s).toLocaleString('en-IN',{timeZone:IST, day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit', hour12:false}) : '—';
const fmtDate = (iso, weekday=true) => iso ? new Date(iso+'T00:00:00Z').toLocaleDateString('en-IN',{timeZone:'UTC', weekday:weekday?'short':undefined, day:'2-digit', month:'short', year:'numeric'}) : '—';
function duration(c){
  if(!c.answered_at || !c.ended_at) return '—';
  const s = Math.max(0, Math.round((ts(c.ended_at)-ts(c.answered_at))/1000));
  return Math.floor(s/60)+':'+String(s%60).padStart(2,'0');
}
const promised = c => c.promised_amount || c.amount;

/* ---------- API ---------- */
async function api(path, body){
  const opts = body===undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)};
  const res = await fetch(path, opts);
  const data = await res.json().catch(()=>({}));
  if(res.status===401 && path!=='api/login'){ showLogin(); throw new Error(data.error||'Sign in first'); }
  if(!res.ok) throw new Error(data.error || 'Request failed ('+res.status+')');
  return data;
}

/* ---------- sign in ---------- */
async function boot(){
  try { S.auth = await api('api/session'); }
  catch(e){ document.body.innerHTML = '<div class="empty"><h3>The RIA backend is not reachable</h3><p>Start it with <span class="mono">python backend/start.py</span> and reload.</p></div>'; return; }
  S.auth.signed_in ? startApp() : showLogin();
}
function showLogin(){
  stopPolling();
  $('#app').classList.add('hide'); $('#login').classList.remove('hide');
  const noPassword = S.auth && !S.auth.password_required;
  $('#loginFields').classList.toggle('hide', noPassword);
  $('#loginSub').textContent = noPassword
    ? 'This dashboard has no password yet, so it only opens on the server itself. Set DASHBOARD_PASSWORD in backend/.env and restart the backend to open it from here.'
    : 'Enter the dashboard password.';
  if(!noPassword) $('#lgPass').focus();
}
async function doLogin(){
  $('#loginErr').classList.add('hide');
  try { await api('api/login', {password:$('#lgPass').value}); S.auth.signed_in = true; $('#lgPass').value=''; startApp(); }
  catch(e){ $('#loginErrText').textContent = e.message; $('#loginErr').classList.remove('hide'); }
}
async function signOut(){ await api('api/logout', {}).catch(()=>{}); S.auth.signed_in=false; showLogin(); }

async function startApp(){
  $('#login').classList.add('hide'); $('#app').classList.remove('hide');
  try { S.settings = await api('api/settings'); S.company = S.company || S.settings.company; } catch(e){ showToast(e.message); }
  await refresh();
  render();
  schedulePoll();
}

/* ---------- live updates ---------- */
let pollTimer = null;
function stopPolling(){ clearTimeout(pollTimer); pollTimer = null; }
function schedulePoll(){
  stopPolling();
  const busy = S.calls.some(c=>ACTIVE.includes(c.status));
  pollTimer = setTimeout(async ()=>{
    if(!document.hidden){ await refresh(); softRender(); }
    schedulePoll();
  }, busy ? 3000 : 10000);
}
async function refresh(){
  try { const d = await api('api/calls'); S.calls = d.calls; S.dialer = d.dialer; }
  catch(e){ /* keep the last data; a sign-in prompt is already shown on 401 */ }
}
/* Re-render without stealing focus from a field the user is typing in */
function softRender(){
  if($('#modalHost').innerHTML) return;
  const f = document.activeElement;
  if(f && /INPUT|SELECT|TEXTAREA/.test(f.tagName) && $('#main').contains(f)){
    const body = $('#callsBody'); if(body) body.innerHTML = callRows();
    return;
  }
  render();
}

/* ---------- shell ---------- */
function go(k){ S.screen = k; history.replaceState(null, '', '#'+k); window.scrollTo(0,0); render(); }
function render(){
  renderNav();
  const t = TITLES[S.screen];
  $('#main').innerHTML = '<div class="page-head"><div><h1>'+t[0]+'</h1><p>'+t[1]+'</p></div>'+
    '<div class="head-right">'+headRight()+'</div></div>' + SCREENS[S.screen]();
}
function dialerChip(){
  const d = S.dialer || {};
  if(!d.configured) return chip('Vobiz trunk not set up','c-red');
  if(!d.running) return chip('Dialer stopped · restart the backend','c-red');
  if(d.error) return '<span class="chip c-red" title="'+esc(d.error)+'">Dialer error · retrying</span>';
  if(d.paused) return S.calls.some(c=>c.status==='queued') ? chip('Not calling · press Start calling','c-gold') : chip('Not calling','c-grey');
  if(!d.in_calling_hours) return chip('Started · waiting for calling hours '+d.calling_hours,'c-gold');
  if(d.active) return '<span class="chip c-green live">Calling · '+d.active+' on call</span>';
  return chip('Calling','c-green');
}
function headRight(){
  if(S.screen==='dashboard') return '<div class="toggle">'+[['today','Today'],['week','7 days'],['all','All time']].map(p=>
    '<button class="'+(S.period===p[0]?'on':'')+'" onclick="S.period=\''+p[0]+'\';render()">'+p[1]+'</button>').join('')+'</div>';
  if(S.screen==='calls') return startStopButton()+'<button class="btn btn-ghost btn-sm" onclick="callOneModal()">+ Add one customer</button>';
  return '';
}
function startStopButton(){
  const d = S.dialer || {}, queued = S.calls.filter(c=>c.status==='queued').length;
  if(d.configured && !d.paused) return '<button class="btn btn-red btn-sm" onclick="setPaused(true)">■ Stop calling</button>';
  const off = !d.configured || !d.running || !queued;
  return '<button class="btn btn-green btn-sm" onclick="setPaused(false)"'+(off?' disabled':'')+
    ' title="'+(off?(!d.configured?'Set up the Vobiz trunk first':!d.running?'The dialer is not running':'Import a list first'):'Calls everyone in the list, one after another')+'">▶ Start calling'+(queued?' ('+queued+')':'')+'</button>';
}
function renderNav(){
  const ptpDue = latestPromises().filter(c=>c.promised_date===todayISO()).length;
  $('#nav').innerHTML = NAV.map(n=>
    '<a class="'+(S.screen===n.k?'on':'')+'" onclick="go(\''+n.k+'\')" tabindex="0" onkeydown="if(event.key===\'Enter\')go(\''+n.k+'\')">'+
      '<span class="ic">'+n.ic+'</span><span>'+n.t+'</span>'+
      (n.k==='ptp'&&ptpDue?'<span class="chip c-gold" style="margin-left:auto" title="Promises due today">'+ptpDue+'</span>':'')+
    '</a>').join('');
  $('#navSel').innerHTML = NAV.map(n=>'<option value="'+n.k+'"'+(S.screen===n.k?' selected':'')+'>'+n.t+'</option>').join('');
  $('#topRight').innerHTML = dialerChip() +
    (S.auth && S.auth.password_required ? '<button class="btn btn-ghost btn-sm" onclick="signOut()">Sign out</button>' : '');
  const queued = S.calls.filter(c=>c.status==='queued').length;
  $('#sideFoot').innerHTML = '<b>'+queued+'</b> waiting to be called.<br>Calls go out '+esc(S.dialer.calling_hours||'')+', '+(S.dialer.concurrency===1?'one after another':S.dialer.concurrency+' at a time')+'.';
}

/* ---------- shared bits ---------- */
function stateChip(c){
  if(c.status==='queued') return chip('Queued','c-blue');
  if(c.status==='dialing') return '<span class="chip c-gold live">Ringing</span>';
  if(c.status==='live') return '<span class="chip c-green live">On call</span>';
  const o = OUTCOMES[c.outcome] || [c.outcome||'Done','','c-grey'];
  return chip(o[0], o[2]);
}
function customerCell(c){
  return '<div class="name">'+esc(c.customer_name)+'</div><div class="meta">'+esc(c.phone)+(c.business?' · '+esc(c.business):'')+'</div>';
}
function invoiceCell(c){
  return '<div class="num">'+esc(c.invoice_no)+'</div><div class="meta">'+inr(c.amount)+' · due '+esc(fmtDate(c.due_date,false))+'</div>';
}
function empty(title, text, cta){
  return '<div class="card"><div class="empty"><h3>'+title+'</h3><p>'+text+'</p>'+(cta||'')+'</div></div>';
}
/* Latest promise per invoice, so a repeat call doesn't count twice */
function latestPromises(){
  const seen = new Set();
  return S.calls.filter(c=>{
    if(c.outcome!=='PROMISE-TO-PAY' || !c.promised_date) return false;
    const k = c.phone+'|'+c.invoice_no;
    if(seen.has(k)) return false;
    seen.add(k); return true;
  });
}
function toCSV(rows){
  return rows.map(r=>r.map(v=>{ const s=String(v==null?'':v); return /[",\r\n]/.test(s) ? '"'+s.replace(/"/g,'""')+'"' : s; }).join(',')).join('\r\n');
}
/* BOM so Excel reads ₹ and Indic names correctly */
function downloadCSV(name, rows){
  const url = URL.createObjectURL(new Blob(['﻿'+toCSV(rows)], {type:'text/csv;charset=utf-8'}));
  const a = document.createElement('a'); a.href=url; a.download=name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(url), 1500);
}
function exportCalls(name, list){
  downloadCSV(name, [['Customer','Business','Phone','Invoice','Amount (INR)','Due date','Status','Outcome','PTP date','Promised amount (INR)','Notes','Language','Queued at','Answered at','Ended at'],
    ...list.map(c=>[c.customer_name,c.business,c.phone,c.invoice_no,c.amount,c.due_date,c.status,c.outcome?OUTCOMES[c.outcome]?OUTCOMES[c.outcome][0]:c.outcome:'',
      c.promised_date||'',c.outcome==='PROMISE-TO-PAY'?promised(c):'',c.notes,LANGS[c.language]||c.language,fmtTime(c.created_at),fmtTime(c.answered_at),fmtTime(c.ended_at)])]);
}
function openModal(title, body, foot){
  $('#modalHost').innerHTML = '<div class="modal-bg" onclick="if(event.target===this)closeModal()"><div class="modal" role="dialog" aria-modal="true" aria-label="'+esc(title)+'">'+
    '<div class="m-h"><h3>'+esc(title)+'</h3></div><div class="m-b">'+body+'</div><div class="m-f">'+foot+'</div></div></div>';
  const f = $('#modalHost input, #modalHost select, #modalHost button'); if(f) f.focus();
}
function closeModal(){ $('#modalHost').innerHTML=''; }

/* ============================================================
   Dashboard
   ============================================================ */
function inPeriod(list){
  if(S.period==='all') return list;
  const from = S.period==='today' ? todayISO() : addDays(todayISO(), -6);
  return list.filter(c=>dayOf(c.created_at) >= from);
}
function scrDashboard(){
  if(!S.calls.length) return empty('No calls yet','Import your invoice list and RIA starts calling customers inside calling hours.',
    '<button class="btn btn-primary btn-sm" onclick="go(\'calls\')">Import invoices</button>');
  const list = inPeriod(S.calls);
  const dialled = list.filter(c=>!['queued','cancelled'].includes(c.status));
  const answered = list.filter(c=>c.answered_at);
  const ptps = list.filter(c=>c.outcome==='PROMISE-TO-PAY');
  const notReached = list.filter(c=>NOT_REACHED.includes(c.outcome));
  const active = list.filter(c=>ACTIVE.includes(c.status));
  const tiles = [
    ['navy', list.length, 'Calls', dialled.length+' dialled · '+active.length+' waiting or on call', 'calls'],
    ['teal', dialled.length ? Math.round(answered.length/dialled.length*100)+'%' : '—', 'Picked up', answered.length+' of '+dialled.length+' dialled', 'calls'],
    ['gold', ptps.length, 'Promises to pay', inr(ptps.reduce((a,c)=>a+promised(c),0))+' promised', 'ptp'],
    ['red', notReached.length, 'Not reached', 'not lifted, busy or failed', 'calls']
  ].map(t=>'<div class="tile '+t[0]+'" onclick="go(\''+t[4]+'\')" tabindex="0" onkeydown="if(event.key===\'Enter\')go(\''+t[4]+'\')"><div class="n">'+t[1]+'</div><div class="l">'+t[2]+'</div><div class="d">'+t[3]+'</div></div>').join('');

  const done = list.filter(c=>c.status==='done' && c.outcome!=='CANCELLED');
  const counts = {}; done.forEach(c=>counts[c.outcome]=(counts[c.outcome]||0)+1);
  const rows = Object.keys(OUTCOMES).filter(k=>counts[k]);
  const max = Math.max(1, ...rows.map(k=>counts[k]));
  const bars = rows.length ? rows.map(k=>'<div class="hb"><div class="hb-l"><span><i style="background:'+OUTCOMES[k][1]+'"></i>'+OUTCOMES[k][0]+'</span>'+
      '<span class="num">'+counts[k]+'<span class="pct">'+Math.round(counts[k]/done.length*100)+'%</span></span></div>'+
      '<div class="bar"><i style="width:'+(counts[k]/max*100)+'%;background:'+OUTCOMES[k][1]+'"></i></div></div>').join('')
    : '<p class="meta">No finished calls in this period.</p>';

  const today = todayISO();
  const upcoming = latestPromises().filter(c=>c.promised_date>=today).sort((a,b)=>a.promised_date.localeCompare(b.promised_date)).slice(0,8);
  const ptpList = upcoming.length ? upcoming.map(c=>'<div class="list-row" onclick="go(\'ptp\')"><div><div class="name">'+esc(c.customer_name)+'</div>'+
      '<div class="meta">'+esc(c.invoice_no)+' · '+inr(promised(c))+'</div></div>'+
      '<div class="rr">'+chip(c.promised_date===today?'Today':fmtDate(c.promised_date), c.promised_date===today?'c-gold':'c-green')+'</div></div>').join('')
    : '<div class="card-b"><p class="meta" style="margin:0">No upcoming promise-to-pay dates.</p></div>';

  return '<div class="grid g4" style="margin-bottom:16px">'+tiles+'</div>'+
    '<div class="grid g2">'+
      '<div class="card"><div class="card-h"><h3>How customers responded</h3><span class="sub">'+done.length+' finished calls</span></div><div class="card-b">'+bars+'</div></div>'+
      '<div class="card"><div class="card-h"><h3>Coming promise-to-pay dates</h3>'+
        '<div class="r"><button class="btn btn-ghost btn-sm" onclick="go(\'ptp\')">All promises →</button></div></div>'+ptpList+'</div>'+
    '</div>';
}

/* ============================================================
   Calls — import the invoice list, follow every call
   ============================================================ */
const CSV_ALIASES = {
  name:['name','customer','customer_name','retailer','retailer_name','party','party_name','contact_name'],
  phone:['phone','mobile','phone_number','mobile_number','mobile_no','phone_no','contact','number'],
  business:['business','business_name','shop','shop_name','firm','firm_name'],
  invoice_no:['invoice_no','invoice','invoice_number','inv_no','bill_no','bill_number','voucher_no'],
  amount:['amount','outstanding','due_amount','balance','pending_amount','invoice_amount'],
  due_date:['due_date','due','date'],
  language:['language','lang','preferred_language']
};
const CALL_TABS = [
  ['all','All',()=>true],
  ['active','In progress',c=>ACTIVE.includes(c.status)],
  ['ptp','Promise to pay',c=>c.outcome==='PROMISE-TO-PAY'],
  ['answered','Other answers',c=>c.answered_at && c.outcome!=='PROMISE-TO-PAY' && !ACTIVE.includes(c.status)],
  ['missed','Not reached',c=>NOT_REACHED.includes(c.outcome)]
];

function scrCalls(){
  const tabs = CALL_TABS.map(t=>'<button role="tab" aria-selected="'+(S.tab===t[0])+'" class="'+(S.tab===t[0]?'on':'')+'" onclick="S.tab=\''+t[0]+'\';render()">'+
    t[1]+'<span class="ct">'+S.calls.filter(t[2]).length+'</span></button>').join('');
  return importCard()+
    '<div class="card"><div class="tabs" role="tablist">'+tabs+'</div>'+
      '<div class="filters"><input type="text" class="search-in" aria-label="Search calls" placeholder="Search name, phone or invoice" value="'+esc(S.q)+'" '+
        'oninput="S.q=this.value;$(\'#callsBody\').innerHTML=callRows()">'+
        '<div class="sp"><button class="btn btn-ghost btn-sm" onclick="exportCalls(\'ria-calls-\'+todayISO()+\'.csv\', filteredCalls())">⤓ Download CSV</button></div></div>'+
      '<div class="tbl-wrap"><table><thead><tr><th>Customer</th><th>Invoice</th><th>Status</th><th class="ptp-col">PTP date</th><th class="right">Promised</th><th>Notes</th><th>Called</th><th class="right">Length</th><th></th></tr></thead>'+
      '<tbody id="callsBody">'+callRows()+'</tbody></table></div></div>';
}
function filteredCalls(){
  const tab = CALL_TABS.find(t=>t[0]===S.tab) || CALL_TABS[0];
  const q = S.q.trim().toLowerCase();
  return S.calls.filter(tab[2]).filter(c=>!q || [c.customer_name,c.phone,c.invoice_no,c.business].join(' ').toLowerCase().includes(q));
}
function callRows(){
  const list = filteredCalls();
  if(!list.length) return '<tr><td colspan="9" style="text-align:center;color:var(--t3);padding:24px;cursor:default">'+
    (S.calls.length ? 'No calls match.' : 'No calls yet. Import your invoice list above.')+'</td></tr>';
  const shown = list.slice(0, 300);
  return shown.map(c=>'<tr style="cursor:default">'+
    '<td>'+customerCell(c)+'</td><td>'+invoiceCell(c)+'</td><td>'+stateChip(c)+'</td>'+
    '<td class="num ptp-col nowrap">'+(c.promised_date?esc(fmtDate(c.promised_date)):'—')+'</td>'+
    '<td class="num right">'+(c.outcome==='PROMISE-TO-PAY'?inr(promised(c)):'—')+'</td>'+
    '<td style="font-size:12.5px;max-width:260px">'+esc(c.notes||'')+'</td>'+
    '<td class="num nowrap" style="font-size:12px">'+fmtTime(c.answered_at||c.created_at)+'</td>'+
    '<td class="num right">'+duration(c)+'</td>'+
    '<td class="right">'+
      (c.status==='queued'?'<button class="btn btn-ghost btn-sm" onclick="cancelCall(\''+esc(c.call_id)+'\')">Cancel</button>':
       c.status==='done'&&c.outcome!=='OPT-OUT'?'<button class="btn btn-ghost btn-sm" onclick="retryCall(\''+esc(c.call_id)+'\')">Call again</button>':'')+
    '</td></tr>').join('') +
    (list.length>shown.length?'<tr><td colspan="9" class="meta" style="cursor:default">Showing 300 of '+list.length+'. Download the CSV for all of them.</td></tr>':'');
}
async function cancelCall(id){
  try { await api('api/calls/'+id+'/cancel', {}); showToast('Call cancelled.'); await refresh(); render(); }
  catch(e){ showToast(e.message); }
}
async function retryCall(id){
  try { await api('api/calls/'+id+'/retry', {}); showToast(S.dialer.paused ? 'Added. Press Start calling to call again.' : 'Added to the end of the call list.'); await refresh(); render(); schedulePoll(); }
  catch(e){ showToast(e.message); }
}

function importCard(){
  const head = '<div class="card" style="margin-bottom:14px"><div class="card-h"><h3>Import invoices to call</h3><span class="sub">.csv · one row per invoice</span>'+
    '<div class="r"><button class="btn btn-ghost btn-sm" onclick="dlTemplate()">⤓ Template</button></div></div><div class="card-b">';
  if(S.imp) return head+importPreview(S.imp)+'</div></div>';
  const cols = [['name',1,'Customer name RIA asks for'],['phone',1,'10-digit mobile, +91 optional'],['invoice_no',1,'Invoice or bill number'],
    ['amount',1,'Amount pending, in rupees'],['due_date',1,'DD-MM-YYYY'],['business',0,'Shop or firm name'],['language',0,'Telugu, Hindi or English · RIA then follows the customer']];
  return head+
    '<div class="imp-grid">'+
      '<div><div class="drop" tabindex="0" role="button" aria-label="Choose a CSV invoice list" onclick="$(\'#csvIn\').click()" '+
        'onkeydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();$(\'#csvIn\').click()}" '+
        'ondragover="event.preventDefault();this.classList.add(\'over\')" ondragleave="this.classList.remove(\'over\')" ondrop="dropCSV(event)">'+
        '<div class="big">⇪</div><div style="font-weight:700;margin-top:6px">Drop the invoice list here, or click to choose</div>'+
        '<div class="meta" style="margin-top:4px">CSV up to 5,000 rows · exported from Excel, Tally or your ERP</div></div>'+
        '<input id="csvIn" type="file" accept=".csv,text/csv" class="hide" onchange="readCSVFile(this.files[0]);this.value=\'\'">'+
        '<div class="field" style="margin:14px 0 0"><label for="company">RIA calls on behalf of</label>'+
          '<input id="company" type="text" value="'+esc(S.company)+'" placeholder="Your company name" oninput="S.company=this.value"></div></div>'+
      '<div><p class="sec-t b" style="margin:0 0 10px;font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;font-weight:800;color:var(--navy)">Columns</p><div class="cols">'+
        cols.map(c=>'<div><span class="mono">'+c[0]+'</span>'+(c[1]?chip('required','c-red'):'')+'<div class="meta">'+c[2]+'</div></div>').join('')+'</div></div>'+
    '</div></div></div>';
}
function parseCSV(text){
  const rows=[]; let row=[], f='', q=false;
  for(let i=0;i<text.length;i++){
    const c=text[i];
    if(q){ if(c==='"'){ if(text[i+1]==='"'){ f+='"'; i++; } else q=false; } else f+=c; }
    else if(c==='"') q=true;
    else if(c===','){ row.push(f); f=''; }
    else if(c==='\n'||c==='\r'){ if(c==='\r'&&text[i+1]==='\n') i++; row.push(f); rows.push(row); row=[]; f=''; }
    else f+=c;
  }
  if(f!==''||row.length){ row.push(f); rows.push(row); }
  return rows.filter(r=>r.some(v=>v.trim()!==''));
}
function dropCSV(e){ e.preventDefault(); e.currentTarget.classList.remove('over'); readCSVFile(e.dataTransfer.files[0]); }
function readCSVFile(file){
  if(!file) return;
  if(!/\.csv$/i.test(file.name) && file.type!=='text/csv'){ showToast('Choose a .csv file. In Excel use File → Save As → CSV UTF-8.'); return; }
  if(file.size > 5*1024*1024){ showToast('That file is over 5 MB. Split it into smaller lists.'); return; }
  file.text().then(t=>ingestCSV(file.name, t)).catch(()=>showToast('That file could not be read.'));
}
function parseDate(s){
  let m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/), y, mo, d;
  if(m){ [y,mo,d] = [+m[1],+m[2],+m[3]]; }
  else if((m = s.match(/^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$/))){ [d,mo,y] = [+m[1],+m[2],+m[3]]; }
  else return '';
  const dt = new Date(Date.UTC(y, mo-1, d));
  return dt.getUTCMonth()===mo-1 && dt.getUTCDate()===d ? dt.toISOString().slice(0,10) : '';
}
/* Mirrors backend dialer.parse_invoice so problems show before anything is queued */
function checkRow(o){
  const errs = [];
  ['name','phone','invoice_no','amount','due_date'].forEach(k=>{ if(!o[k]) errs.push(k+' missing'); });
  let d = o.phone.replace(/\D/g,'');
  if(d.length===12 && d.startsWith('91')) d=d.slice(2); else if(d.length===11 && d.startsWith('0')) d=d.slice(1);
  if(o.phone && !/^[6-9]\d{9}$/.test(d)) errs.push('Invalid mobile');
  const amt = o.amount.replace(/^(rs\.?|inr)/i,'').replace(/[₹,\s]/g,'');
  if(o.amount && !(+amt>0)) errs.push('Amount not a number');
  const due = o.due_date ? parseDate(o.due_date) : '';
  if(o.due_date && !due) errs.push('Bad due date');
  if(o.language && !LANGS[o.language.toLowerCase()]) errs.push('Language not Telugu/Hindi/English');
  return Object.assign(o, {phone10:d, amt:+amt||0, due, errs});
}
function ingestCSV(name, text){
  const grid = parseCSV(text.replace(/^﻿/,''));
  if(grid.length<2){ S.imp={file:name, error:'The file has a header but no data rows.'}; render(); return; }
  const hdr = grid[0].map(h=>h.trim().toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_|_$/g,''));
  const col = {};
  Object.entries(CSV_ALIASES).forEach(([k,al])=>{ const i=hdr.findIndex(h=>al.includes(h)); if(i>-1) col[k]=i; });
  const missing = ['name','phone','invoice_no','amount','due_date'].filter(k=>col[k]===undefined);
  if(missing.length){ S.imp={file:name, error:'Missing column'+(missing.length>1?'s':'')+': '+missing.join(', ')+'. Columns found: '+grid[0].join(', ')}; render(); return; }
  if(grid.length>5001){ S.imp={file:name, error:'The file has '+(grid.length-1).toLocaleString('en-IN')+' rows. The limit is 5,000 per import.'}; render(); return; }
  const seen = new Set();
  const rows = grid.slice(1).map((r,i)=>{
    const o = {line:i+2};
    Object.keys(CSV_ALIASES).forEach(k=>o[k] = col[k]===undefined ? '' : (r[col[k]]||'').trim());
    checkRow(o);
    const key = o.phone10+'|'+o.invoice_no;
    if(!o.errs.length){ if(seen.has(key)) o.errs.push('Duplicate invoice'); else seen.add(key); }
    return o;
  });
  S.imp = {file:name, rows};
  render();
}
function importPreview(imp){
  if(imp.error) return '<div class="msg msg-err"><span>⚠</span><span><b>'+esc(imp.file)+'</b> — '+esc(imp.error)+'</span></div>'+
    '<button class="btn btn-ghost btn-sm" onclick="S.imp=null;render()">Choose another file</button>';
  const ok = imp.rows.filter(r=>!r.errs.length), bad = imp.rows.length-ok.length;
  const shown = imp.rows.slice().sort((a,b)=>b.errs.length-a.errs.length || a.line-b.line).slice(0,60);
  return '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px">'+chip(imp.file,'c-blue')+
      '<span class="meta">Nothing is called until you press Start calling</span></div>'+
    '<div class="ptp-sum" style="padding:0;margin-bottom:14px;border:0">'+
      '<div><span class="num">'+imp.rows.length+'</span><span>rows read</span></div>'+
      '<div><span class="num" style="color:var(--green)">'+ok.length+'</span><span>ready to call</span></div>'+
      '<div><span class="num" style="color:'+(bad?'var(--red)':'var(--t3)')+'">'+bad+'</span><span>rejected</span></div>'+
      '<div><span class="num">'+inr(ok.reduce((a,r)=>a+r.amt,0))+'</span><span>pending in list</span></div>'+
    '</div>'+
    '<div class="tbl-wrap" style="max-height:320px;border:1px solid var(--line-2);border-radius:8px"><table><thead><tr><th>Row</th><th>Customer</th><th>Phone</th><th>Invoice</th><th class="right">Amount</th><th>Due</th><th>Language</th><th>Check</th></tr></thead><tbody>'+
    shown.map(r=>'<tr style="cursor:default'+(r.errs.length?';background:#FFF8F9':'')+'"><td class="num">'+r.line+'</td><td>'+esc(r.name||'—')+'</td>'+
      '<td class="num">'+esc(r.phone||'—')+'</td><td>'+esc(r.invoice_no||'—')+'</td>'+
      '<td class="num right">'+(r.amt?inr(r.amt):esc(r.amount||'—'))+'</td><td class="num">'+esc(r.due?fmtDate(r.due):r.due_date||'—')+'</td>'+
      '<td>'+esc(LANGS[r.language.toLowerCase()]||r.language||'Default')+'</td>'+
      '<td>'+(r.errs.length?chip(r.errs.join(' · '),'c-red'):chip('OK','c-green'))+'</td></tr>').join('')+
    '</tbody></table></div>'+
    (imp.rows.length>shown.length?'<div class="meta" style="margin-top:6px">Showing '+shown.length+' of '+imp.rows.length+' rows, problems first.</div>':'')+
    '<div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap;align-items:center">'+
      '<button class="btn btn-primary btn-sm" id="callAllBtn" onclick="callImported()"'+(ok.length?'':' disabled')+'>Add '+ok.length+' customer'+(ok.length===1?'':'s')+' to the call list</button>'+
      (bad?'<button class="btn btn-ghost btn-sm" onclick="dlRejects()">⤓ Download '+bad+' rejected rows</button>':'')+
      '<button class="btn btn-ghost btn-sm" onclick="S.imp=null;render()">Discard</button>'+
      '<span class="meta" style="margin-left:auto">Calls go out '+esc(S.dialer.calling_hours||'')+' from your Vobiz number</span>'+
    '</div>';
}
const toPayload = r => ({name:r.name, phone:r.phone10, business:r.business, invoice_no:r.invoice_no, amount:r.amt, due_date:r.due, language:r.language});
async function queueRows(rows){
  const company = S.company.trim();
  if(!company){ showToast('Enter the company RIA calls on behalf of.'); return null; }
  const res = await api('api/calls', {rows, company});
  await refresh(); schedulePoll();
  return res;
}
async function callImported(){
  const ok = S.imp.rows.filter(r=>!r.errs.length);
  const btn = $('#callAllBtn'); if(btn) btn.disabled = true;
  let res;
  try { res = await queueRows(ok.map(toPayload)); }
  catch(e){ showToast(e.message); if(btn) btn.disabled = false; return; }
  if(!res){ if(btn) btn.disabled = false; return; }
  S.imp = null; S.tab = 'active'; render();
  if(!res.skipped.length){ showToast(res.queued+' added. Press Start calling to begin.'); return; }
  openModal(res.queued+' added, '+res.skipped.length+' skipped',
    '<p style="margin:0">These rows were not queued:</p><ul class="skipped">'+
      res.skipped.map(s=>'<li>Row '+ok[s.row-1].line+' · '+esc(ok[s.row-1].name)+' — '+esc(s.reason)+'</li>').join('')+'</ul>',
    '<button class="btn btn-primary btn-sm" onclick="closeModal()">OK</button>');
}
function dlRejects(){
  const bad = S.imp.rows.filter(r=>r.errs.length);
  downloadCSV('rejected-'+S.imp.file.replace(/\.csv$/i,'')+'.csv',
    [['row','name','phone','business','invoice_no','amount','due_date','language','problem'], ...bad.map(r=>[r.line,r.name,r.phone,r.business,r.invoice_no,r.amount,r.due_date,r.language,r.errs.join('; ')])]);
}
function dlTemplate(){
  downloadCSV('ria-invoice-list-template.csv', [['name','phone','business','invoice_no','amount','due_date','language'],
    ['Ravi Kumar','9876543210','Ravi Stores','INV-2047','48750','24-09-2026','Telugu'],
    ['Anil Sharma','+91 99630 55821','Sharma Traders','INV-2051','12500','30-09-2026','Hindi']]);
}
function callOneModal(){
  openModal('Add one customer',
    '<div class="form-grid">'+
      '<div class="field"><label for="oN">Customer name</label><input id="oN" type="text"></div>'+
      '<div class="field"><label for="oP">Mobile</label><input id="oP" type="tel" placeholder="98765 43210"></div>'+
      '<div class="field"><label for="oI">Invoice number</label><input id="oI" type="text"></div>'+
      '<div class="field"><label for="oA">Amount pending (₹)</label><input id="oA" type="number" min="1"></div>'+
      '<div class="field"><label for="oD">Due date</label><input id="oD" type="date"></div>'+
      '<div class="field"><label for="oL">Language</label><select id="oL"><option value="">Default</option><option value="te">Telugu</option><option value="hi">Hindi</option><option value="en">English</option></select></div>'+
    '</div>'+
    '<div class="field"><label for="oB">Business (optional)</label><input id="oB" type="text"></div>'+
    '<div class="field"><label for="oC">RIA calls on behalf of</label><input id="oC" type="text" value="'+esc(S.company)+'"></div>'+
    '<div id="oErr" class="msg msg-err hide" style="margin:0"></div>',
    '<button class="btn btn-ghost btn-sm" onclick="closeModal()">Cancel</button><button class="btn btn-primary btn-sm" id="oGo" onclick="callOne()">Add to call list</button>');
}
async function callOne(){
  const o = checkRow({name:$('#oN').value.trim(), phone:$('#oP').value.trim(), invoice_no:$('#oI').value.trim(), amount:$('#oA').value.trim(),
    due_date:$('#oD').value, business:$('#oB').value.trim(), language:$('#oL').value});
  S.company = $('#oC').value.trim() || S.company;
  if(o.errs.length){ $('#oErr').textContent = o.errs.join(' · '); $('#oErr').classList.remove('hide'); return; }
  $('#oGo').disabled = true;
  try {
    const res = await queueRows([toPayload(o)]);
    if(!res){ $('#oGo').disabled = false; return; }
    if(res.skipped.length){ $('#oErr').textContent = res.skipped[0].reason; $('#oErr').classList.remove('hide'); $('#oGo').disabled = false; return; }
    closeModal(); S.tab='active'; go('calls');
    showToast(S.dialer.paused ? 'Added. Press Start calling to call '+o.name+'.' : 'Added. RIA calls '+o.name+' after the calls before it.');
  } catch(e){ $('#oErr').textContent = e.message; $('#oErr').classList.remove('hide'); $('#oGo').disabled = false; }
}

/* ============================================================
   Promises to pay — the PTP date column
   ============================================================ */
const PTP_STATES = {today:['Due today','c-gold'], week:['This week','c-blue'], later:['Later','c-green'], passed:['Date passed','c-red']};
function ptpState(c){
  const today = todayISO();
  if(c.promised_date===today) return 'today';
  if(c.promised_date<today) return 'passed';
  return c.promised_date<=addDays(today,7) ? 'week' : 'later';
}
const PTP_FILTERS = {
  open:['Open (today and later)', c=>ptpState(c)!=='passed'],
  today:['Due today', c=>ptpState(c)==='today'],
  week:['Next 7 days', c=>['today','week'].includes(ptpState(c))],
  passed:['Date passed', c=>ptpState(c)==='passed'],
  all:['All promises', ()=>true]
};
function dlPtp(){
  exportCalls('ria-promises-to-pay-'+todayISO()+'.csv',
    latestPromises().filter(PTP_FILTERS[S.ptp][1]).sort((a,b)=>a.promised_date.localeCompare(b.promised_date)));
}
function scrPtp(){
  const all = latestPromises().sort((a,b)=>a.promised_date.localeCompare(b.promised_date));
  if(!all.length) return empty('No promises yet','When a customer gives RIA a date to pay, it shows up here with its PTP date.',
    '<button class="btn btn-primary btn-sm" onclick="go(\'calls\')">Go to calls</button>');
  const by = k => all.filter(c=>ptpState(c)===k);
  const sum = l => inr(l.reduce((a,c)=>a+promised(c),0));
  const list = all.filter(PTP_FILTERS[S.ptp][1]);
  const open = all.filter(PTP_FILTERS.open[1]);
  const rows = list.map(c=>{ const st = PTP_STATES[ptpState(c)];
    return '<tr style="cursor:default"><td>'+customerCell(c)+'</td><td>'+invoiceCell(c)+'</td>'+
      '<td class="num right">'+inr(promised(c))+(c.promised_amount&&c.promised_amount<c.amount?'<div class="meta">part payment</div>':'')+'</td>'+
      '<td class="num ptp-col nowrap" style="font-size:13.5px">'+esc(fmtDate(c.promised_date))+'</td>'+
      '<td>'+chip(st[0],st[1])+'</td>'+
      '<td class="num nowrap" style="font-size:12px">'+fmtTime(c.answered_at||c.created_at)+'</td>'+
      '<td style="font-size:12.5px;max-width:240px">'+esc(c.notes||'')+'</td>'+
      '<td class="right">'+(ptpState(c)==='passed'?'<button class="btn btn-ghost btn-sm" onclick="retryCall(\''+esc(c.call_id)+'\')">Call again</button>':'')+'</td></tr>'; }).join('')
    || '<tr><td colspan="8" style="text-align:center;color:var(--t3);padding:24px;cursor:default">No promises in this group.</td></tr>';
  return '<div class="card"><div class="ptp-sum">'+
      '<div><span class="num">'+sum(open)+'</span><span>'+open.length+' open promises</span></div>'+
      '<div><span class="num" style="color:var(--gold)">'+sum(by('today'))+'</span><span>'+by('today').length+' due today</span></div>'+
      '<div><span class="num" style="color:var(--blue)">'+sum(by('week'))+'</span><span>'+by('week').length+' in the next 7 days</span></div>'+
      '<div><span class="num" style="color:var(--red)">'+sum(by('passed'))+'</span><span>'+by('passed').length+' past their date</span></div>'+
    '</div>'+
    '<div class="filters"><select aria-label="Which promises" onchange="S.ptp=this.value;render()">'+
      Object.entries(PTP_FILTERS).map(([k,v])=>'<option value="'+k+'"'+(S.ptp===k?' selected':'')+'>'+v[0]+'</option>').join('')+'</select>'+
      '<div class="sp"><button class="btn btn-ghost btn-sm" onclick="dlPtp()">⤓ Download PTP list</button></div></div>'+
    '<div class="tbl-wrap"><table><thead><tr><th>Customer</th><th>Invoice</th><th class="right">Promised</th><th class="ptp-col">PTP date</th><th>Status</th><th>Captured</th><th>Notes</th><th></th></tr></thead><tbody>'+rows+'</tbody></table></div>'+
    '<div class="card-h" style="border-top:1px solid var(--line-2);border-bottom:0"><span class="sub">Showing '+list.length+' of '+all.length+' · the latest promise for each invoice.</span></div></div>';
}

/* ============================================================
   Settings
   ============================================================ */
function scrSettings(){
  const s = S.settings || {}, d = S.dialer || {};
  const missing = t => '<span style="color:var(--red)">'+t+'</span>';
  const kv = [
    ['Calls on behalf of', s.company ? esc(s.company) : missing('Not set — add COLLECTION_COMPANY')],
    ['Vobiz number', s.vobiz_number ? esc(s.vobiz_number) : missing('Not set — add VOBIZ_PHONE_NUMBER')],
    ['Vobiz SIP domain', s.vobiz_domain ? esc(s.vobiz_domain) : missing('Not set — add VOBIZ_SIP_DOMAIN')],
    ['Outbound trunk', d.configured ? 'Connected' : missing('Missing — run python backend/setup_vobiz.py')],
    ['Dialer', !d.configured ? '—' : !d.running ? missing('Stopped — check the backend log and restart it') : d.error ? missing(esc(d.error)) : 'Running'],
    ['Calling hours', esc(d.calling_hours||'')],
    ['Calls at the same time', esc(d.concurrency)],
    ['Longest call', esc(d.max_minutes)+' minutes'],
    ['Opening language', esc(LANGS[s.opening_language]||s.opening_language||'')],
    ['Speech to text', esc(s.stt||'')],
    ['Language model', esc(s.llm||'')],
    ['Voice', esc(s.tts||'')]
  ].map(r=>'<dt>'+r[0]+'</dt><dd>'+r[1]+'</dd>').join('');
  return '<div class="grid g2">'+
    '<div class="card"><div class="card-h"><h3>Configuration</h3><span class="sub">backend/.env</span></div><div class="card-b"><dl class="kv">'+kv+'</dl></div></div>'+
    '<div class="card" style="align-self:start"><div class="card-h"><h3>Calling</h3><div class="r">'+dialerChip()+'</div></div><div class="card-b">'+
      '<p style="margin:0 0 14px;color:var(--t2)">'+(d.paused?'RIA is not placing calls. Customers in the list wait until you press Start calling.':'RIA is calling the list one after another inside calling hours, and stops when the list is done. Stopping lets a call already on the line finish.')+'</p>'+
      startStopButton()+
    '</div></div>'+
  '</div>';
}
async function setPaused(paused){
  try { S.dialer = await api('api/dialer', {paused}); render(); showToast(paused?'Stopped. A call already on the line will finish.':(S.dialer.in_calling_hours?'Calling started.':'Started. Calls begin at '+String(S.dialer.calling_hours||'').slice(0,5)+' IST.')); }
  catch(e){ showToast(e.message); }
}

const SCREENS = {dashboard:scrDashboard, calls:scrCalls, ptp:scrPtp, settings:scrSettings};

document.addEventListener('keydown', e=>{ if(e.key==='Escape') closeModal(); });
document.addEventListener('visibilitychange', ()=>{ if(!document.hidden && S.auth && S.auth.signed_in) refresh().then(softRender); });
if(SCREENS[location.hash.slice(1)]) S.screen = location.hash.slice(1);
boot();
