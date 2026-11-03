"""面板单页 HTML（内嵌，无外部资源依赖）。

页面分三区：总览（场所权益/持仓/挂单、今日成交量与手续费、未实现盈亏、
KillSwitch 状态）、任务（列表/启停/参数只读）、账本（最近成交）。
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
th,td{border:1px solid #2a3247;padding:5px 8px;font-size:13px;text-align:left}
th{background:#1a2133}
button{background:#2456a6;color:#fff;border:0;padding:5px 12px;border-radius:4px;cursor:pointer}
button.stop{background:#8a3b3b} button.kill{background:#b02020;font-weight:bold}
.warn{background:#5a1f1f;border:1px solid #b02020;padding:8px 12px;border-radius:4px}
.ok{color:#7dd87d}.bad{color:#ff8080}.muted{color:#9aa4b8}
pre{background:#161c2b;padding:8px;overflow:auto;font-size:12px}
</style>
</head>
<body>
<h1>DeTSP 交易面板 <span class="muted" id="mode"></span></h1>
<p class="warn">⚠ 本面板<b>没有任何登录鉴权</b>，只允许绑本机 127.0.0.1 使用，
不要改绑公网地址、不要做端口映射。</p>

<h2>总览</h2>
<div id="killbox"></div>
<p>今日成交量：<b id="tvol">-</b>　今日手续费：<b id="tfee">-</b>　
今日成交笔数：<b id="tcnt">-</b>　持仓未实现盈亏（估算）：<b id="upnl">-</b></p>
<table id="venues"><thead><tr>
<th>场所</th><th>模式</th><th>权益</th><th>可用</th><th>持仓</th><th>挂单数</th>
</tr></thead><tbody></tbody></table>

<h2>任务</h2>
<table id="tasks"><thead><tr>
<th>名称</th><th>策略</th><th>状态</th><th>最近错误</th><th>参数（只读）</th><th>操作</th>
</tr></thead><tbody></tbody></table>

<h2>账本（最近成交）</h2>
<table id="fills"><thead><tr>
<th>时间</th><th>场所</th><th>币种</th><th>策略</th><th>方向</th>
<th>数量</th><th>价格</th><th>手续费</th><th>订单号</th>
</tr></thead><tbody></tbody></table>

<script>
const $=s=>document.querySelector(s);
function fmt(x,d=2){return x==null?"-":Number(x).toFixed(d)}
async function post(url){const r=await fetch(url,{method:"POST"});return r.json()}
async function refresh(){
  const ov=await (await fetch("/api/overview")).json();
  $("#mode").textContent="（"+(ov.dry_run?"DRY_RUN 只决策不下单":"实盘模式")+"）";
  const ks=ov.killswitch;
  $("#killbox").innerHTML=ks.halted
    ? '<p class="bad">⛔ 全局停机中：'+ks.reason+' <button onclick="post(\\'/api/killswitch/reset\\').then(refresh)">解除停机</button></p>'
    : '<p class="ok">运行中</p><button class="kill" onclick="if(confirm(\\'确认触发全局停机？所有任务将停止开仓。\\'))post(\\'/api/killswitch\\').then(refresh)">⛔ 全局停机</button>';
  $("#tvol").textContent=fmt(ov.today.volume);
  $("#tfee").textContent=fmt(ov.today.fees,4);
  $("#tcnt").textContent=ov.today.fills;
  $("#upnl").textContent=fmt(ov.unrealized_pnl);
  $("#venues tbody").innerHTML=ov.venues.map(v=>"<tr><td>"+v.venue_id+"</td><td>"+v.mode+
    "</td><td>"+fmt(v.equity)+"</td><td>"+fmt(v.available)+"</td><td>"+
    (v.positions.length?v.positions.map(p=>p.symbol+" "+p.qty+" @ "+p.entry_price).join("<br>"):"-")+
    (v.error?'<br><span class="bad">'+v.error+"</span>":"")+
    "</td><td>"+v.open_orders+"</td></tr>").join("");
  const tk=await (await fetch("/api/tasks")).json();
  $("#tasks tbody").innerHTML=tk.tasks.map(t=>{
    let btn="";
    if(t.status==="running")btn='<button class="stop" onclick="post(\\'/api/tasks/'+t.id+'/stop\\').then(refresh)">停止</button>';
    else if(t.status==="stopped")btn='<button onclick="post(\\'/api/tasks/'+t.id+'/start\\').then(refresh)">启动</button>';
    return "<tr><td>"+t.name+"</td><td>"+t.strategy+"</td><td>"+t.status+
      "</td><td>"+(t.last_error||"")+"</td><td><pre>"+JSON.stringify(t.params,null,1)+
      "</pre></td><td>"+btn+"</td></tr>"}).join("");
  const fl=await (await fetch("/api/fills?limit=50")).json();
  $("#fills tbody").innerHTML=fl.fills.map(f=>"<tr><td>"+new Date(f.ts*1000).toLocaleString()+
    "</td><td>"+f.venue+"</td><td>"+f.symbol+"</td><td>"+f.strategy+"</td><td>"+f.side+
    "</td><td>"+f.qty+"</td><td>"+f.price+"</td><td>"+f.fee+"</td><td>"+f.order_id+"</td></tr>").join("");
}
refresh(); setInterval(refresh, 5000);
</script>
</body>
</html>
"""
