"""面板单页 HTML（内嵌，无外部资源依赖）。

布局（用户定版）：左侧固定边栏 + 主区域分页，不整页刷新。边栏
自上而下：总览 / 中性网格 / 轮换对冲刷量 / 溢价套利 / 资金费套利 /
数据 / 设置 / 账本，底部有通用「＋ 新建任务」。

每个策略页同构，由通用渲染器按 /api/strategy-specs 的规格
（含 page 元数据：两腿字段与标签、primary 常用字段、机会类型）
生成，自上而下六个区块：交易平台（两腿场所下拉）→ 策略机会
（/api/market 排行按所选两腿过滤，点行回填）→ 保证金（两腿可用
余额，来自 /api/overview）→ 开仓区（primary 字段 + 大号运行按钮，
一键按自动任务名创建并启动）→ 高级配置（其余字段，折叠）→
本策略任务（状态/持仓/今日成交/启停改删/平仓）。

总览（场所权益/持仓/挂单、今日成交、未实现盈亏、KillSwitch）、
数据（公开行情全表与排行）、账本（最近成交）、设置（密钥只报
已填/未填、绝不回显值）保持原有功能。任务编辑仍走弹窗（与策略
页共用 specs 动态表单渲染/收集函数）。配置文件里的任务在面板
只可启停，面板任务才可删改，界面上如实标注来源。
"""

PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>DeTSP 交易面板</title>
<style>
body{font-family:system-ui,"Microsoft YaHei",sans-serif;margin:20px;background:#0f1420;color:#e6e9ef}
h1{font-size:20px} h2{font-size:16px;margin-top:24px}
table{border-collapse:collapse;width:100%;margin:8px 0}
th,td{border:1px solid #2a3247;padding:5px 8px;font-size:13px;text-align:left;vertical-align:top}
th{background:#1a2133}
button{background:#2456a6;color:#fff;border:0;padding:5px 12px;border-radius:4px;cursor:pointer}
button.stop{background:#8a3b3b} button.kill{background:#b02020;font-weight:bold}
button.mini{padding:2px 8px;font-size:12px;margin:2px 2px 2px 0}
.warn{background:#5a1f1f;border:1px solid #b02020;padding:8px 12px;border-radius:4px}
.ok{color:#7dd87d}.bad{color:#ff8080}.muted{color:#9aa4b8}
pre{background:#161c2b;padding:8px;overflow:auto;font-size:12px;margin:0}
.tag{border:1px solid #2a3247;border-radius:3px;padding:0 5px;font-size:11px;color:#9aa4b8}
#overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:10}
#modal{background:#161c2b;max-width:560px;margin:40px auto;padding:16px 20px;border-radius:8px;max-height:85vh;overflow:auto}
#modal h3{margin-top:0}
fieldset{border:1px solid #2a3247;border-radius:4px;margin:10px 0}
fieldset legend{color:#9aa4b8;font-size:12px;padding:0 6px}
#modal fieldset{border:1px solid #2a3247;margin:10px 0;border-radius:4px}
#modal legend{color:#9aa4b8;font-size:12px;padding:0 6px}
.fld{display:block;margin:7px 0;font-size:13px}
.fld input[type=number],.fld input[type=text],.fld select,#mName{width:100%;box-sizing:border-box;background:#0f1420;color:#e6e9ef;border:1px solid #2a3247;border-radius:4px;padding:5px 8px;margin-top:3px}
#modal select{background:#0f1420;color:#e6e9ef;border:1px solid #2a3247;border-radius:4px;padding:5px 8px}
#mErr{white-space:pre-wrap}
#layout{display:flex;gap:20px;align-items:flex-start}
#sidebar{width:172px;flex:0 0 172px;position:sticky;top:20px;display:flex;flex-direction:column;gap:4px}
#sidebar .sideTitle{font-size:19px;font-weight:bold;margin:2px 0 10px}
#sidebar button{text-align:left;background:#1a2133;padding:9px 12px;font-size:14px}
#sidebar button.on{background:#2456a6}
#sidebar .sideSep{border-top:1px solid #2a3247;margin:8px 0 4px}
#sidebar #newBtn{background:#2456a6;text-align:center;margin-top:10px}
#sidebar .sideNote{font-size:11px;color:#9aa4b8;line-height:1.6;margin-top:10px}
#main{flex:1;min-width:0}
.page{display:none}.page.on{display:block}
.block{border:1px solid #2a3247;border-radius:6px;padding:12px 14px;margin:14px 0}
.block h3{margin:2px 0 10px;font-size:15px}
label.inl{display:inline-block;margin:0 18px 4px 0;font-size:13px}
label.inl select{display:block;margin-top:4px;background:#0f1420;color:#e6e9ef;border:1px solid #2a3247;border-radius:4px;padding:5px 8px;min-width:170px}
.pform select,.aform select,.pform input,.aform input{background:#0f1420;color:#e6e9ef;border:1px solid #2a3247;border-radius:4px;padding:5px 8px}
.pform .fld input,.pform .fld select,.aform .fld input,.aform .fld select{width:100%;box-sizing:border-box;margin-top:3px}
.pform{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:0 16px}
.runBtn{font-size:16px;padding:10px 30px;font-weight:bold}
.runErr{white-space:pre-wrap}
.marginLine{font-size:15px;margin:4px 0}
tr.best td{background:#1d2f1d}
tr.selrow td{background:#1d2a47}
td.na{color:#5c6578}
.chip{background:#1a2133;margin:2px 4px 2px 0}
.chip.on{background:#2456a6}
details{margin:6px 0}
details summary{cursor:pointer;color:#9aa4b8;font-size:13px}
details div{font-size:13px;padding:6px 4px;line-height:1.7}
.sec input{background:#0f1420;color:#e6e9ef;border:1px solid #2a3247;border-radius:4px;padding:5px 8px;width:100%;box-sizing:border-box;margin-top:3px}
.mktviews{margin:10px 0}
.mktviews button{background:#1a2133;margin-right:6px}
.mktviews button.on{background:#2456a6}
</style>
</head>
<body>
<div id="layout">
<nav id="sidebar">
<div class="sideTitle">DeTSP 交易面板</div>
<button data-page="overview" class="on">总览</button>
<button data-page="strat-hedged_grid">中性网格</button>
<button data-page="strat-rotation_hedge">轮换对冲刷量</button>
<button data-page="strat-premium_arb">溢价套利</button>
<button data-page="strat-funding_carry">资金费套利</button>
<div class="sideSep"></div>
<button data-page="market">数据</button>
<button data-page="settings">设置</button>
<button data-page="fills">账本</button>
<button id="newBtn">＋ 新建任务</button>
<p class="sideNote">边栏底部「＋ 新建任务」是通用入口（弹窗选策略）；
每个策略页里则直接在本页选标的、一键运行。</p>
</nav>
<main id="main">

<section class="page on" id="page-overview">
<h2>总览 <span class="muted" id="mode"></span></h2>
<p class="warn">⚠ 本面板只允许绑本机 127.0.0.1 使用，POST 操作带随机
token 校验（防跨站借道），但<b>不等于登录鉴权</b>：不要改绑公网地址、
不要做端口映射。</p>
<div id="killbox"></div>
<p>今日成交量：<b id="tvol">-</b>　今日手续费：<b id="tfee">-</b>　
今日成交笔数：<b id="tcnt">-</b>　持仓未实现盈亏（估算）：<b id="upnl">-</b></p>
<table id="venues"><thead><tr>
<th>场所</th><th>模式</th><th>权益</th><th>可用</th><th>持仓</th><th>挂单数</th>
</tr></thead><tbody></tbody></table>
<div id="events"></div>
</section>

<section class="page" id="page-strat-hedged_grid"></section>
<section class="page" id="page-strat-rotation_hedge"></section>
<section class="page" id="page-strat-premium_arb"></section>
<section class="page" id="page-strat-funding_carry"></section>

<section class="page" id="page-market">
<h2>数据（公开行情，不用密钥）</h2>
<p class="muted">全所总表与机会排行；策略页里有按策略过滤好的同一份数据，
这里看全貌。资金费已统一换算为每小时 bps；标「混口径」的组合只是
参考——策略选向时会拒绝用不同时点口径的费率相减开仓。<br>
数据时间：<b id="mktTime">-</b>　<button class="mini" id="mktRefresh">刷新</button>
<span class="muted">（打开数据/策略页时每 30 秒自动刷新）</span></p>
<div class="mktviews" id="viewBtns">
<button data-view="spread" class="on">价差排行（溢价套利）</button>
<button data-view="funding">资金费差排行（资金费套利 / 轮换对冲）</button>
<button data-view="grid">网格区间（中性网格）</button>
</div>
<div id="view-spread" class="mktview">
<h3>价差排行（溢价套利：在便宜的所买、贵的所卖）</h3>
<table id="spreadTbl"><thead><tr>
<th>币种</th><th>买入（低价所）</th><th>卖出（高价所）</th><th>价差 bps</th><th></th><th>操作</th>
</tr></thead><tbody></tbody></table>
</div>
<div id="view-funding" class="mktview" style="display:none">
<h3>资金费差排行（资金费套利 / 轮换对冲：多费率低的所、空费率高的所）</h3>
<table id="fundTbl"><thead><tr>
<th>币种</th><th>做多（低费率）</th><th>做空（高费率）</th><th>净费差 bps/小时</th><th>口径</th><th>操作</th>
</tr></thead><tbody></tbody></table>
</div>
<div id="view-grid" class="mktview" style="display:none">
<h3>中性网格：选币看 24 小时区间</h3>
<p>币种 <select id="gridSym"></select>
场所 <select id="gridVenue"><option value="hyperliquid">hyperliquid</option>
<option value="entropy">entropy</option><option value="tradexyz">tradexyz</option></select>
<button class="mini" id="gridGo">查看区间</button>
<span class="muted">（K 线只有 Hyperliquid 家族有公开接口）</span></p>
<div id="gridOut"></div>
</div>
<h3>全表（每币种 × 7 所：价格 / 资金费 bps·小时）</h3>
<div id="mktMatrix"></div>
</section>

<section class="page" id="page-settings">
<h2>设置（密钥）</h2>
<p class="muted">这里只填/改交易所密钥：已填的只显示「已设置」，<b>绝不回显任何密钥值</b>；
输入框留空 = 不修改。保存后写回 .env 文件，<b>需重启程序才生效</b>。
只填 API 钱包/签名私钥，永远不要填主钱包私钥或助记词。</p>
<div id="secGroups"></div>
<p><button id="secSave">保存密钥设置</button>
<span id="secMsg" class="ok"></span></p>
</section>

<section class="page" id="page-fills">
<h2>账本（最近成交）</h2>
<table id="fills"><thead><tr>
<th>时间</th><th>场所</th><th>币种</th><th>策略/任务</th><th>方向</th>
<th>数量</th><th>价格</th><th>手续费</th><th>订单号</th>
</tr></thead><tbody></tbody></table>
</section>

</main>
</div>

<div id="overlay"><div id="modal">
<h3 id="mTitle">新建任务</h3>
<p class="fld">策略
<select id="mStrategy"></select></p>
<p class="muted" id="mBlurb"></p>
<p class="fld" id="mNameRow">任务名（唯一，建后不可改名）
<input type="text" id="mName" placeholder="如 carry-BTC-093015"></p>
<div id="mForm"></div>
<p class="bad" id="mErr"></p>
<p><button id="mSave">保存</button>
<button id="mCancel" style="background:#445">取消</button></p>
<p class="muted">运行中的任务保存新参数时，会先停止（撤单+平仓）再按新参数重组；
重组失败不会保存新参数，可点启动按原参数恢复。</p>
</div></div>

<script>
const $=s=>document.querySelector(s);
const TOKEN="__TOKEN__"; // 由后端在返回页面时注入（H2）
let SPECS=[], VENUES=[], TASKS=[], EDITING=null, LAST_OV=null, FILLS=[];
function esc(x){return String(x==null?"":x).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function fmt(x,d=2){return x==null?"-":Number(x).toFixed(d)}
async function post(url){const r=await fetch(url,{method:"POST",headers:{"X-API-Token":TOKEN}});return r.json()}
async function postJson(url,obj){const r=await fetch(url,{method:"POST",headers:{"X-API-Token":TOKEN,"Content-Type":"application/json"},body:JSON.stringify(obj)});return r.json()}

async function loadSpecs(){
const d=await (await fetch("/api/strategy-specs")).json();
SPECS=d.strategies||[]; VENUES=d.venues||[];
$("#mStrategy").innerHTML=SPECS.map(s=>'<option value="'+s.strategy+'">'+esc(s.title)+"（"+s.strategy+"）</option>").join("");
SPECS.forEach(buildStrategyPage);
}

function specOf(st){return SPECS.find(s=>s.strategy===st)}

// 单个字段渲染（弹窗表单与策略页共用）。symbolSelect 时币种渲染成
// 下拉（选项由 fillSymbolSelects 按观察列表填充），否则文本框。
function renderField(f, v, symbolSelect){
if(f.type==="bool")
return '<label class="fld"><input type="checkbox" data-f="'+f.name+'"'+(v?" checked":"")+"> "+esc(f.label)+"</label>";
if(f.type==="enum")
return '<label class="fld">'+esc(f.label)+'<select data-f="'+f.name+'">'+f.options.map(o=>'<option value="'+esc(o.value)+'"'+(o.value===v?" selected":"")+">"+esc(o.label)+"</option>").join("")+"</select></label>";
if(f.type==="venue")
return '<label class="fld">'+esc(f.label)+'<select data-f="'+f.name+'">'+VENUES.map(x=>'<option value="'+esc(x)+'"'+(x===v?" selected":"")+">"+esc(x)+"</option>").join("")+"</select></label>";
if(symbolSelect&&f.name==="symbol")
return '<label class="fld">'+esc(f.label)+(f.required?" *":"")+'<select data-f="symbol" class="symSel"></select></label>';
const t=(f.type==="int"||f.type==="float")?"number":"text";
const step=f.type==="int"?"1":"any";
return '<label class="fld">'+esc(f.label)+(f.required?" *":"")+'<input type="'+t+'" step="'+step+'" data-f="'+f.name+'" value="'+(v==null?"":esc(v))+'"></label>';
}

function fieldsHtml(fields, values, symbolSelect){
const groups={};
fields.forEach(f=>{(groups[f.group]=groups[f.group]||[]).push(f)});
const names=Object.keys(groups);
if(names.length<=1) return fields.map(f=>renderField(f,(values&&values[f.name]!==undefined)?values[f.name]:f.default,symbolSelect)).join("");
return names.map(g=>"<fieldset><legend>"+esc(g)+"</legend>"+
groups[g].map(f=>renderField(f,(values&&values[f.name]!==undefined)?values[f.name]:f.default,symbolSelect)).join("")+"</fieldset>").join("");
}

function buildForm(st, values){
const spec=specOf(st); if(!spec){$("#mForm").innerHTML="";return}
$("#mBlurb").textContent=spec.blurb||"";
$("#mForm").innerHTML=fieldsHtml(spec.fields, values, false);
}

// 从容器内按规格收集参数（弹窗与策略页共用同一口径）
function collectFrom(box, spec){
const out={};
for(const f of spec.fields){
const el=box.querySelector('[data-f="'+f.name+'"]'); if(!el) continue;
if(f.type==="bool"){out[f.name]=el.checked;continue}
if(f.type==="enum"||f.type==="venue"||f.type==="str"||el.tagName==="SELECT"){
const v=el.value.trim(); if(v!=="")out[f.name]=v; continue}
const v=el.value.trim();
if(v===""){continue}
out[f.name]=Number(v);
}
return out;
}

function applyPrefill(box, params){
if(!params) return;
for(const k in params){
const el=box.querySelector('[data-f="'+k+'"]'); if(!el) continue;
if(el.type==="checkbox") el.checked=!!params[k];
else el.value=params[k];
}
}

function openModal(editName, prefill){
EDITING=editName||null;
const t=editName?TASKS.find(x=>x.name===editName):null;
$("#mTitle").textContent=t?("编辑任务："+t.name):"新建任务";
$("#mNameRow").style.display=t?"none":"block";
$("#mName").value="";
const st=t?t.strategy:((prefill&&prefill.strategy)||(SPECS[0]&&SPECS[0].strategy));
$("#mStrategy").value=st;
$("#mStrategy").disabled=!!t;
buildForm(st, t?(t.params.params||{}):null);
if(prefill&&prefill.params) applyPrefill($("#mForm"), prefill.params);
$("#mErr").textContent="";
$("#overlay").style.display="block";
}
function closeModal(){$("#overlay").style.display="none"}

$("#newBtn").addEventListener("click",()=>openModal(null));
$("#mCancel").addEventListener("click",closeModal);
$("#mStrategy").addEventListener("change",e=>buildForm(e.target.value,null));
$("#mSave").addEventListener("click",async()=>{
const st=$("#mStrategy").value;
const params=collectFrom($("#mForm"), specOf(st));
let r;
if(EDITING) r=await postJson("/api/tasks/update",{name:EDITING,params});
else r=await postJson("/api/tasks/create",{name:$("#mName").value.trim(),strategy:st,params});
if(!r.ok){$("#mErr").textContent=r.error||"保存失败";return}
closeModal(); refresh();
});

document.addEventListener("click",async e=>{
const b=e.target.closest("[data-act]"); if(!b)return;
const act=b.dataset.act, name=b.dataset.name;
if(act==="kill"){if(confirm("确认触发全局停机？所有任务将停止开仓。")){await post("/api/killswitch");refresh()}}
else if(act==="killreset"){await post("/api/killswitch/reset");refresh()}
else if(act==="stop"||act==="start"){
const r=await post("/api/tasks/"+encodeURIComponent(name)+"/"+act);
if(!r.ok)alert(r.error||"操作失败"); else if(r.note)alert(r.note);
refresh();
}
else if(act==="edit"){openModal(name)}
else if(act==="del"){
if(confirm("确认删除任务「"+name+"」？参数将从面板任务文件移除，无法恢复。")){
const r=await postJson("/api/tasks/delete",{name});
if(!r.ok)alert(r.error||"删除失败");
refresh();
}
}
else if(act==="close"){
const v=b.dataset.venue,s=b.dataset.symbol,o=b.dataset.owner;
const msg=o?("确认平掉 "+v+" "+s+" 在任务「"+o+"」名下的持仓？\\n注意：该任务若仍在运行，可能按自身逻辑重新开仓或对账处理。")
:("⚠ 手动平仓确认："+v+" "+s+" 没有任务归属，将平掉该场所该币种的全部持仓（含手工仓位）。确认平仓？");
if(confirm(msg)){
const r=await postJson("/api/positions/close",{venue_id:v,symbol:s});
alert(r.ok?r.note:(r.error||"平仓失败"));
refresh();
}
}
else if(act==="newtask"){
let pf=null;
try{pf=JSON.parse(b.dataset.prefill||"null")}catch(e){}
openModal(null, pf);
}
});

// ---------- 总览 / 任务 / 成交刷新（5 秒） ----------
function taskPositions(name){
const out=[];
if(!LAST_OV) return out;
LAST_OV.venues.forEach(v=>v.positions.forEach(p=>{
if(p.owner===name) out.push({venue:v.venue_id,...p});
}));
return out;
}
function taskTodayFills(name){
const t0=new Date(); t0.setHours(0,0,0,0);
const ts0=t0.getTime()/1000;
let n=0, vol=0;
FILLS.forEach(f=>{if(f.strategy===name&&f.ts>=ts0){n++; vol+=Math.abs(f.qty)*f.price}});
return {n, vol};
}

async function refresh(){
const ov=await (await fetch("/api/overview")).json();
LAST_OV=ov;
$("#mode").textContent="（"+(ov.dry_run?"DRY_RUN 只决策不下单":"实盘模式")+"）";
const ks=ov.killswitch;
$("#killbox").innerHTML=ks.halted
? '<p class="bad">⛔ 全局停机中：'+esc(ks.reason)+' <button data-act="killreset">解除停机</button></p>'
: '<p class="ok">运行中</p><button class="kill" data-act="kill">⛔ 全局停机</button>';
$("#tvol").textContent=fmt(ov.today.volume);
$("#tfee").textContent=fmt(ov.today.fees,4);
$("#tcnt").textContent=ov.today.fills;
$("#upnl").textContent=fmt(ov.unrealized_pnl);
$("#venues tbody").innerHTML=ov.venues.map(v=>"<tr><td>"+esc(v.venue_id)+"</td><td>"+esc(v.mode)+
"</td><td>"+fmt(v.equity)+"</td><td>"+fmt(v.available)+"</td><td>"+
(v.positions.length?v.positions.map(p=>
esc(p.symbol)+" "+p.qty+" @ "+p.entry_price+
(p.owner?' <span class="tag">任务 '+esc(p.owner)+"</span>":' <span class="tag">无归属</span>')+
' <button class="mini stop" data-act="close" data-venue="'+esc(v.venue_id)+'" data-symbol="'+esc(p.symbol)+'" data-owner="'+esc(p.owner||"")+'">平仓</button>'
).join("<br>"):"-")+
(v.error?'<br><span class="bad">'+esc(v.error)+"</span>":"")+
"</td><td>"+v.open_orders+"</td></tr>").join("");
$("#events").innerHTML=(ov.events&&ov.events.length)
? '<p class="muted">系统事件（启动对账、面板操作等）：<br>'+ov.events.map(esc).join("<br>")+"</p>": "";
const tk=await (await fetch("/api/tasks")).json();
TASKS=tk.tasks;
const fl=await (await fetch("/api/fills?limit=200")).json();
FILLS=fl.fills||[];
$("#fills tbody").innerHTML=FILLS.slice(0,50).map(f=>"<tr><td>"+new Date(f.ts*1000).toLocaleString()+
"</td><td>"+esc(f.venue)+"</td><td>"+esc(f.symbol)+"</td><td>"+esc(f.strategy)+"</td><td>"+esc(f.side)+
"</td><td>"+f.qty+"</td><td>"+f.price+"</td><td>"+f.fee+"</td><td>"+esc(f.order_id)+"</td></tr>").join("");
SPECS.forEach(spec=>{renderMargin(spec.strategy); renderStratTasks(spec.strategy)});
}

// ---------- 左侧边栏导航 ----------
let CURPAGE="overview", MKT=null, SECLOADED=false;
function switchPage(name){
CURPAGE=name;
document.querySelectorAll("#sidebar button[data-page]").forEach(b=>b.classList.toggle("on",b.dataset.page===name));
document.querySelectorAll(".page").forEach(p=>p.classList.toggle("on",p.id==="page-"+name));
if(name==="market"||name.indexOf("strat-")===0){loadMarket(false)}
if(name==="settings"&&!SECLOADED){loadSecrets()}
}
document.querySelectorAll("#sidebar button[data-page]").forEach(b=>b.addEventListener("click",()=>switchPage(b.dataset.page)));

// ---------- 策略页（通用渲染器，四页同构） ----------
const SPAGES={};   // strategy -> 页面状态
const HL_FAMILY=["hyperliquid","entropy","tradexyz"];
const DEFAULT_WATCHLIST=["BTC","ETH","SOL","HYPE","AAPL","SNDK"];

function buildStrategyPage(spec){
const st=spec.strategy, pg=spec.page;
const root=document.getElementById("page-strat-"+st);
if(!root) return;
const primary=spec.fields.filter(f=>f.primary&&pg.venue_fields.indexOf(f.name)<0);
const advanced=spec.fields.filter(f=>!f.primary&&pg.venue_fields.indexOf(f.name)<0);
const oppTitle={funding:"策略机会（资金费差排行：多费率低的所、空费率高的所）",
spread:"策略机会（价差排行：在便宜的所买、贵的所卖）",
grid:"策略机会（价格与近 24 小时区间）"}[pg.opportunity];
root.innerHTML=
"<h2>"+esc(spec.title)+'</h2><p class="muted">'+esc(spec.blurb)+"</p>"+
'<div class="block"><h3>交易平台</h3>'+
'<label class="inl">'+esc(pg.venue_labels[0])+' <select class="vA"></select></label>'+
'<label class="inl">'+esc(pg.venue_labels[1])+' <select class="vB"></select></label>'+
'<span class="muted">切换场所后，下方机会表与保证金跟着刷新。</span>'+
'<p class="bad listNote"></p></div>'+
'<div class="block"><h3>'+esc(oppTitle)+'</h3><div class="opp"><p class="muted">行情加载中…</p></div></div>'+
'<div class="block"><h3>保证金</h3><div class="margin"><p class="muted">加载中…</p></div></div>'+
'<div class="block"><h3>开仓</h3><div class="pform">'+
primary.map(f=>renderField(f,f.default,true)).join("")+"</div>"+
(pg.note?'<p class="muted">'+esc(pg.note)+"</p>":"")+
'<p class="bad runErr"></p>'+
'<button class="runBtn">▶ 运行</button> <span class="ok runOk"></span>'+
'<span class="muted">任务名自动生成（策略-币种-时分秒），点运行即创建并启动。</span></div>'+
'<details class="block"><summary>高级配置（其余参数，改动随运行一并提交）</summary>'+
'<div class="aform">'+fieldsHtml(advanced,null,false)+"</div></details>"+
'<div class="block"><h3>本策略任务</h3><table class="ttasks"><thead><tr>'+
"<th>名称</th><th>标的</th><th>来源</th><th>状态</th><th>持仓</th><th>今日成交</th><th>最近错误</th><th>操作</th>"+
'</tr></thead><tbody></tbody></table></div>';
const S={spec:spec, root:root, showAll:false, selSymbol:"",
vA:root.querySelector(".vA"), vB:root.querySelector(".vB"),
listNote:root.querySelector(".listNote"),
opp:root.querySelector(".opp"), margin:root.querySelector(".margin"),
pform:root.querySelector(".pform"), aform:root.querySelector(".aform"),
runErr:root.querySelector(".runErr"), runOk:root.querySelector(".runOk"),
tbody:root.querySelector(".ttasks tbody")};
SPAGES[st]=S;
const opts=VENUES.map(v=>'<option value="'+esc(v)+'">'+esc(v)+"</option>").join("");
S.vA.innerHTML=opts; S.vB.innerHTML=opts;
if(VENUES.length>1) S.vB.selectedIndex=1;
const onVenue=()=>{renderOpp(st); renderMargin(st); if(pg.opportunity==="grid")loadGridCandles(st)};
S.vA.addEventListener("change",onVenue);
S.vB.addEventListener("change",onVenue);
const symEl=S.pform.querySelector('[data-f="symbol"]');
if(symEl) symEl.addEventListener("change",()=>{S.selSymbol=symEl.value; applyListingGuards(st); renderOpp(st); if(pg.opportunity==="grid")loadGridCandles(st)});
S.opp.addEventListener("click",e=>{
const tr=e.target.closest("tr[data-sym]"); if(!tr) return;
pickOppRow(st, tr.dataset.sym, tr.dataset.va, tr.dataset.vb);
});
root.querySelector(".runBtn").addEventListener("click",()=>runStrategy(st));
fillSymbolSelects();
}

function pageSymbol(S){
const el=S.pform.querySelector('[data-f="symbol"]');
return el?el.value:(S.selSymbol||"");
}
function setPageSymbol(S, sym){
S.selSymbol=sym;
const el=S.pform.querySelector('[data-f="symbol"]');
if(!el) return;
if(![...el.options].some(o=>o.value===sym)){
const o=document.createElement("option"); o.value=sym; o.textContent=sym; el.appendChild(o);
}
el.value=sym;
}

// 点机会行：回填场所与币种到本页开仓区
function pickOppRow(st, sym, va, vb){
const S=SPAGES[st]; if(!S) return;
if(va) S.vA.value=va;
if(vb) S.vB.value=vb;
if(sym) setPageSymbol(S, sym);
applyListingGuards(st);
renderOpp(st); renderMargin(st);
if(S.spec.page.opportunity==="grid") loadGridCandles(st);
}

// 上市联动：选定币种后，未上市该标的的场所在两腿下拉里置灰禁用
// 并标（未上市）；已选腿若未上市，自动切到一所上市的并给页面提示。
// 数据异常（error）只是暂时取不到，不禁用。
function symVenueStatus(sym, vid){
if(!MKT) return null;
const row=MKT.symbols.find(x=>x.symbol===sym);
if(!row) return null;
const c=row.venues[vid];
return c?c.status:null;
}
function applyListingGuards(st){
const S=SPAGES[st]; if(!S||!MKT) return;
const sym=pageSymbol(S); if(!sym) return;
const notes=[];
[S.vA, S.vB].forEach(sel=>{
[...sel.options].forEach(o=>{
const nl=symVenueStatus(sym, o.value)==="not_listed";
o.disabled=nl;
o.textContent=nl?o.value+"（未上市）":o.value;
});
});
const fixLeg=(sel, other)=>{
const cur=sel.options[sel.selectedIndex];
if(!cur||!cur.disabled) return;
const alt=[...sel.options].find(o=>!o.disabled&&o.value!==other.value)
||[...sel.options].find(o=>!o.disabled);
if(alt){notes.push(cur.value+" 未上市 "+sym+"，已自动切换为 "+alt.value);
sel.value=alt.value}
};
fixLeg(S.vA, S.vB); fixLeg(S.vB, S.vA);
if(S.listNote) S.listNote.textContent=notes.join("；");
if(notes.length){renderMargin(st)}
}

// 机会表过滤：行两腿与所选两腿是同一对（任一方向）
function pairMatch(S, x, y){
const a=S.vA.value, b=S.vB.value;
return (x===a&&y===b)||(x===b&&y===a);
}

function renderOpp(st){
const S=SPAGES[st]; if(!S) return;
const kind=S.spec.page.opportunity;
if(kind==="grid"){renderGridOpp(st); return}
if(!MKT){S.opp.innerHTML='<p class="muted">行情加载中…</p>'; return}
const isFund=(kind==="funding");
const all=isFund?MKT.funding_ranking:MKT.spread_ranking;
const rows=S.showAll?all:all.filter(r=>isFund?pairMatch(S,r.long_venue,r.short_venue):pairMatch(S,r.buy_venue,r.sell_venue));
const firstClean=rows.findIndex(r=>!r.mixed);
const head=isFund
?"<tr><th>币种</th><th>做多（低费率）</th><th>做空（高费率）</th><th>净费差 bps/小时</th><th>口径</th></tr>"
:"<tr><th>币种</th><th>买入（低价所）</th><th>卖出（高价所）</th><th>价差 bps</th><th></th></tr>";
const body=rows.map((r,i)=>{
let cls="";
if(i===firstClean) cls="best";
if(r.symbol===pageSymbol(S)) cls=(cls+" selrow").trim();
const sel=cls?' class="'+cls+'"':"";
if(isFund)
return '<tr data-sym="'+esc(r.symbol)+'" data-va="'+esc(r.long_venue)+'" data-vb="'+esc(r.short_venue)+'"'+sel+'><td>'+esc(r.symbol)+'</td><td>'+esc(r.long_venue)+'</td><td>'+esc(r.short_venue)+
"</td><td>"+fmt(r.edge_bps_h,3)+"</td><td>"+(r.mixed?'<span class="bad">混口径（仅参考）</span>':esc(KIND_CN[r.rate_kind]||r.rate_kind))+(i===firstClean?" · 当前最优":"")+"</td></tr>";
return '<tr data-sym="'+esc(r.symbol)+'" data-va="'+esc(r.buy_venue)+'" data-vb="'+esc(r.sell_venue)+'"'+sel+'><td>'+esc(r.symbol)+'</td><td>'+esc(r.buy_venue)+'</td><td>'+esc(r.sell_venue)+
"</td><td>"+fmt(r.spread_bps,2)+"</td><td>"+(i===firstClean?"当前最优":"")+"</td></tr>";
}).join("");
S.opp.innerHTML="<table><thead>"+head+"</thead><tbody>"+
(body||'<tr><td colspan="5" class="muted">所选两腿组合暂无可运行的标的（该组合下没有两所都有数据的币种），可点下方看全部。</td></tr>')+
"</tbody></table>"+
'<p class="muted">点某一行 = 选中该标的（场所与币种回填到下方开仓区）。 '+
'<a href="#" class="tglAll">'+(S.showAll?"只看所选两腿组合":"看全部组合（不限场所）")+"</a></p>";
S.opp.querySelector(".tglAll").addEventListener("click",e=>{e.preventDefault(); S.showAll=!S.showAll; renderOpp(st)});
}

// 中性网格机会区：现价 + 24h 高低 + 建议区间（一键填入开仓区）。
// candles 结果按 (场所,币种) 缓存在页面状态里，行情定时刷新重渲染
// 时直接用缓存，不反复打接口。
function renderGridOpp(st){
const S=SPAGES[st]; if(!S) return;
const sym=pageSymbol(S)||(MKT&&MKT.watchlist&&MKT.watchlist[0])||"BTC";
const chips=((MKT&&MKT.watchlist)||DEFAULT_WATCHLIST).map(s=>
'<button class="chip'+(s===sym?" on":"")+'" data-chip="'+esc(s)+'">'+esc(s)+"</button>").join("");
let priceLine='<p class="muted">行情加载中…</p>';
if(MKT){
const row=MKT.symbols.find(x=>x.symbol===sym);
const cell=row&&row.venues[S.vA.value];
let txt;
if(!cell||cell.status==="error") txt='<span title="'+esc((cell&&cell.reason)||"数据暂时取不到")+'">数据异常</span>';
else if(cell.status==="not_listed") txt="未上市";
else txt=fmt(cell.price,cell.price<10?4:2);
priceLine="<p>"+esc(sym)+" 在 "+esc(S.vA.value)+" 现价：<b>"+txt+"</b></p>";
}
S.opp.innerHTML="<p>"+chips+"</p>"+priceLine+'<div class="gridInfo"></div>';
S.opp.querySelectorAll("[data-chip]").forEach(c=>c.addEventListener("click",()=>{setPageSymbol(S,c.dataset.chip); applyListingGuards(st); renderGridOpp(st)}));
renderGridInfo(st);
}

function renderGridInfo(st){
const S=SPAGES[st]; if(!S) return;
const info=S.opp.querySelector(".gridInfo"); if(!info) return;
const venue=S.vA.value, sym=pageSymbol(S);
const key=venue+"|"+sym;
if(HL_FAMILY.indexOf(venue)<0){
info.innerHTML='<p class="muted">K 线只有 Hyperliquid 家族（hyperliquid / entropy / tradexyz）有公开接口；当前网格腿是 '+esc(venue)+'，请在「交易平台」把网格腿切到 Hyperliquid 家族再看建议区间。</p>';
return;
}
const gd=S.gridData;
if(gd&&gd.key===key){
if(!gd.ok){info.innerHTML='<p class="bad">'+esc(gd.error||"该币在该所无 K 线数据")+"</p>"; return}
info.innerHTML="<p>近 24 小时：最高 <b>"+fmt(gd.high,gd.high<10?4:2)+"</b>，最低 <b>"+fmt(gd.low,gd.low<10?4:2)+
"</b>。建议网格区间（高低各扩 2%）：<b>"+fmt(gd.suggested_lower,gd.suggested_lower<10?4:2)+" ~ "+fmt(gd.suggested_upper,gd.suggested_upper<10?4:2)+"</b> "+
'<button class="mini useRange">填入开仓区</button></p>';
info.querySelector(".useRange").addEventListener("click",()=>{
applyPrefill(S.pform,{lower_price:gd.suggested_lower,upper_price:gd.suggested_upper});
});
return;
}
info.innerHTML='<p class="muted">查询 '+esc(sym)+" 近 24 小时区间中…</p>";
loadGridCandles(st);
}

async function loadGridCandles(st){
const S=SPAGES[st]; if(!S) return;
const venue=S.vA.value, sym=pageSymbol(S);
const key=venue+"|"+sym;
if(S.gridFetching===key) return;
S.gridFetching=key;
try{
const d=await (await fetch("/api/market/candles?venue="+encodeURIComponent(venue)+"&symbol="+encodeURIComponent(sym))).json();
S.gridData={key:key, ok:!!d.ok, high:d.high, low:d.low,
suggested_lower:d.suggested_lower, suggested_upper:d.suggested_upper,
error:d.error};
}catch(e){S.gridData={key:key, ok:false, error:"网络异常，K线暂时取不到"}}
S.gridFetching="";
renderGridInfo(st);
}

// 保证金区：两腿可用余额（总览数据，paper 标注模拟）
function renderMargin(st){
const S=SPAGES[st]; if(!S||!LAST_OV) return;
const labels=S.spec.page.venue_labels;
const line=(label, vid)=>{
const v=LAST_OV.venues.find(x=>x.venue_id===vid);
if(!v) return label+" "+esc(vid)+"：-";
if(v.error) return label+" "+esc(vid)+'：<span class="bad">'+esc(v.error)+"</span>";
return label+" <b>"+esc(vid)+"</b>：$"+fmt(v.available)+" 可用"+
(v.mode==="paper"?' <span class="tag">模拟余额</span>':"")+
' <span class="muted">（权益 $'+fmt(v.equity)+"）</span>";
};
S.margin.innerHTML='<p class="marginLine">'+line(labels[0], S.vA.value)+"</p>"+
'<p class="marginLine">'+line(labels[1], S.vB.value)+"</p>";
}

// 本策略任务表（状态/持仓/今日成交/操作，数据来自 overview+tasks+fills）
function renderStratTasks(st){
const S=SPAGES[st]; if(!S) return;
const list=TASKS.filter(t=>t.strategy===st);
S.tbody.innerHTML=list.map(t=>{
const sym=(t.params&&t.params.params&&t.params.params.symbol)||"-";
const poss=taskPositions(t.name);
const posHtml=poss.length?poss.map(p=>esc(p.venue)+" "+esc(p.symbol)+" "+p.qty+" @ "+p.entry_price+
' <button class="mini stop" data-act="close" data-venue="'+esc(p.venue)+'" data-symbol="'+esc(p.symbol)+'" data-owner="'+esc(t.name)+'">平仓</button>').join("<br>"):"-";
const tf=taskTodayFills(t.name);
let btns="";
if(t.status==="running")btns+='<button class="mini stop" data-act="stop" data-name="'+esc(t.name)+'">停止</button>';
else if(t.status==="stopped"||t.status==="error")btns+='<button class="mini" data-act="start" data-name="'+esc(t.name)+'">启动</button>';
if(t.source==="panel"){
btns+='<button class="mini" data-act="edit" data-name="'+esc(t.name)+'">编辑</button>';
btns+='<button class="mini stop" data-act="del" data-name="'+esc(t.name)+'">删除</button>';
}
return "<tr><td>"+esc(t.name)+"</td><td>"+esc(sym)+"</td><td>"+
(t.source==="panel"?"面板":"配置文件")+"</td><td>"+esc(t.status)+"</td><td>"+posHtml+
"</td><td>"+(tf.n?tf.n+" 笔 / $"+fmt(tf.vol):"-")+"</td><td>"+esc(t.last_error||"")+"</td><td>"+btns+"</td></tr>";
}).join("")||'<tr><td colspan="8" class="muted">暂无该策略任务，用上方开仓区一键运行，或边栏底部「＋ 新建任务」。</td></tr>';
}

// 一键运行：组装参数 → 自动任务名 → 创建并启动
async function runStrategy(st){
const S=SPAGES[st]; if(!S) return;
const spec=S.spec, pg=spec.page;
const params={};
params[pg.venue_fields[0]]=S.vA.value;
params[pg.venue_fields[1]]=S.vB.value;
Object.assign(params, collectFrom(S.pform, spec), collectFrom(S.aform, spec));
const now=new Date();
const pad=n=>String(n).padStart(2,"0");
const name=pg.name_prefix+"-"+(params.symbol||"NA")+"-"+pad(now.getHours())+pad(now.getMinutes())+pad(now.getSeconds());
S.runErr.textContent=""; S.runOk.textContent="";
const r=await postJson("/api/tasks/create",{name:name, strategy:st, params:params});
if(!r.ok){S.runErr.textContent=r.error||"创建失败"; return}
S.runOk.textContent="已创建并运行："+r.task;
refresh();
}

// 币种下拉选项（观察列表；选中值不在列表里时保留）
function fillSymbolSelects(){
const list=(MKT&&MKT.watchlist&&MKT.watchlist.length)?MKT.watchlist:DEFAULT_WATCHLIST;
document.querySelectorAll("select.symSel").forEach(el=>{
const cur=el.value;
const vals=list.slice();
if(cur&&vals.indexOf(cur)<0) vals.unshift(cur);
el.innerHTML=vals.map(s=>{
const row=MKT&&MKT.symbols&&MKT.symbols.find(x=>x.symbol===s);
const cnt=row&&row.listed_venues?row.listed_venues.length:null;
return '<option value="'+esc(s)+'">'+esc(s)+(cnt==null?"":"（"+cnt+"/7 所上市）")+"</option>"}).join("");
if(cur) el.value=cur;
});
}

// ---------- 数据页（全表与排行，保持原功能） ----------
const KIND_CN={predicted:"下期预测",current:"当期",last_settled:"上期已结算",unknown:"未知"};
function showView(v){
document.querySelectorAll(".mktview").forEach(x=>x.style.display="none");
$("#view-"+v).style.display="block";
document.querySelectorAll("#viewBtns button").forEach(b=>b.classList.toggle("on",b.dataset.view===v));
}
document.querySelectorAll("#viewBtns button").forEach(b=>b.addEventListener("click",()=>showView(b.dataset.view)));
function prefillBtn(strategy,params,label){
const pf=esc(JSON.stringify({strategy:strategy,params:params}));
return '<button class="mini" data-act="newtask" data-prefill="'+pf+'">'+(label||"用此标的新建任务")+"</button>";
}
async function loadMarket(force){
try{
const d=await (await fetch("/api/market"+(force?"?force=1":""))).json();
MKT=d;
$("#mktTime").textContent=new Date(d.updated_at*1000).toLocaleString();
const firstClean=d.funding_ranking.findIndex(r=>!r.mixed);
$("#spreadTbl tbody").innerHTML=d.spread_ranking.map((r,i)=>
'<tr'+(i===0?' class="best"':"")+'><td>'+esc(r.symbol)+"</td><td>"+esc(r.buy_venue)+"</td><td>"+esc(r.sell_venue)+
"</td><td>"+fmt(r.spread_bps,2)+"</td><td>"+(i===0?"当前最优":"")+"</td><td>"+
prefillBtn("premium_arb",{base_venue:r.buy_venue,hedge_venue:r.sell_venue,symbol:r.symbol})+"</td></tr>").join("")
|| '<tr><td colspan="6" class="muted">暂无足够数据（至少要两个所有价格）</td></tr>';
$("#fundTbl tbody").innerHTML=d.funding_ranking.map((r,i)=>
'<tr'+(i===firstClean?' class="best"':"")+'><td>'+esc(r.symbol)+"</td><td>"+esc(r.long_venue)+"</td><td>"+esc(r.short_venue)+
"</td><td>"+fmt(r.edge_bps_h,3)+"</td><td>"+(r.mixed?'<span class="bad">混口径（仅参考）</span>':esc(KIND_CN[r.rate_kind]||r.rate_kind))+
"</td><td>"+(i===firstClean?"当前最优 ":"")+
prefillBtn("funding_carry",{venue_a:r.long_venue,venue_b:r.short_venue,symbol:r.symbol},"建资金费套利")+" "+
prefillBtn("rotation_hedge",{venue_a:r.long_venue,venue_b:r.short_venue,symbol:r.symbol},"建轮换对冲")+"</td></tr>").join("")
|| '<tr><td colspan="6" class="muted">暂无足够数据</td></tr>';
const vids=["arcus","lighter","lighter_rh","hyperliquid","entropy","tradexyz","aster"];
$("#mktMatrix").innerHTML='<table><thead><tr><th>币种</th>'+vids.map(v=>"<th>"+v+"</th>").join("")+"<th>最大价差</th><th>最优费差</th></tr></thead><tbody>"+
d.symbols.map(s=>"<tr><td><b>"+esc(s.symbol)+"</b></td>"+vids.map(v=>{
const c=s.venues[v];
if(!c||c.status==="not_listed")return '<td class="na">未上市</td>';
if(c.status==="error")return '<td class="na" title="'+esc(c.reason||"数据暂时取不到")+'">数据异常</td>';
return "<td>"+(c.price==null?"-":fmt(c.price,c.price<10?4:2))+"<br><span class='muted'>"+
(c.funding_bps_h==null?"费率 -":fmt(c.funding_bps_h,3)+" bps/h · "+esc(KIND_CN[c.rate_kind]||c.rate_kind||""))+"</span></td>"}).join("")+
"<td>"+(s.best_spread?fmt(s.best_spread.spread_bps,2)+" bps<br><span class='muted'>"+esc(s.best_spread.buy_venue)+" → "+esc(s.best_spread.sell_venue)+"</span>":"-")+"</td>"+
"<td>"+(s.best_funding?fmt(s.best_funding.edge_bps_h,3)+" bps/h<br><span class='muted'>多 "+esc(s.best_funding.long_venue)+" / 空 "+esc(s.best_funding.short_venue)+(s.best_funding.mixed?" · 混口径":"")+"</span>":"-")+"</td></tr>").join("")+
"</tbody></table>"+(Object.keys(d.venue_errors||{}).length?'<p class="bad">暂时取不到数据的场所：'+
Object.entries(d.venue_errors).map(e=>esc(e[0])).join("、")+"（已在表中标为「数据异常」，不影响其他场所）</p>":"");
const gs=$("#gridSym");
if(!gs.options.length){gs.innerHTML=d.watchlist.map(s=>'<option value="'+esc(s)+'">'+esc(s)+"</option>").join("")}
fillSymbolSelects();
SPECS.forEach(spec=>{applyListingGuards(spec.strategy); renderOpp(spec.strategy)});
}catch(e){$("#mktTime").textContent="加载失败，请稍后重试"}
}
$("#mktRefresh").addEventListener("click",()=>loadMarket(true));
$("#gridGo").addEventListener("click",async()=>{
const v=$("#gridVenue").value,s=$("#gridSym").value;
$("#gridOut").innerHTML="查询中…";
const d=await (await fetch("/api/market/candles?venue="+encodeURIComponent(v)+"&symbol="+encodeURIComponent(s))).json();
if(!d.ok){$("#gridOut").innerHTML='<p class="bad">'+esc(d.error||"不可用")+"</p>";return}
$("#gridOut").innerHTML="<p>"+esc(s)+" 近 24 小时：最高 <b>"+fmt(d.high,d.high<10?4:2)+"</b>，最低 <b>"+fmt(d.low,d.low<10?4:2)+
"</b>。建议网格区间（高低各扩 2%）：<b>"+fmt(d.suggested_lower,d.suggested_lower<10?4:2)+" ~ "+fmt(d.suggested_upper,d.suggested_upper<10?4:2)+"</b></p>"+
prefillBtn("hedged_grid",{grid_venue:v,symbol:s,lower_price:d.suggested_lower,upper_price:d.suggested_upper});
});
setInterval(()=>{if(CURPAGE==="market"||CURPAGE.indexOf("strat-")===0)loadMarket(false)},30000);

// ---------- 设置页（密钥） ----------
const GUIDES={
hyperliquid:"<b>怎么获取</b>（入口：app.hyperliquid.xyz）：<br>1. 打开 Hyperliquid 网页端并连接钱包；<br>2. 右上角菜单 More → API（或直接打开 app.hyperliquid.xyz/API）；<br>3. 生成 API 钱包（agent wallet），页面会显示它的地址和私钥；<br>4. 主账户地址填你的钱包主地址，私钥填刚生成的 agent 钱包私钥——agent 只能交易、不能提现，泄露风险比主私钥小，但仍要保密。<br>Entropy、trade.xyz 是同一套基础设施的子所，共用这套密钥；trade.xyz 想用独立账户，再填下面两个 _XYZ 字段。",
arcus:"<b>怎么获取</b>（入口：arcus.xyz，接口域名 api.arcus.xyz）：<br>1. 打开 Arcus 官网并登录/连接钱包；<br>2. 进账户的 API 设置页，创建 API 密钥，页面会给出公钥与私钥；<br>3. 私钥只显示一次，当场保存；<br>4. 地址填钱包地址，子账户序号填该 API 所属子账户的序号（主账户一般为 0）。",
lighter:"<b>怎么获取</b>（入口：lighter.xyz）：<br>1. 打开 Lighter 官网并登录；<br>2. 进个人中心的 API 管理（API Keys），创建新的 API Key；<br>3. 记下三样：账户序号（Account Index）、API Key 序号（API Key Index）、私钥——私钥只显示一次。<br>主网与 Robinhood 链是两套独立账户，密钥不要混填。",
lighter_rh:"<b>怎么获取</b>（入口：lighter.xyz 的 Robinhood 链 / rh.lighter.xyz）：<br>1. 在 Lighter 官网切换到 Robinhood 链（或打开 rh.lighter.xyz）并登录；<br>2. 同样在 API 管理里创建 API Key，记下账户序号、API Key 序号与私钥；<br>3. 这套密钥与主网完全独立，不要混填。",
aster:"<b>怎么获取</b>（入口：asterdex.com）：<br>1. 打开 Aster 官网并登录；<br>2. 右上角头像 → API 管理，创建 API；<br>3. 保存 API Key 与 Secret Key——Secret 只显示一次，丢了只能删掉重建。"
};
async function loadSecrets(){
const d=await (await fetch("/api/settings/secrets")).json();
SECLOADED=true;
if(d.error){$("#secGroups").innerHTML='<p class="bad">'+esc(d.error)+"</p>";return}
$("#secGroups").innerHTML=d.groups.map(g=>
'<fieldset class="sec" style="border:1px solid #2a3247;border-radius:4px;margin:10px 0"><legend>'+esc(g.title)+"</legend>"+
(GUIDES[g.key]?'<details><summary>怎么获取？</summary><div>'+GUIDES[g.key]+"</div></details>":"")+
g.fields.map(f=>'<label class="fld">'+esc(f.label)+
(f.required===true?' <span class="bad">*</span>':"")+(f.required==="one_of"?' <span class="muted">(二选一必填)</span>':"")+
' <span class="'+(f.filled?"ok":"bad")+'">'+(f.filled?"已设置":"未设置")+'</span>'+
'<input type="'+(f.secret?"password":"text")+'" data-sec="'+esc(f.name)+'" autocomplete="off" placeholder="'+
(f.filled?"已设置，留空不修改":"未设置，在此填写")+'"></label>').join("")+"</fieldset>").join("");
}
$("#secSave").addEventListener("click",async()=>{
const updates={};
document.querySelectorAll("[data-sec]").forEach(el=>{const v=el.value.trim();if(v)updates[el.dataset.sec]=v});
if(!Object.keys(updates).length){$("#secMsg").textContent="没有要保存的新值（输入框都是空的）";return}
const r=await postJson("/api/settings/secrets",updates);
if(!r.ok){$("#secMsg").textContent="保存失败："+(r.error||"");return}
$("#secMsg").textContent="已保存字段："+r.changed.join("、")+"。需重启程序生效。";
loadSecrets();
});

loadSpecs().then(refresh); setInterval(refresh, 5000);
</script>
</body>
</html>
"""
