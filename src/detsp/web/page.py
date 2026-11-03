"""面板单页 HTML（内嵌，无外部资源依赖）。

页面分五个标签：总览（场所权益/持仓/挂单、今日成交量与手续费、
未实现盈亏、KillSwitch 状态）、任务（新建/编辑/删除/启停）、
数据（公开行情扫描：7 所价格/资金费与按策略的机会排行，点策略
卡看对应视图，排行行可一键预填新建任务）、账本（最近成交）、
设置（密钥填写状态与写回 .env，只报已填/未填、绝不回显值）。
任务表单由后端 /api/strategy-specs 的参数规格动态生成（与 config
校验同一份规格，见 strategies/specs.py）；配置文件里的任务在面板
只可启停，面板任务才可删改，界面上如实标注来源。
所有数据来自 /api/* JSON 接口，前端定时刷新。
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
#modal fieldset{border:1px solid #2a3247;margin:10px 0;border-radius:4px}
#modal legend{color:#9aa4b8;font-size:12px;padding:0 6px}
.fld{display:block;margin:7px 0;font-size:13px}
.fld input[type=number],.fld input[type=text],.fld select,#mName{width:100%;box-sizing:border-box;background:#0f1420;color:#e6e9ef;border:1px solid #2a3247;border-radius:4px;padding:5px 8px;margin-top:3px}
#modal select{background:#0f1420;color:#e6e9ef;border:1px solid #2a3247;border-radius:4px;padding:5px 8px}
#mErr{white-space:pre-wrap}
.tabs{margin:14px 0 4px}
.tabs button{background:#1a2133;margin-right:6px}
.tabs button.on{background:#2456a6}
.tabpage{display:none}.tabpage.on{display:block}
.cards{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
.card{background:#1a2133;border:1px solid #2a3247;border-radius:6px;padding:10px 14px;cursor:pointer;min-width:150px}
.card.on{border-color:#2456a6;background:#1d2a47}
.card b{display:block;font-size:14px;margin-bottom:4px}
.card span{font-size:12px;color:#9aa4b8}
tr.best td{background:#1d2f1d}
td.na{color:#5c6578}
details{margin:6px 0}
details summary{cursor:pointer;color:#9aa4b8;font-size:13px}
details div{font-size:13px;padding:6px 4px;line-height:1.7}
.sec input{background:#0f1420;color:#e6e9ef;border:1px solid #2a3247;border-radius:4px;padding:5px 8px;width:100%;box-sizing:border-box;margin-top:3px}
</style>
</head>
<body>
<h1>DeTSP 交易面板 <span class="muted" id="mode"></span></h1>
<p class="warn">⚠ 本面板只允许绑本机 127.0.0.1 使用，POST 操作带随机
token 校验（防跨站借道），但<b>不等于登录鉴权</b>：不要改绑公网地址、
不要做端口映射。</p>

<div class="tabs">
<button data-tab="overview" class="on">总览</button>
<button data-tab="tasks">任务</button>
<button data-tab="market">数据</button>
<button data-tab="fills">账本</button>
<button data-tab="settings">设置</button>
</div>

<div class="tabpage on" id="tab-overview">
<h2>总览</h2>
<div id="killbox"></div>
<p>今日成交量：<b id="tvol">-</b>　今日手续费：<b id="tfee">-</b>　
今日成交笔数：<b id="tcnt">-</b>　持仓未实现盈亏（估算）：<b id="upnl">-</b></p>
<table id="venues"><thead><tr>
<th>场所</th><th>模式</th><th>权益</th><th>可用</th><th>持仓</th><th>挂单数</th>
</tr></thead><tbody></tbody></table>
<div id="events"></div>
</div>

<div class="tabpage" id="tab-tasks">
<h2>任务 <button id="newBtn">＋ 新建任务</button></h2>
<p class="muted">来源为「面板」的任务可编辑/删除；来源为「配置文件」的任务只能启停，
要改参数请改 config.yaml 后重启。场所的 paper/live 模式不在面板切换（安全边界）；
不知道选什么标的，先去「数据」页看机会排行。</p>
<table id="tasks"><thead><tr>
<th>名称</th><th>策略</th><th>来源</th><th>状态</th><th>最近错误</th><th>参数</th><th>操作</th>
</tr></thead><tbody></tbody></table>
</div>

<div class="tabpage" id="tab-market">
<h2>数据（公开行情，不用密钥）</h2>
<p class="muted">点一个策略，下面就是该策略要看的数据；排行按机会大小排，
第一名标「当前最优」。每行可一键新建任务（场所/币种自动预填，参数仍要核对）。
资金费已统一换算为每小时 bps；标「混口径」的组合只是参考——策略选向时
会拒绝用不同时点口径的费率相减开仓。<br>
数据时间：<b id="mktTime">-</b>　<button class="mini" id="mktRefresh">刷新</button>
<span class="muted">（打开本页时每 30 秒自动刷新）</span></p>
<div class="cards" id="stratCards"></div>
<div id="view-spread" class="mktview">
<h3>价差排行（溢价套利：在便宜的所买、贵的所卖）</h3>
<table id="spreadTbl"><thead><tr>
<th>币种</th><th>买入（低价所）</th><th>卖出（高价所）</th><th>价差 bps</th><th></th><th>操作</th>
</tr></thead><tbody></tbody></table>
</div>
<div id="view-funding" class="mktview" style="display:none">
<h3>资金费差排行（carry / 轮换对冲：多费率低的所、空费率高的所）</h3>
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
</div>

<div class="tabpage" id="tab-fills">
<h2>账本（最近成交）</h2>
<table id="fills"><thead><tr>
<th>时间</th><th>场所</th><th>币种</th><th>策略</th><th>方向</th>
<th>数量</th><th>价格</th><th>手续费</th><th>订单号</th>
</tr></thead><tbody></tbody></table>
</div>

<div class="tabpage" id="tab-settings">
<h2>设置（密钥）</h2>
<p class="muted">这里只填/改交易所密钥：已填的只显示「已设置」，<b>绝不回显任何密钥值</b>；
输入框留空 = 不修改。保存后写回 .env 文件，<b>需重启程序才生效</b>。
只填 API 钱包/签名私钥，永远不要填主钱包私钥或助记词。</p>
<div id="secGroups"></div>
<p><button id="secSave">保存密钥设置</button>
<span id="secMsg" class="ok"></span></p>
</div>

<div id="overlay"><div id="modal">
<h3 id="mTitle">新建任务</h3>
<p class="fld">策略
<select id="mStrategy"></select></p>
<p class="muted" id="mBlurb"></p>
<p class="fld" id="mNameRow">任务名（唯一，建后不可改名）
<input type="text" id="mName" placeholder="如 carry-btc-1"></p>
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
let SPECS=[], VENUES=[], TASKS=[], EDITING=null;
function esc(x){return String(x==null?"":x).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function fmt(x,d=2){return x==null?"-":Number(x).toFixed(d)}
async function post(url){const r=await fetch(url,{method:"POST",headers:{"X-API-Token":TOKEN}});return r.json()}
async function postJson(url,obj){const r=await fetch(url,{method:"POST",headers:{"X-API-Token":TOKEN,"Content-Type":"application/json"},body:JSON.stringify(obj)});return r.json()}

async function loadSpecs(){
const d=await (await fetch("/api/strategy-specs")).json();
SPECS=d.strategies||[]; VENUES=d.venues||[];
$("#mStrategy").innerHTML=SPECS.map(s=>'<option value="'+s.strategy+'">'+esc(s.title)+"（"+s.strategy+"）</option>").join("");
}

function specOf(st){return SPECS.find(s=>s.strategy===st)}
function buildForm(st, values){
const spec=specOf(st); if(!spec){$("#mForm").innerHTML="";return}
$("#mBlurb").textContent=spec.blurb||"";
const groups={};
spec.fields.forEach(f=>{(groups[f.group]=groups[f.group]||[]).push(f)});
$("#mForm").innerHTML=Object.keys(groups).map(g=>
"<fieldset><legend>"+esc(g)+"</legend>"+groups[g].map(f=>{
const v=(values&&values[f.name]!==undefined)?values[f.name]:f.default;
if(f.type==="bool")
return '<label class="fld"><input type="checkbox" data-f="'+f.name+'"'+(v?" checked":"")+"> "+esc(f.label)+"</label>";
if(f.type==="enum")
return '<label class="fld">'+esc(f.label)+'<select data-f="'+f.name+'">'+f.options.map(o=>'<option value="'+esc(o.value)+'"'+(o.value===v?" selected":"")+">"+esc(o.label)+"</option>").join("")+"</select></label>";
if(f.type==="venue")
return '<label class="fld">'+esc(f.label)+'<select data-f="'+f.name+'">'+VENUES.map(x=>'<option value="'+esc(x)+'"'+(x===v?" selected":"")+">"+esc(x)+"</option>").join("")+"</select></label>";
const t=(f.type==="int"||f.type==="float")?"number":"text";
const step=f.type==="int"?"1":"any";
return '<label class="fld">'+esc(f.label)+(f.required?" *":"")+'<input type="'+t+'" step="'+step+'" data-f="'+f.name+'" value="'+(v==null?"":esc(v))+'"></label>';
}).join("")+"</fieldset>").join("");
}

function collectParams(st){
const spec=specOf(st), out={};
for(const f of spec.fields){
const el=$('#mForm [data-f="'+f.name+'"]'); if(!el) continue;
if(f.type==="bool"){out[f.name]=el.checked;continue}
if(f.type==="enum"||f.type==="venue"||f.type==="str"){
const v=el.value.trim(); if(v!=="")out[f.name]=v; continue}
const v=el.value.trim();
if(v===""){continue}
out[f.name]=Number(v);
}
return out;
}

function applyPrefill(params){
if(!params) return;
for(const k in params){
const el=$('#mForm [data-f="'+k+'"]'); if(!el) continue;
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
if(prefill&&prefill.params) applyPrefill(prefill.params);
$("#mErr").textContent="";
$("#overlay").style.display="block";
}
function closeModal(){$("#overlay").style.display="none"}

$("#newBtn").addEventListener("click",()=>openModal(null));
$("#mCancel").addEventListener("click",closeModal);
$("#mStrategy").addEventListener("change",e=>buildForm(e.target.value,null));
$("#mSave").addEventListener("click",async()=>{
const st=$("#mStrategy").value;
const params=collectParams(st);
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
:("⚠ 手动平仓确认："+v+" "+s+" 没有任务归属，将平掉该场所该币种的（含手工仓位）。确认平仓？");
if(confirm(msg)){
const r=await postJson("/api/positions/close",{venue_id:v,symbol:s});
alert(r.ok?r.note:(r.error||"平仓失败"));
refresh();
}
}
else if(act==="newtask"){
let pf=null;
try{pf=JSON.parse(b.dataset.prefill||"null")}catch(e){}
switchTab("tasks");
openModal(null, pf);
}
});

async function refresh(){
const ov=await (await fetch("/api/overview")).json();
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
$("#tasks tbody").innerHTML=TASKS.map(t=>{
let btns="";
if(t.status==="running")btns+='<button class="mini stop" data-act="stop" data-name="'+esc(t.name)+'">停止</button>';
else if(t.status==="stopped"||t.status==="error")btns+='<button class="mini" data-act="start" data-name="'+esc(t.name)+'">启动</button>';
if(t.source==="panel"){
btns+='<button class="mini" data-act="edit" data-name="'+esc(t.name)+'">编辑</button>';
btns+='<button class="mini stop" data-act="del" data-name="'+esc(t.name)+'">删除</button>';
}
return "<tr><td>"+esc(t.name)+"</td><td>"+esc(t.strategy)+"</td><td>"+
(t.source==="panel"?"面板":"配置文件")+"</td><td>"+esc(t.status)+
"</td><td>"+esc(t.last_error||"")+"</td><td><pre>"+esc(JSON.stringify(t.params,null,1))+
"</pre></td><td>"+btns+"</td></tr>"}).join("");
const fl=await (await fetch("/api/fills?limit=50")).json();
$("#fills tbody").innerHTML=fl.fills.map(f=>"<tr><td>"+new Date(f.ts*1000).toLocaleString()+
"</td><td>"+esc(f.venue)+"</td><td>"+esc(f.symbol)+"</td><td>"+esc(f.strategy)+"</td><td>"+esc(f.side)+
"</td><td>"+f.qty+"</td><td>"+f.price+"</td><td>"+f.fee+"</td><td>"+esc(f.order_id)+"</td></tr>").join("");
}
// ---------- 标签页 ----------
let CURTAB="overview", MKT=null, MKTLOADED=false, SECLOADED=false;
function switchTab(name){
CURTAB=name;
document.querySelectorAll(".tabs button").forEach(b=>b.classList.toggle("on",b.dataset.tab===name));
document.querySelectorAll(".tabpage").forEach(p=>p.classList.toggle("on",p.id==="tab-"+name));
if(name==="market"){loadMarket(false)}
if(name==="settings"&&!SECLOADED){loadSecrets()}
}
document.querySelectorAll(".tabs button").forEach(b=>b.addEventListener("click",()=>switchTab(b.dataset.tab)));

// ---------- 数据页 ----------
const STRAT_VIEWS={premium_arb:"spread",funding_carry:"funding",rotation_hedge:"funding",hedged_grid:"grid"};
const STRAT_CARDS=[
["premium_arb","溢价套利","看价差排行：在便宜的所买、贵的所卖"],
["funding_carry","资金费 carry","看资金费差排行：多低费率、空高费率"],
["rotation_hedge","轮换对冲刷量","同看资金费差排行（选向口径相同）"],
["hedged_grid","中性网格","选币看近 24 小时高低定区间"]];
let CURVIEW="spread";
function showView(v){
CURVIEW=v;
document.querySelectorAll(".mktview").forEach(x=>x.style.display="none");
$("#view-"+v).style.display="block";
document.querySelectorAll("#stratCards .card").forEach(c=>c.classList.toggle("on",STRAT_VIEWS[c.dataset.st]===v));
}
$("#stratCards").innerHTML=STRAT_CARDS.map(c=>'<div class="card" data-st="'+c[0]+'"><b>'+c[1]+'</b><span>'+c[2]+"</span></div>").join("");
document.querySelectorAll("#stratCards .card").forEach(c=>c.addEventListener("click",()=>showView(STRAT_VIEWS[c.dataset.st])));
function prefillBtn(strategy,params,label){
const pf=esc(JSON.stringify({strategy:strategy,params:params}));
return '<button class="mini" data-act="newtask" data-prefill="'+pf+'">'+(label||"用此标的新建任务")+"</button>";
}
const KIND_CN={predicted:"下期预测",current:"当期",last_settled:"上期已结算",unknown:"未知"};
async function loadMarket(force){
try{
const d=await (await fetch("/api/market"+(force?"?force=1":""))).json();
MKT=d; MKTLOADED=true;
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
prefillBtn("funding_carry",{venue_a:r.long_venue,venue_b:r.short_venue,symbol:r.symbol},"建 carry")+" "+
prefillBtn("rotation_hedge",{venue_a:r.long_venue,venue_b:r.short_venue,symbol:r.symbol},"建轮换对冲")+"</td></tr>").join("")
|| '<tr><td colspan="6" class="muted">暂无足够数据</td></tr>';
const vids=["arcus","lighter","lighter_rh","hyperliquid","entropy","tradexyz","aster"];
$("#mktMatrix").innerHTML='<table><thead><tr><th>币种</th>'+vids.map(v=>"<th>"+v+"</th>").join("")+"<th>最大价差</th><th>最优费差</th></tr></thead><tbody>"+
d.symbols.map(s=>"<tr><td><b>"+esc(s.symbol)+"</b></td>"+vids.map(v=>{
const c=s.venues[v];
if(!c)return '<td class="na">无数据</td>';
return "<td>"+(c.price==null?"-":fmt(c.price,c.price<10?4:2))+"<br><span class='muted'>"+
(c.funding_bps_h==null?"费率 -":fmt(c.funding_bps_h,3)+" bps/h · "+esc(KIND_CN[c.rate_kind]||c.rate_kind||""))+"</span></td>"}).join("")+
"<td>"+(s.best_spread?fmt(s.best_spread.spread_bps,2)+" bps<br><span class='muted'>"+esc(s.best_spread.buy_venue)+" → "+esc(s.best_spread.sell_venue)+"</span>":"-")+"</td>"+
"<td>"+(s.best_funding?fmt(s.best_funding.edge_bps_h,3)+" bps/h<br><span class='muted'>多 "+esc(s.best_funding.long_venue)+" / 空 "+esc(s.best_funding.short_venue)+(s.best_funding.mixed?" · 混口径":"")+"</span>":"-")+"</td></tr>").join("")+
"</tbody></table>"+(Object.keys(d.venue_errors||{}).length?'<p class="bad">取数失败的场所：'+
Object.entries(d.venue_errors).map(e=>esc(e[0])).join("、")+"（已标无数据，不影响其他场所）</p>":"");
const gs=$("#gridSym");
if(!gs.options.length){gs.innerHTML=d.watchlist.map(s=>'<option value="'+esc(s)+'">'+esc(s)+"</option>").join("")}
}catch(e){$("#mktTime").textContent="加载失败："+e}
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
setInterval(()=>{if(CURTAB==="market")loadMarket(false)},30000);

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
