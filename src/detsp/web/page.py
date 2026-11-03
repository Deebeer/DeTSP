"""面板单页 HTML（内嵌，无外部资源依赖）。

页面分三区：总览（场所权益/持仓/挂单、今日成交量与手续费、未实现盈亏、
KillSwitch 状态）、任务（新建/编辑/删除/启停）、账本（最近成交）。
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
</style>
</head>
<body>
<h1>DeTSP 交易面板 <span class="muted" id="mode"></span></h1>
<p class="warn">⚠ 本面板只允许绑本机 127.0.0.1 使用，POST 操作带随机
token 校验（防跨站借道），但<b>不等于登录鉴权</b>：不要改绑公网地址、
不要做端口映射。</p>

<h2>总览</h2>
<div id="killbox"></div>
<p>今日成交量：<b id="tvol">-</b>　今日手续费：<b id="tfee">-</b>　
今日成交笔数：<b id="tcnt">-</b>　持仓未实现盈亏（估算）：<b id="upnl">-</b></p>
<table id="venues"><thead><tr>
<th>场所</th><th>模式</th><th>权益</th><th>可用</th><th>持仓</th><th>挂单数</th>
</tr></thead><tbody></tbody></table>
<div id="events"></div>

<h2>任务 <button id="newBtn">＋ 新建任务</button></h2>
<p class="muted">来源为「面板」的任务可编辑/删除；来源为「配置文件」的任务只能启停，
要改参数请改 config.yaml 后重启。场所的 paper/live 模式与密钥不在面板操作（安全边界）。</p>
<table id="tasks"><thead><tr>
<th>名称</th><th>策略</th><th>来源</th><th>状态</th><th>最近错误</th><th>参数</th><th>操作</th>
</tr></thead><tbody></tbody></table>

<h2>账本（最近成交）</h2>
<table id="fills"><thead><tr>
<th>时间</th><th>场所</th><th>币种</th><th>策略</th><th>方向</th>
<th>数量</th><th>价格</th><th>手续费</th><th>订单号</th>
</tr></thead><tbody></tbody></table>

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

function openModal(editName){
EDITING=editName||null;
const t=editName?TASKS.find(x=>x.name===editName):null;
$("#mTitle").textContent=t?("编辑任务："+t.name):"新建任务";
$("#mNameRow").style.display=t?"none":"block";
$("#mName").value="";
const st=t?t.strategy:(SPECS[0]&&SPECS[0].strategy);
$("#mStrategy").value=st;
$("#mStrategy").disabled=!!t;
buildForm(st, t?(t.params.params||{}):null);
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
loadSpecs().then(refresh); setInterval(refresh, 5000);
</script>
</body>
</html>
"""
