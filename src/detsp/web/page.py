"""面板单页 HTML（内嵌，无外部资源依赖）。

布局（用户定版）：左侧窄边栏 + 主区域分页，不整页刷新。边栏自上
而下共 7 项：总览 / 中性网格 / 溢价套利 / 资金费套利 /
策略记录 / 数据库 / 设置——没有账本页，也没有通用新建
任务入口：新建任务只能从各策略页发起。「数据库」页（第十二
轮新增，在设置上面）放数据服务（中枢库 / 费差库的运行状态、
条目数、更新时间与启停）；设置页只留密钥。费率不再有可编辑
表：全所统一内置默认（taker 5 / maker 2 bps，core.fee_table
常量）。

总览（顺序定版）：页头三行——标题「总览」→ 安全警告（原文）
→ 运行状态与全局停机按钮同一行；其后是今日与累计两行统计
（成交量/手续费/笔数/盈亏估算，口径见 server.overview）→
盈亏口径小字 → 场所表（权益/可用/持仓/挂单数）→「最佳机会」
混合列表（/api/best 不带 strategy 参数时返回三策略各自
Top 5 合并、按机会值 % 降序，点行跳策略页并带入两腿与
标的）→ 系统事件。运行模式（模拟/实盘）只看场所表的模式列，
标题上不重复标注。
「策略记录」是独立页（/api/records：运行仓位按开始时间倒序、
仓位历史按平仓时间倒序，历史由后端追踪仓位消失落库）。

每个策略页同构，由通用渲染器按 /api/strategy-specs 的规格
（含 page 元数据：两腿字段、primary、内置中性档、运作方式
文案）生成，分区顺序统一为：页头（策略名 + 说明 + 「运作
方式」折叠）→ 运行模式 → 选择两腿 → 仓位设置 → 策略配置 →
更多配置（折叠）→ 交易预览。字段按 specs 的 field.zone
分区渲染（legs/position/config/more，hidden 不渲染；
derived_fields 组装派生、面板不渲染输入框）；specs 尚未带
zone 时回退旧渲染（配置区按五段 section 组织、再无 section
时走按 section 映射的语义分组），不白屏。
运行模式用 page.run_modes 的显示名渲染（中性网格只有「自主」
一项时不渲染切换器、只显示静态文字；溢价/资金费三档为
固腿固标 semi_fixed / 固腿优标 semi_auto / 优腿优标
full_auto；旧值 auto 按 semi_auto 处理；semi_auto 时仓位设置
的币种下拉禁用并注明由系统自动选标，full_auto 时隐藏选择
两腿、只留保证金 + 系统参数只读展示，提交带 run_mode 与
顶层 amount_usd（保证金），任务列表与策略记录的任务名带
模式徽标、自动选标类任务显示当前标的与已循环轮数）。
第十二轮定版：预设已全砍（优腿优标没填的参数按内置中性档
补，specs.NEUTRAL_DEFAULTS）。
选择两腿（第十三轮恢复）：场所下拉 + 方向徽标 + 每腿金额/
数量 + 每腿可用余额（权益）恢复到腿行（第十二轮曾被一并砍
掉）；统计/价格/标的数据/杠杆四样仍在仓位设置，不搬回。
仓位设置横排两列（第十三轮）：配对统计行整行在顶（「共同
N · A所上市 X · B所上市 Y」）→ 第一行 币种 | 保证金 → 空
一行 → 第二行 杠杆 | 杠杆（每腿输入框上方一行「场所名 ·
最高杠杆 · 保证金模式」，如 entropy · 15x · 逐仓 only，
资金费单杠杆占左列）→ 数据块两行整行在底（两腿价 + 价差 /
两腿资金费）。金额语义（第十二轮）：仓位设置填的是
保证金（USD），仓位名义 = 保证金 × 基准腿杠杆（网格为网格
腿杠杆），对冲腿开相同名义、其实际占用保证金 = 名义 ÷ 对冲
腿杠杆，在预览与保证金行旁列清；网格按总名义反推每格量。
溢价中枢/上带/下带接中枢库（/api/libraries）：自动选标类
模式下只读、无库条目时提示需录制满 3 小时；固腿固标可改、
有库值时自动填入且不冲掉用户已改的值。资金费仓位设置下方
有一行费差库信息（中位持续/平均费差/预计回本/样本）。交易
预览（按平台分列标记价/预估执行价/名义/占用保证金 + 入场
条件指示 + 所填杠杆超过某所该标的上限时的黄色警告）→ 运行
按钮 → 运行中仓位（本策略任务与持仓，含启停改删/平仓）。
运行中的全部仓位在策略页与策略记录页双处可见。

数据实时性：策略页打开期间 pairs 每 20 秒自动重拉（与扫描
缓存对齐），排序/预览/腿数量随新数据重算，已选标的与表单
不被冲掉；总览最佳机会同样 20 秒刷新，总览统计/场所与策略
记录随 5 秒 overview 节奏。各数据区块右上角有「更新
HH:MM:SS」小字（时间取自接口 updated_at）；刷新失败保留旧
数据并把时间戳标黄注明。各所各标的的最高杠杆不同、有的标
的仅逐仓：仓位设置的杠杆行逐腿显示该标的在该所的最高杠杆
与保证金模式（数据来自各所公开接口，拿不到显示未知），预览里所填
杠杆超上限会警告——本面板只展示与警告，程序不自动改杠杆
（既有口径）。

视觉：极客终端风——近黑底、直角、等宽字体、高密度表格、状态用
小圆点 + 大写英文小标签。颜色只用一套变量（--bg/--panel/
--line/--txt/--mut/--acc/--red/--yel 等）：说明文字统一暗灰，
强调只有终端绿，涨/多绿、跌/空红、警告黄，不用其他杂色。字段
呈现统一三段式：第一行字段名（必填带星号）、第二行说明
（specs 的 desc，暗灰小字）、第三行输入框；与原仓差异这类
长注保留在字段下方小字。设置（密钥只报已填/未填、绝不回显
值）保持原有功能。任务编辑仍走弹窗（与策略页共用 specs
动态表单渲染/收集函数）。配置文件里的任务在面板只可启停，
面板任务才可删改，界面上如实标注来源。
"""

PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>DeTSP 交易面板</title>
<style>
/* 全站唯一配色：正文主色 --txt、标题亮色 --bright、说明暗灰
--mut、次暗 --dim；强调只有终端绿 --acc；负/空 --red、正/多
--acc、警告 --yel。结构色（底/面/边框）也全部走变量，任何地方
不许再出现这套之外的杂色（旧主题的蓝色/圆角一类早已清除）。 */
:root{--bg:#0b0e11;--panel:#11161c;--panel2:#0d1117;--line:#232a32;--line2:#30363d;--txt:#c9d1d9;--bright:#e6edf3;--mut:#8b949e;--dim:#484f58;--acc:#3fb950;--red:#f85149;--redline:#5a2b2b;--yel:#d29922;--onacc:#06130a}
body{font-family:ui-monospace,"JetBrains Mono",Consolas,"Courier New",monospace;margin:0;background:var(--bg);color:var(--txt);font-size:13px;line-height:1.6;font-variant-numeric:tabular-nums}
h1{font-size:17px;color:var(--bright)} h2{font-size:15px;margin:22px 0 12px;color:var(--bright);letter-spacing:.03em}
h3{font-size:13px}
a{color:var(--acc)}
table{border-collapse:collapse;width:100%;margin:8px 0}
th,td{border:1px solid var(--line);padding:6px 10px;font-size:12px;text-align:left;vertical-align:top}
th{background:var(--panel2);color:var(--mut);text-transform:uppercase;letter-spacing:.06em;font-size:11px;font-weight:600}
tbody tr:nth-child(even){background:rgba(255,255,255,.016)}
button{background:transparent;color:var(--txt);border:1px solid var(--line2);padding:4px 12px;border-radius:0;cursor:pointer;font:inherit;font-size:12px}
button:hover{background:var(--txt);color:var(--bg);border-color:var(--txt)}
button.stop{border-color:var(--redline);color:var(--red)}
button.stop:hover{background:var(--red);border-color:var(--red);color:var(--bg)}
button.kill{border-color:var(--red);color:var(--red);font-weight:bold}
button.kill:hover{background:var(--red);color:var(--bg)}
button.mini{padding:1px 8px;font-size:11px;margin:2px 2px 2px 0}
.warn{background:rgba(248,81,73,.07);border:1px solid var(--redline);padding:8px 12px}
.ok{color:var(--acc)}.bad{color:var(--red)}.muted{color:var(--mut)}
pre{background:var(--panel2);border:1px solid var(--line);padding:8px;overflow:auto;font-size:12px;margin:0}
.tag{border:1px solid var(--line);padding:0 5px;font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;background:var(--dim);vertical-align:1px}
.dot.g{background:var(--acc)}.dot.r{background:var(--red)}.dot.y{background:var(--yel)}
.st{font-size:11px;letter-spacing:.06em;white-space:nowrap}
#overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:10}
#modal{background:var(--panel);border:1px solid var(--line);max-width:560px;margin:40px auto;padding:16px 20px;max-height:85vh;overflow:auto}
#modal h3{margin-top:0;color:var(--bright)}
fieldset{border:1px solid var(--line);margin:12px 0;padding:12px 14px}
fieldset legend{color:var(--mut);font-size:11px;padding:0 6px;text-transform:uppercase;letter-spacing:.05em}
.fld{display:block;margin:10px 0;font-size:12px}
.fname{display:block;color:var(--bright)}
.fdesc{display:block;color:var(--mut);font-size:11px;margin-top:1px;line-height:1.45}
.upd{float:right;font-size:11px;color:var(--mut);font-weight:normal;letter-spacing:0;text-transform:none}
.fld input[type=number],.fld input[type=text],.fld select,#mName{width:100%;box-sizing:border-box;background:var(--bg);color:var(--txt);border:1px solid var(--line);border-radius:0;padding:5px 8px;margin-top:3px;font:inherit}
.fhelp{font-size:11px;color:var(--mut);line-height:1.5;margin-top:2px}
#modal select{background:var(--bg);color:var(--txt);border:1px solid var(--line);border-radius:0;padding:5px 8px;font:inherit}
#mErr{white-space:pre-wrap}
#layout{display:flex;align-items:stretch}
#sidebar{width:168px;flex:0 0 168px;background:var(--panel2);border-right:1px solid var(--line);padding:14px 10px;position:sticky;top:0;height:100vh;display:flex;flex-direction:column;gap:2px}
#sidebar .sideTitle{font-size:14px;font-weight:bold;margin:0 4px 12px;color:var(--bright);letter-spacing:.05em}
#sidebar button{text-align:left;background:transparent;border:1px solid transparent;border-left:2px solid transparent;padding:6px 10px;font-size:13px;color:var(--mut)}
#sidebar button:hover{background:var(--panel);color:var(--txt)}
#sidebar button.on{color:var(--acc);border-left-color:var(--acc);background:rgba(63,185,80,.06)}
#sidebar .sideSep{border-top:1px solid var(--line);margin:8px 0}
#sidebar .sideNote{font-size:11px;color:var(--mut);line-height:1.6;margin-top:12px}
#main{flex:1;min-width:0;padding:8px 26px 40px}
.page{display:none}.page.on{display:block}
.block{border:1px solid var(--line);background:var(--panel);padding:16px 18px;margin:18px 0}
.block h3{margin:0 0 10px;font-size:12px;color:var(--bright);letter-spacing:.05em}
.block h3::before{content:"[ ";color:var(--acc)}
.block h3::after{content:" ]";color:var(--acc)}
.pform select,.aform select,.pform input,.aform input{background:var(--bg);color:var(--txt);border:1px solid var(--line);border-radius:0;padding:5px 8px;font:inherit}
.pform .fld input,.pform .fld select,.aform .fld input,.aform .fld select{width:100%;box-sizing:border-box;margin-top:3px}
.pform{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:0 16px}
.runBtn{background:var(--acc);color:var(--onacc);border-color:var(--acc);font-size:15px;padding:10px 30px;font-weight:bold;width:100%}
.runBtn:hover{background:transparent;color:var(--acc);border-color:var(--acc)}
.amtRow{display:flex;gap:14px;align-items:flex-end;flex-wrap:wrap;margin-bottom:12px}
.amtMain{flex:0 1 320px}
.amtMain .fld{font-size:13px;color:var(--mut)}
.amtMain input{font-size:19px;padding:7px 10px}
.modeRow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.modeRow button.on{background:var(--acc);color:var(--onacc);border-color:var(--acc);font-weight:bold}
.pickRow{cursor:pointer}
.pickRow:hover td{background:rgba(63,185,80,.08)}
.symWrap{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.symWrap select{background:var(--bg);color:var(--txt);border:1px solid var(--line);border-radius:0;padding:6px 10px;min-width:340px;font:inherit;font-size:13px}
.legRow{display:flex;gap:14px;align-items:center;flex-wrap:wrap;padding:10px 0;border-bottom:1px dashed var(--line)}
.legRow:last-child{border-bottom:none}
.legRow .legLab{flex:0 0 74px;color:var(--mut);font-size:12px}
.legRow select{background:var(--bg);color:var(--txt);border:1px solid var(--line);border-radius:0;padding:5px 8px;min-width:160px;font:inherit}
.legRow input.legAmt{width:130px;background:var(--bg);color:var(--mut);border:1px solid var(--line);border-radius:0;padding:5px 8px;font:inherit}
.legRow .legBal{flex-basis:100%;font-size:12px;color:var(--mut)}
.dirB{border:1px solid var(--line);padding:1px 8px;font-size:11px;white-space:nowrap}
.dirB.long{color:var(--acc);border-color:var(--acc)}
.dirB.short{color:var(--red);border-color:var(--red)}
.howBox ol{margin:4px 0 4px 18px;padding:0;line-height:1.8;font-size:12px}
.runErr{white-space:pre-wrap}
details{margin:6px 0}
details summary{cursor:pointer;color:var(--mut);font-size:12px}
details div{font-size:12px;padding:6px 4px;line-height:1.8}
.sec input{background:var(--bg);color:var(--txt);border:1px solid var(--line);border-radius:0;padding:5px 8px;width:100%;box-sizing:border-box;margin-top:3px;font:inherit}
.gridSug{margin-top:10px}
.levInfo{font-size:12px;color:var(--mut);margin:10px 0 0}
.posGrid{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}
.posGrid .symWrap select{min-width:0;width:100%}
.posCell{min-width:0}
.levGrid{margin-top:14px}
.posData{margin-top:12px}
.symData{margin-top:8px;font-size:12px;line-height:1.7}
.symData .sdT{color:var(--bright)}
.symData .sdLab{color:var(--mut)}
.stateRow{display:flex;gap:16px;align-items:center;margin:10px 0}
.advBox{margin-top:16px;border-top:1px dashed var(--line);padding-top:10px}
.cfgSec{margin:14px 0 4px;border-top:1px dashed var(--line);padding-top:10px}
.cfgSec h4{margin:0 0 2px;font-size:12px;color:var(--bright);letter-spacing:.05em}
details.cfgSec summary{cursor:pointer;color:var(--bright);font-size:12px;letter-spacing:.05em}
.secSummary{color:var(--mut);font-size:12px;margin:2px 0 6px;line-height:1.55}
.fullAutoCfg{margin-top:12px;border-top:1px dashed var(--line);padding-top:10px}
.faParams{line-height:1.9;font-size:12px}
</style>
</head>
<body>
<div id="layout">
<nav id="sidebar">
<div class="sideTitle">DeTSP 交易面板</div>
<button data-page="overview" class="on">总览</button>
<button data-page="strat-hedged_grid">中性网格</button>
<button data-page="strat-premium_arb">溢价套利</button>
<button data-page="strat-funding_carry">资金费套利</button>
<button data-page="records">策略记录</button>
<button data-page="database">数据库</button>
<div class="sideSep"></div>
<button data-page="settings">设置</button>
<p class="sideNote">新建任务只能从各策略页发起；
运行中仓位在各策略页与策略记录里都能看到。</p>
</nav>
<main id="main">

<section class="page on" id="page-overview">
<h2>总览 <span class="upd" id="ovUpd"></span></h2>
<p class="warn">⚠ 本面板只允许绑本机 127.0.0.1 使用，POST 操作带随机
token 校验（防跨站借道），但<b>不等于登录鉴权</b>：不要改绑公网地址、
不要做端口映射。</p>
<div id="killbox"></div>
<p>今日成交量：<b id="tvol">-</b>　今日手续费：<b id="tfee">-</b>　
今日成交笔数：<b id="tcnt">-</b>　今日盈亏（估算）：<b id="tpnl">-</b></p>
<p>累计成交量：<b id="avol">-</b>　累计手续费：<b id="afee">-</b>　
累计成交笔数：<b id="acnt">-</b>　累计盈亏（估算）：<b id="apnl">-</b></p>
<p class="muted" id="pnlNote"></p>
<table id="venues"><thead><tr>
<th>场所</th><th>模式</th><th>权益</th><th>可用</th><th>持仓</th><th>挂单数</th>
</tr></thead><tbody></tbody></table>
<h3>最佳机会 <span class="upd" id="bestUpd"></span></h3>
<p class="muted">三策略各自的当前最佳标的合并成一张表，按机会值
从大到小排列；点任意一行跳到对应策略页并带入两腿与标的。</p>
<table id="bestTbl"><thead><tr>
<th>策略</th><th>标的</th><th>做法</th><th>机会值（按此排序，口径不同）</th><th>操作</th>
</tr></thead><tbody></tbody></table>
<div id="events"></div>
</section>

<section class="page" id="page-records">
<h2>策略记录 <span class="upd" id="recUpd"></span></h2>
<p class="muted">运行仓位（按开始时间倒序）</p>
<table id="recOpen"><thead><tr>
<th>策略</th><th>任务</th><th>场所</th><th>币种</th><th>方向</th><th>数量</th><th>开仓价</th><th>操作</th>
</tr></thead><tbody></tbody></table>
<p class="muted">仓位历史（按平仓时间倒序）</p>
<table id="recClosed"><thead><tr>
<th>策略</th><th>任务</th><th>场所</th><th>币种</th><th>方向</th><th>数量</th><th>开仓价</th><th>平仓价</th><th>开仓时间</th><th>平仓时间</th>
</tr></thead><tbody></tbody></table>
</section>

<section class="page" id="page-strat-hedged_grid"></section>
<section class="page" id="page-strat-premium_arb"></section>
<section class="page" id="page-strat-funding_carry"></section>

<section class="page" id="page-settings">
<h2>设置（密钥）</h2>
<p class="muted">这里只填/改交易所密钥：已填的只显示「已设置」，<b>绝不回显任何密钥值</b>；
输入框留空 = 不修改。保存后写回 .env 文件，<b>需重启程序才生效</b>。
只填 API 钱包/签名私钥，永远不要填主钱包私钥或助记词。</p>
<div id="secGroups"></div>
<p><button id="secSave">保存密钥设置</button>
<span id="secMsg" class="ok"></span></p>
</section>

<section class="page" id="page-database">
<h2>数据库</h2>
<p class="muted">中枢库（溢价套利的中枢/入场带）与费差库（资金费的持续性
统计）共用同一条录制流水线：随面板启动自动录制，也可在这里手动启停。
费率不用在这里维护：全所统一内置默认（taker 5 / maker 2 bps）。</p>
<h3>数据服务</h3>
<div id="libStatus"><p class="muted">数据服务状态加载中…</p></div>
<p><button id="libStart">启动数据服务</button>
<button id="libStop">停止数据服务</button>
<span id="libMsg" class="ok"></span></p>
</section>

</main>
</div>

<div id="overlay"><div id="modal">
<h3 id="mTitle">编辑任务</h3>
<p class="fld"><span class="fname">策略</span>
<select id="mStrategy"></select></p>
<p class="muted" id="mBlurb"></p>
<p class="fld" id="mNameRow"><span class="fname">任务名</span><span class="fdesc">唯一，建后不可改名</span>
<input type="text" id="mName" placeholder="如 carry-BTC-093015"></p>
<div id="mForm"></div>
<p class="bad" id="mErr"></p>
<p><button id="mSave">保存</button>
<button id="mCancel">取消</button></p>
<p class="muted">运行中的任务保存新参数时，会先停止（撤单+平仓）再按新参数重组；
重组失败不会保存新参数，可点启动按原参数恢复。</p>
</div></div>

<script>
const $=s=>document.querySelector(s);
const TOKEN="__TOKEN__"; // 由后端在返回页面时注入（H2）
let SPECS=[], VENUES=[], TASKS=[], EDITING=null, LAST_OV=null, FILLS=[];
let OVSTALE=false, BESTSTALE=false, RECSTALE=false;  // 刷新失败保留旧数据时置位，时间戳标黄
function esc(x){return String(x==null?"":x).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function fmt(x,d=2){return x==null?"-":Number(x).toFixed(d)}
// 数据区块右上角的「更新 HH:MM:SS」：时间一律取接口的 updated_at
function updClock(ts){const d=new Date(ts*1000),p=n=>String(n).padStart(2,"0");
return p(d.getHours())+":"+p(d.getMinutes())+":"+p(d.getSeconds())}
function setUpd(el, ts, stale){
if(!el) return;
if(ts==null){el.textContent=""; el.style.color=""; return}
el.textContent="更新 "+updClock(ts)+(stale?"（刷新失败，显示上次数据）":"");
el.style.color=stale?"var(--yel)":"";
}
async function post(url){const r=await fetch(url,{method:"POST",headers:{"X-API-Token":TOKEN}});return r.json()}
async function postJson(url,obj){const r=await fetch(url,{method:"POST",headers:{"X-API-Token":TOKEN,"Content-Type":"application/json"},body:JSON.stringify(obj)});return r.json()}

async function loadSpecs(){
const d=await (await fetch("/api/strategy-specs")).json();
SPECS=d.strategies||[]; VENUES=d.venues||[];
$("#mStrategy").innerHTML=SPECS.map(s=>'<option value="'+s.strategy+'">'+esc(s.title)+"（"+s.strategy+"）</option>").join("");
SPECS.forEach(buildStrategyPage);
}

function specOf(st){return SPECS.find(s=>s.strategy===st)}

// 单个字段渲染（弹窗表单与策略页共用）。三段式：第一行字段名
// （必填带星号）、第二行说明（specs 的 desc，暗灰小字）、第三
// 行输入框。descOverride 给定（如金额区的「自由填写」说明）时
// 用它顶替字段自带说明。symbolSelect 时币种渲染成下拉（选项
// 由 fillSymbolSelects 按观察列表填充），否则文本框。
function fldHead(f, descOverride){
const desc=descOverride!==undefined?descOverride:f.desc;
return '<span class="fname">'+esc(f.label)+(f.required?" *":"")+"</span>"+
(desc?'<span class="fdesc">'+esc(desc)+"</span>":"");
}
function renderFieldRaw(f, v, symbolSelect, descOverride){
const head=fldHead(f, descOverride);
if(f.type==="bool")
return '<label class="fld">'+head+'<input type="checkbox" data-f="'+f.name+'"'+(v?" checked":"")+"></label>";
if(f.type==="enum")
return '<label class="fld">'+head+'<select data-f="'+f.name+'">'+f.options.map(o=>'<option value="'+esc(o.value)+'"'+(o.value===v?" selected":"")+">"+esc(o.label)+"</option>").join("")+"</select></label>";
if(f.type==="venue")
return '<label class="fld">'+head+'<select data-f="'+f.name+'">'+VENUES.map(x=>'<option value="'+esc(x)+'"'+(x===v?" selected":"")+">"+esc(x)+"</option>").join("")+"</select></label>";
if(symbolSelect&&f.name==="symbol")
return '<label class="fld">'+head+'<select data-f="symbol" class="symSel"></select></label>';
const t=(f.type==="int"||f.type==="float")?"number":"text";
const step=f.type==="int"?"1":"any";
return '<label class="fld">'+head+'<input type="'+t+'" step="'+step+'" data-f="'+f.name+'" value="'+(v==null?"":esc(v))+'"></label>';
}

// 带 help 的包装：与原仓差异这类长注渲染在字段下方小字
function renderField(f, v, symbolSelect, descOverride){
const html=renderFieldRaw(f, v, symbolSelect, descOverride);
return f.help?html+'<div class="fhelp">'+esc(f.help)+"</div>":html;
}

function fieldsHtml(fields, values, symbolSelect){
const groups={};
fields.forEach(f=>{(groups[f.group]=groups[f.group]||[]).push(f)});
const names=Object.keys(groups);
if(names.length<=1) return fields.map(f=>renderField(f,(values&&values[f.name]!==undefined)?values[f.name]:f.default,symbolSelect)).join("");
return names.map(g=>"<fieldset><legend>"+esc(g)+"</legend>"+
groups[g].map(f=>renderField(f,(values&&values[f.name]!==undefined)?values[f.name]:f.default,symbolSelect)).join("")+"</fieldset>").join("");
}

// 兜底分组渲染（specs 无 section 时用）：按开仓 / 平仓 / 保护 /
// 其他语义分组（由 field.section 映射；specs 的 adv_group 已于
// 第十四轮删除），与弹窗 fieldsHtml 分开
function advFieldsHtml(fields){
const order=["开仓","平仓","保护","其他"];
const sec2g={"开仓条件":"开仓","平仓条件":"平仓","平仓后怎么办":"平仓","保护":"保护"};
const groups={};
fields.forEach(f=>{const g=sec2g[f.section]||"其他";(groups[g]=groups[g]||[]).push(f)});
return order.filter(g=>groups[g]&&groups[g].length).map(g=>
"<fieldset><legend>"+esc(g)+"</legend>"+
groups[g].map(f=>renderField(f,f.default,false)).join("")+"</fieldset>").join("");
}

// ---------- 配置五段（第十轮）：交易对象 / 开仓条件 / 平仓条件 /
// 平仓后怎么办 / 保护。段归属来自 specs 的 field.section；任一
// 字段都没带 section 时返回 null，调用方回退上面的旧分组渲染
//（按 section 映射的语义分组），保证后端未就绪的中间状态不白屏。
const SECTION_ORDER=["交易对象","开仓条件","平仓条件","平仓后怎么办","保护"];
function isConfigField(spec, f){
const pg=spec.page;
if(f.zone) return f.zone==="config"||f.zone==="more";
return pg.venue_fields.indexOf(f.name)<0&&f.name!=="symbol"&&f.name!==pg.amount;
}
function configFieldHtml(f){
let html=renderField(f, f.default, false);
// manual 豁免标记：out_of_range_action 是网格唯一的退出方式、
// min_edge_bps_per_hour 是资金费进出共用一条线（manual 下作退出
// 线），两者所在段被隐藏时靠这个标记单独豁免显示它一个字段
if(f.name==="out_of_range_action"||f.name==="min_edge_bps_per_hour")
html=html.replace('<label class="fld">',
'<label class="fld" data-manual-keep="1">');
return html;
}
function sectionsHtml(spec, configFields){
if(!configFields.some(f=>f.section)) return null;
const groups={};
configFields.forEach(f=>{const g=f.section||"交易对象";
(groups[g]=groups[g]||[]).push(f)});
return SECTION_ORDER.filter(g=>groups[g]&&groups[g].length).map(g=>{
const inner='<p class="secSummary" data-sec-summary="'+esc(g)+'" style="display:none"></p>'+
'<div class="pform">'+groups[g].map(configFieldHtml).join("")+"</div>";
if(g==="保护")   // 保护段默认折叠，其余段常显
return '<details class="cfgSec" data-section="'+esc(g)+'"><summary>'+
esc(g)+"</summary>"+inner+"</details>";
return '<div class="cfgSec" data-section="'+esc(g)+'"><h4>'+esc(g)+
"</h4>"+inner+"</div>";
}).join("");
}
function legacyConfigHtml(primaryRest, advanced){
return (primaryRest.length?'<div class="pform">'+
primaryRest.map(f=>renderField(f,f.default,false)).join("")+"</div>":"")+
'<details class="advBox"><summary>其余字段（按开仓 / 平仓 / 保护 / 其他分组）</summary>'+
'<div class="aform">'+advFieldsHtml(advanced)+"</div></details>";
}

// ---------- 段总结句：模板 {字段名} 占位符 → 当前表单值 ----------
// 数值原样、布尔开/关、枚举显示选项文案；0 值只做通用处理：
// 字段说明里写了「0=不限」显示不限、写了「0=关」显示关，不为
// 单个字段写死。模板缺失（section_summaries 没有该段）不显示。
function formatSummaryValue(spec, name, v){
const f=spec.fields.find(x=>x.name===name);
if(v==null||v==="") return "—";
if(typeof v==="boolean"||(f&&f.type==="bool")) return v?"开":"关";
if(f&&f.type==="enum"&&f.options){
const o=f.options.find(x=>x.value===v); if(o) return o.label;
}
const num=Number(v);
if(typeof v!=="object"&&v!==""&&!isNaN(num)){
if(num===0&&f){
const d=(f.desc||"")+" "+(f.label||"");
if(d.indexOf("0=不限")>=0) return "不限";
if(d.indexOf("0=关")>=0) return "关";
}
return String(v);
}
return String(v);
}
function fillSummaryTemplate(spec, tpl, values){
return String(tpl).replace(/\\{([a-zA-Z0-9_]+)\\}/g,(m, name)=>
formatSummaryValue(spec, name, values?values[name]:undefined));
}
function currentFormValues(S){
const values={};
S.spec.fields.forEach(f=>{if(f.default!=null) values[f.name]=f.default});
Object.assign(values, collectFrom(S.root, S.spec));
return values;
}
// 全自动的总结句/只读参数取值：内置中性档（page.neutral_
// defaults，第十二轮取代预设）合并 Params 默认
function mergedAutoValues(S){
const values={};
S.spec.fields.forEach(f=>{if(f.default!=null) values[f.name]=f.default});
Object.assign(values, S.spec.page.neutral_defaults||{});
return values;
}
function updateSectionSummaries(st){
const S=SPAGES[st]; if(!S) return;
const sums=(S.spec.page&&S.spec.page.section_summaries)||{};
const vals=S.runMode==="full_auto"?mergedAutoValues(S):currentFormValues(S);
S.root.querySelectorAll("[data-sec-summary]").forEach(el=>{
if(el.closest(".fullAutoCfg")) return;   // 全自动区自己渲染
const sec=el.getAttribute("data-sec-summary"), tpl=sums[sec];
if(!tpl){el.style.display="none"; el.textContent=""; return}
el.textContent=fillSummaryTemplate(S.spec, tpl, vals);
el.style.display=el.dataset.forceHide==="1"?"none":"";
});
renderFullAutoCfg(st, vals, sums);
}
function renderFullAutoCfg(st, valsOpt, sumsOpt){
const S=SPAGES[st]; if(!S) return;
const sums=sumsOpt||(S.spec.page&&S.spec.page.section_summaries)||{};
const box=S.root.querySelector(".fullAutoSums"); if(!box) return;
const vals=valsOpt||mergedAutoValues(S);
box.innerHTML=["开仓条件","平仓条件","平仓后怎么办","保护"].map(sec=>{
const tpl=sums[sec]; if(!tpl) return "";
return '<p class="secSummary">'+esc(sec)+"："+
esc(fillSummaryTemplate(S.spec, tpl, vals))+"</p>";
}).join("");
const fa=S.root.querySelector(".faParams");
if(fa) fa.innerHTML=S.spec.fields.filter(f=>isConfigField(S.spec, f)).map(f=>
esc(f.label)+"："+esc(formatSummaryValue(S.spec, f.name, vals[f.name]))
).join("<br>");
}

function buildForm(st, values){
const spec=specOf(st); if(!spec){$("#mForm").innerHTML="";return}
$("#mBlurb").textContent=spec.blurb||"";
// 组装派生字段（如溢价单笔上限）不渲染输入框：它由「单笔上
// 限占仓位 %」在组装时算出，弹窗里改它会被覆盖
const derived=(spec.page&&spec.page.derived_fields)||[];
$("#mForm").innerHTML=fieldsHtml(
spec.fields.filter(f=>derived.indexOf(f.name)<0), values, false);
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
if(r.ok&&r.warnings&&r.warnings.length) TASK_WARNINGS[name]=r.warnings;   // 强行启动的警告记到任务行黄字显示
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
});

// 状态显示：小圆点 + 大写英文小标签（终端风）
function statusHtml(st){
const map={running:["g","RUNNING"],stopped:["","IDLE"],error:["r","ERROR"]};
const m=map[st]||["y",String(st||"-").toUpperCase()];
return '<span class="st"><span class="dot '+m[0]+'"></span>'+esc(m[1])+"</span>";
}

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
let ov;
try{
ov=await (await fetch("/api/overview")).json();
OVSTALE=false;
}catch(e){
if(!LAST_OV) return;   // 首屏就失败：无旧数据可留，下个周期重试
ov=LAST_OV; OVSTALE=true;   // 刷新失败：保留旧数据，时间戳标黄注明
}
LAST_OV=ov;
setUpd($("#ovUpd"), ov.updated_at, OVSTALE);
const ks=ov.killswitch;
// 页头第三行：运行状态与全局停机按钮同一行左右排布
$("#killbox").innerHTML=ks.halted
? '<p class="bad">⛔ 全局停机中：'+esc(ks.reason)+' <button data-act="killreset">解除停机</button></p>'
: '<div class="stateRow"><span class="ok"><span class="dot g"></span>RUNNING</span><button class="kill" data-act="kill">⛔ 全局停机</button></div>';
$("#tvol").textContent=fmt(ov.today.volume);
$("#tfee").textContent=fmt(ov.today.fees,4);
$("#tcnt").textContent=ov.today.fills;
const pnlCell=(id, v)=>{const el=$(id); el.textContent=fmt(v);
el.className=v>0?"ok":(v<0?"bad":"")};
pnlCell("#tpnl", ov.today.pnl);
$("#avol").textContent=fmt(ov.all_time.volume);
$("#afee").textContent=fmt(ov.all_time.fees,4);
$("#acnt").textContent=ov.all_time.fills;
pnlCell("#apnl", ov.all_time.pnl);
const exclT=ov.today.pnl_excluded||0, exclA=ov.all_time.pnl_excluded||0;
$("#pnlNote").textContent="盈亏（估算）口径：已平仓实现盈亏（按平仓价 − 开仓价计算，按盘口标记价平仓的记录含估算成分）+ 当前持仓未实现盈亏 "+
fmt(ov.unrealized_pnl)+"（未实现是当前快照，今日与累计都全额计入）"+
(exclA?"；另有平仓价未知的历史记录未计入盈亏（今日 "+exclT+" 条 / 累计 "+exclA+" 条）":"")+"。";
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
try{
const rc=await (await fetch("/api/records?limit=50")).json();
RECORDS=rc; RECSTALE=false; renderRecords();
setUpd($("#recUpd"), rc.updated_at, false);
}catch(e){
RECSTALE=true;   // 记录暂时取不到：保留上次渲染，时间戳标黄
if(RECORDS) setUpd($("#recUpd"), RECORDS.updated_at, true);
}
SPECS.forEach(spec=>{renderStratTasks(spec.strategy); renderLegs(spec.strategy); renderPreview(spec.strategy)});
}

// ---------- 策略记录页（运行仓位 + 仓位历史） ----------
let RECORDS=null;
function fmtTs(ts){
if(!ts) return "-";
const d=new Date(ts*1000), p=n=>String(n).padStart(2,"0");
return d.getFullYear()+"-"+p(d.getMonth()+1)+"-"+p(d.getDate())+" "+
p(d.getHours())+":"+p(d.getMinutes())+":"+p(d.getSeconds());
}
function stratTitle(st){const s=specOf(st); return s?s.title:(st||"-")}
function sideHtml(side){return side==="long"?'<span class="ok">多</span>':'<span class="bad">空</span>'}
function renderRecords(){
const oT=$("#recOpen tbody"), cT=$("#recClosed tbody");
if(!oT||!cT||!RECORDS) return;
oT.innerHTML=RECORDS.open.map(r=>"<tr><td>"+esc(stratTitle(r.strategy))+"</td><td>"+
(r.task?esc(r.task)+modeBadge(r.task):'<span class="tag">无归属</span>')+"</td><td>"+esc(r.venue)+"</td><td>"+esc(r.symbol)+
"</td><td>"+sideHtml(r.side)+"</td><td>"+fmt(r.qty, r.qty<1?6:4)+"</td><td>"+
fmt(r.entry_price, r.entry_price<10?4:2)+"</td><td>"+
'<button class="mini stop" data-act="close" data-venue="'+esc(r.venue)+'" data-symbol="'+esc(r.symbol)+'" data-owner="'+esc(r.task||"")+'">平仓</button>'+
"</td></tr>").join("")||'<tr><td colspan="8" class="muted">暂无运行中仓位。</td></tr>';
const srcTxt={fills:"成交均价", mark:"标记价", unknown:"平仓价未知"};
cT.innerHTML=RECORDS.closed.map(r=>"<tr><td>"+esc(stratTitle(r.strategy))+"</td><td>"+
(r.task?esc(r.task):'<span class="tag">无归属</span>')+"</td><td>"+esc(r.venue)+"</td><td>"+esc(r.symbol)+
"</td><td>"+sideHtml(r.side)+"</td><td>"+fmt(r.qty, r.qty<1?6:4)+"</td><td>"+
fmt(r.entry_price, r.entry_price<10?4:2)+"</td><td>"+
(r.close_price==null?"-":fmt(r.close_price, r.close_price<10?4:2))+
' <span class="tag">'+esc(srcTxt[r.close_price_source]||"")+"</span></td><td>"+
fmtTs(r.opened_ts)+"</td><td>"+fmtTs(r.closed_ts)+"</td></tr>").join("")||
'<tr><td colspan="10" class="muted">暂无历史仓位（仓位平掉后会落到这里）。</td></tr>';
}

// ---------- 总览：最佳机会（三策略合并成一张表，按机会值降序） ----------
// 后端 /api/best（不带 strategy）已把三策略各自 Top 5 按统一
// 机会值 % 排好：资金费类 = 预计年化 %、套利 = 单次价差 %、
// 网格 = 日波动 %；口径不同，表头已注明，只做横向参考。
let BESTALL=null;
async function loadBest(){
try{
BESTALL=await (await fetch("/api/best")).json();
BESTSTALE=false;
}catch(e){BESTSTALE=true}   // 刷新失败：保留上次列表，时间戳标黄
renderBest();
}
function bestDoText(spec, r){
const kind=spec?spec.page.opportunity:"";
if(kind==="funding")
return "多 "+esc(r.long_venue)+" / 空 "+esc(r.short_venue)+
(r.mixed?' <span class="tag">混口径仅参考</span>':"");
if(kind==="spread"){
const buy=r.price_a<=r.price_b?r.venue_a:r.venue_b;
const sell=buy===r.venue_a?r.venue_b:r.venue_a;
return "买 "+esc(buy)+" / 卖 "+esc(sell);
}
if(kind==="grid")
return "在 "+esc(r.venue)+" 做网格 · 现价 "+
fmt(r.price, r.price<10?4:2);
return "-";
}
function bestScoreText(spec, r){
const kind=spec?spec.page.opportunity:"";
if(r.score_pct==null) return "-";
if(kind==="funding") return "预计年化 "+(r.score_pct>=0?"+":"")+fmt(r.score_pct,1)+"%";
if(kind==="spread") return "单次价差 "+fmt(r.score_pct,2)+"%";
if(kind==="grid") return "日波动 "+fmt(r.score_pct,2)+"%";
return fmt(r.score_pct,2)+"%";
}
function renderBest(){
const tb=$("#bestTbl tbody"); if(!tb) return;
setUpd($("#bestUpd"), BESTALL?BESTALL.updated_at:null, BESTSTALE);
if(!BESTALL){tb.innerHTML='<tr><td colspan="5" class="muted">加载中…</td></tr>'; return}
tb.innerHTML=BESTALL.rows.map(r=>{
const spec=specOf(r.strategy);
const va=r.venue_a||r.venue||"", vb=r.venue_b||"";
return '<tr class="pickRow"><td>'+esc(spec?spec.title:r.strategy)+"</td><td><b>"+
esc(r.symbol)+"</b></td><td>"+bestDoText(spec, r)+"</td><td>"+
bestScoreText(spec, r)+'</td><td><button class="mini" data-goto="'+
esc(r.strategy)+'" data-va="'+esc(va)+'" data-vb="'+esc(vb)+
'" data-sym="'+esc(r.symbol)+'">去配置</button></td></tr>';
}).join("")||'<tr><td colspan="5" class="muted">暂无可排行的机会。</td></tr>';
}

// 点最佳机会卡片行：跳策略页并预填两腿场所 + 币种（网格还带建议区间）
async function gotoStrategy(st, va, vb, sym){
switchPage("strat-"+st);
const S=SPAGES[st]; if(!S) return;
if(va) S.vA.value=va;
if(vb) S.vB.value=vb;
S.pendingSymbol=sym||"";
if(st==="hedged_grid"&&va&&HL_FAMILY.indexOf(va)>=0&&sym){
try{
const d=await (await fetch("/api/market/candles?venue="+encodeURIComponent(va)+"&symbol="+encodeURIComponent(sym))).json();
if(d.ok) S.pendingPrefill={lower_price:d.suggested_lower, upper_price:d.suggested_upper};
}catch(e){/* 无建议区间不阻塞跳转 */}
}
loadPairs(st);
}
document.addEventListener("click",e=>{
const row=e.target.closest("[data-goto]");
if(row) gotoStrategy(row.dataset.goto, row.dataset.va, row.dataset.vb, row.dataset.sym);
});

// ---------- 左侧边栏导航 ----------
let CURPAGE="overview", MKT=null, SECLOADED=false;
function switchPage(name){
CURPAGE=name;
document.querySelectorAll("#sidebar button[data-page]").forEach(b=>b.classList.toggle("on",b.dataset.page===name));
document.querySelectorAll(".page").forEach(p=>p.classList.toggle("on",p.id==="page-"+name));
if(name.indexOf("strat-")===0){const st=name.slice(6); if(SPAGES[st]) loadPairs(st)}
if(name==="overview"){loadBest()}
if(name==="settings"){if(!SECLOADED) loadSecrets()}
if(name==="database"){loadLibraryStatus()}
}
document.querySelectorAll("#sidebar button[data-page]").forEach(b=>b.addEventListener("click",()=>switchPage(b.dataset.page)));

// ---------- 策略页（通用渲染器，四页同构） ----------
const SPAGES={};   // strategy -> 页面状态
const HL_FAMILY=["hyperliquid","entropy","tradexyz"];
const DEFAULT_WATCHLIST=["BTC","ETH","SOL","HYPE","AAPL","SNDK"];

function fieldOf(spec, name){return spec.fields.find(f=>f.name===name)}

// ---------- 运行模式显示名（第十一轮定版） ----------
// 用 spec.page.run_modes 渲染（value 仍是 manual/semi_fixed/
// semi_auto/full_auto，提交口径不变）；specs 未带 run_modes 时
// 按定版兜底：网格只有自主，溢价/资金费是三档。
function defaultRunModes(st){
if(st==="hedged_grid") return [{value:"manual",label:"自主"}];
if(st==="premium_arb"||st==="funding_carry")
return [{value:"semi_fixed",label:"固腿固标"},
{value:"semi_auto",label:"固腿优标"},
{value:"full_auto",label:"优腿优标"}];
return [{value:"manual",label:"自主"},
{value:"semi_fixed",label:"固腿固标"},
{value:"semi_auto",label:"固腿优标"},
{value:"full_auto",label:"优腿优标"}];
}
// 切换器按 run_modes 收放：不在清单里的模式按钮隐藏，按钮
// 文案换成 run_modes 的 label；只有一项时整行切换器隐藏、
// 只显示静态文字（如中性网格的「自主」）。
function applyRunModeButtons(S){
const modes=S.runModes||[];
S.root.querySelectorAll("[data-mode]").forEach(b=>{
const m=modes.find(x=>x.value===b.dataset.mode);
if(!m){b.style.display="none"; return}
b.style.display=""; b.textContent=m.label;
});
const row=S.root.querySelector(".modeRow");
if(row) row.style.display=modes.length<=1?"none":"";
const stat=S.root.querySelector(".modeStatic");
if(stat){
stat.style.display=modes.length<=1?"":"none";
if(modes.length===1) stat.textContent="运行模式："+modes[0].label;
}
}
// zone 字段渲染：与 renderField 同口径，另给溢价中枢三字段打
// data-lib-field 标记（接中枢库时按模式置只读/回填），给网格
// out_of_range_action 与资金费 min_edge_bps_per_hour 打 manual
// 豁免标记（同 configFieldHtml），
// 给每个字段打 data-section 标记（applySectionVisibility 按
// 字段逐个显隐用——zone 渲染走扁平 .pform、没有 .cfgSec 容器，
// 此前段级显隐对 zone 字段整体失效，第十四轮补）
function zoneFieldHtml(f){
let html=renderField(f, f.default, false);
if(f.section)
html=html.replace('class="fld"',
'class="fld" data-section="'+f.section+'"');
if(f.name==="out_of_range_action"||f.name==="min_edge_bps_per_hour")
html=html.replace('<label class="fld"',
'<label class="fld" data-manual-keep="1"');
if(["midline_bps","upper_bps","lower_bps"].indexOf(f.name)>=0)
html=html.replace('data-f="'+f.name+'"',
'data-f="'+f.name+'" data-lib-field="'+f.name+'"');
return html;
}

function buildStrategyPage(spec){
const st=spec.strategy, pg=spec.page;
const root=document.getElementById("page-strat-"+st);
if(!root) return;
const primaryRest=spec.fields.filter(f=>f.primary&&pg.venue_fields.indexOf(f.name)<0&&f.name!=="symbol"&&f.name!==pg.amount);
const advanced=spec.fields.filter(f=>!f.primary&&pg.venue_fields.indexOf(f.name)<0);
// 分区：字段按 zone 分组——legs=选择两腿（场所下拉专用渲
// 染）、position=仓位设置（symbol/保证金专用渲染 + 统计行 +
// 两腿杠杆 + 其余 position 字段 + 数据块）、config=策略配置、
// more=更多配置（折叠）、hidden=系统值不渲染；page.derived_
// fields 是组装派生字段（单笔上限），任何区都不渲染输入框。
// specs 未带 zone 时回退旧口径：配置区字段 = 非两腿/非
// symbol/非金额的全部字段，按五段 section 渲染（或再回退
// legacy 分组），position 区只放 symbol + 保证金。
const runModes=(pg.run_modes&&pg.run_modes.length)?pg.run_modes:defaultRunModes(st);
const initialMode=runModes[0].value;
const hasZone=spec.fields.some(f=>f.zone);
const derivedFields=pg.derived_fields||[];
let positionFields=[], configFields, moreFields=[], levFields=[], posPlainFields=[];
if(hasZone){
positionFields=spec.fields.filter(f=>f.zone==="position"&&pg.venue_fields.indexOf(f.name)<0&&f.name!=="symbol"&&f.name!==pg.amount&&derivedFields.indexOf(f.name)<0);
levFields=positionFields.filter(f=>f.name.indexOf("leverage")===0);
posPlainFields=positionFields.filter(f=>f.name.indexOf("leverage")!==0);
configFields=spec.fields.filter(f=>f.zone==="config"&&derivedFields.indexOf(f.name)<0);
moreFields=spec.fields.filter(f=>f.zone==="more"&&derivedFields.indexOf(f.name)<0);
}else{
configFields=spec.fields.filter(f=>isConfigField(spec, f));
}
let configInner, moreBlockHtml="";
if(hasZone){
configInner='<div class="pform">'+configFields.map(zoneFieldHtml).join("")+"</div>";
if(moreFields.length)
moreBlockHtml='<details class="block moreBlock"><summary>更多配置</summary>'+
'<div class="pform">'+moreFields.map(zoneFieldHtml).join("")+"</div></details>";
}else{
const secHtml=sectionsHtml(spec, configFields);
configInner=secHtml!==null?secHtml:legacyConfigHtml(primaryRest, advanced);
}
const steps=(pg.how_it_works||[]).map(s=>"<li>"+esc(s)+"</li>").join("");
// 保证金输入框（三策略统一，第十二轮）：填的是保证金（USD），
// 仓位名义 = 保证金 × 基准腿杠杆（网格为网格腿杠杆、资金费
// 为单杠杆）；marginInfo 旁注实时显示派生名义与对冲腿占用
const marginDesc={
hedged_grid:"USD，仓位名义 = 保证金 × 网格腿杠杆",
premium_arb:"USD，仓位名义 = 保证金 × 基准腿杠杆",
funding_carry:"USD，仓位名义 = 保证金 × 杠杆"}[st]||"USD（保证金）";
const marginHtml='<div class="amtRow"><div class="amtMain"><label class="fld">'+
'<span class="fname">保证金</span><span class="fdesc">'+esc(marginDesc)+"</span>"+
'<input type="number" step="any" min="0" class="marginInp" placeholder="如 100"></label></div>'+
'<span class="muted marginInfo"></span></div>';
// 网格的保证金说明整行排在第一行（币种|保证金）之后、杠杆行之前
const gridMarginNoteHtml=st==="hedged_grid"
? '<p class="muted">网格总名义 = 保证金 × 网格腿杠杆，用作网格资金上限：改保证金会按网格腿现价反推每格量（在下方配置区）；反过来改每格量/格数，这里显示约合保证金。</p>'
: "";
// 两腿杠杆区（第十三轮横排第二行）：每腿一个 posCell，杠杆
// 输入框上方一行 levInfo（「场所名 · 最高杠杆 · 保证金模式」，
// renderLegs 随数据刷新填）；资金费是单杠杆字段，两腿共用，
// 两行 levInfo 一起标、只占左列，右列留空
let levAreaHtml="";
if(levFields.length){
levAreaHtml='<div class="posGrid levGrid">';
if(levFields.length===1){
levAreaHtml+='<div class="posCell"><div class="levInfo" data-levinfo-leg="0"></div>'+
'<div class="levInfo" data-levinfo-leg="1"></div>'+
renderField(levFields[0], levFields[0].default, false)+'</div>'+
'<div class="posCell"></div>';
}else{
levFields.forEach((f,i)=>{levAreaHtml+=
'<div class="posCell"><div class="levInfo" data-levinfo-leg="'+i+'"></div>'+
renderField(f, f.default, false)+"</div>"});
}
levAreaHtml+="</div>";
}
const posPlainHtml=posPlainFields.length
? '<div class="pform positionFields">'+posPlainFields.map(f=>renderField(f,f.default,false)).join("")+"</div>"
: "";
// 选择两腿（第十三轮恢复第十一轮腿行）：场所下拉 + 方向
// 徽标 + 金额（只读）/数量 + 每腿可用余额；杠杆行不搬回
// （已定版在仓位设置，见 levInfo）
const legRow=idx=>
'<div class="legRow"><span class="legLab">'+esc(pg.venue_labels[idx])+'</span>'+
'<select class="vSel" data-leg="'+idx+'"></select>'+
'<span class="dirB" data-dir="'+idx+'"></span>'+
'金额 $<input class="legAmt" data-legamt="'+idx+'" readonly>'+
'数量 <span data-legqty="'+idx+'">-</span>'+
'<span class="legBal" data-legbal="'+idx+'">可用 —</span></div>';
root.innerHTML=
"<h2>"+esc(spec.title)+'</h2><p class="muted">'+esc(spec.blurb)+"</p>"+
'<details class="block howBox"><summary>运作方式</summary><ol>'+steps+"</ol></details>"+
'<div class="block"><h3>运行模式</h3>'+
'<div class="modeRow"><button data-mode="manual" class="on">自主</button><button data-mode="semi_fixed">固腿固标</button><button data-mode="semi_auto">固腿优标</button><button data-mode="full_auto">优腿优标</button></div>'+
'<p class="muted modeStatic" style="display:none"></p>'+
'<p class="muted modeDesc"></p></div>'+
'<div class="block fullAutoNote" style="display:none"><p class="muted">全自动：两腿和标的都由系统选，你只选策略、在下面填保证金。</p></div>'+
'<div class="block legsBlock"><span class="upd pairsUpd"></span><h3>选择两腿</h3><div class="legs">'+legRow(0)+legRow(1)+"</div>"+
'<p class="muted">先选两所：这两所共同上市的标的在下面「仓位设置」里按策略排序。方向由所选标的的机会自动定（见每腿的方向徽标）；金额 = 仓位名义（保证金 × 基准腿杠杆、两腿相同、只读），数量按现价实时换算；每条腿下方显示该所可用余额（权益），随数据刷新更新；该标的在各所的最高杠杆/保证金模式见「仓位设置」的杠杆行。</p>'+
'<p class="bad listNote"></p><div class="gridSug"></div></div>'+
'<div class="block positionBlock"><h3>仓位设置</h3>'+
'<div class="pairsStats muted"></div>'+
'<p class="muted autoPickNote" style="display:none">半自动·自动选标：标的由系统自动选——每轮开仓都按当前策略口径重新选排第一的共同标的，下方下拉不用选。</p>'+
'<div class="posGrid"><div class="posCell"><div class="symWrap"><label>币种 <select class="symSel" data-f="symbol"></select></label></div></div>'+
'<div class="posCell">'+marginHtml+"</div></div>"+
gridMarginNoteHtml+levAreaHtml+posPlainHtml+
'<div class="symData posData"></div>'+
'<div class="fundingLibInfo muted"></div></div>'+
'<div class="block configBlock"><h3>策略配置</h3>'+
'<div class="configFields">'+
'<p class="muted">正面只放核心字段，其余在下方「更多配置」里。'+(hasZone?"":"配置按五段排：交易对象 / 开仓条件 / 平仓条件 / 平仓后怎么办 / 保护，每段标题下有一句随你填写实时变化的总结。")+'</p>'+
(pg.note?'<p class="muted">'+esc(pg.note)+"</p>":"")+
configInner+'<p class="muted libNote" style="display:none"></p></div>'+
'<div class="fullAutoCfg" style="display:none">'+
'<p class="muted">全自动只定这两样：策略（本页）和保证金（上面「仓位设置」区）。两腿、标的和其余参数都由系统按内置中性档定。</p>'+
'<div class="fullAutoSums"></div>'+
'<details class="fullAutoParams"><summary>看系统用的参数</summary>'+
'<div class="faParams muted"></div>'+
'<p class="muted">只读展示实际会用的参数（内置中性档合并默认值）。要改参数请切到半自动。</p></details></div></div>'+
moreBlockHtml+
'<div class="block"><span class="upd prevUpd"></span><h3>交易预览</h3><div class="preview"><p class="muted">加载中…</p></div></div>'+
'<div class="block"><p class="bad runErr"></p><p class="runWarn" style="color:var(--yel)"></p>'+
'<button class="runBtn">▶ 运行</button> <span class="ok runOk"></span>'+
'<p class="muted">任务名自动生成（策略-币种-时分秒），点运行即创建并启动。</p></div>'+
'<div class="block"><h3>运行中仓位</h3><table class="ttasks"><thead><tr>'+
"<th>名称</th><th>标的</th><th>来源</th><th>状态</th><th>持仓</th><th>今日成交</th><th>最近错误</th><th>操作</th>"+
'</tr></thead><tbody></tbody></table></div>';
const S={spec:spec, root:root, runMode:initialMode, runModes:runModes, libEdited:{}, selSymbol:"", candles:{}, candleFetching:{},
pairsRows:[], pairsMeta:null, pairKey:"", pairSeq:0, pairsStale:false,
totalManual:false,
pendingSymbol:"", pendingPrefill:null, applying:false,
vA:root.querySelector('[data-leg="0"]'), vB:root.querySelector('[data-leg="1"]'),
symSel:root.querySelector(".symSel"),
pairsStats:root.querySelector(".pairsStats"),
posData:root.querySelector(".posData"),
pairsUpd:root.querySelector(".pairsUpd"),
prevUpd:root.querySelector(".prevUpd"),
listNote:root.querySelector(".listNote"),
gridSug:root.querySelector(".gridSug"),
legsEl:root.querySelector(".legs"), previewEl:root.querySelector(".preview"),
runErr:root.querySelector(".runErr"), runWarn:root.querySelector(".runWarn"), runOk:root.querySelector(".runOk"),
tbody:root.querySelector(".ttasks tbody")};
SPAGES[st]=S;
const opts=VENUES.map(v=>'<option value="'+esc(v)+'">'+esc(v)+"</option>").join("");
S.vA.innerHTML=opts; S.vB.innerHTML=opts;
if(VENUES.length>1) S.vB.selectedIndex=1;
const onVenue=()=>{S.pendingSymbol=pageSymbol(S); S.libEdited={}; loadPairs(st)};
S.vA.addEventListener("change",onVenue);
S.vB.addEventListener("change",onVenue);
S.symSel.addEventListener("change",()=>{S.selSymbol=S.symSel.value; S.libEdited={}; renderPosData(st); renderLegs(st); renderPreview(st); updateGridSug(st); syncGridMargin(st); loadPremiumEntry(st); loadFundingInfo(st)});
root.querySelectorAll("[data-mode]").forEach(b=>b.addEventListener("click",()=>setRunMode(st, b.dataset.mode)));
applyRunModeButtons(S);
setRunMode(st, initialMode);
const mi=root.querySelector(".marginInp");
if(mi) mi.addEventListener("input",()=>{S.totalManual=true; if(st==="hedged_grid") gridMarginToQty(st); renderLegs(st); renderPreview(st)});
root.addEventListener("input",e=>{
if(e.target.dataset&&e.target.dataset.libField&&(S.runMode==="semi_fixed"||S.runMode==="manual"))
S.libEdited[e.target.dataset.libField]=true;   // 固腿固标手改的中枢值：刷新不冲掉
if(e.target.dataset&&["qty_per_grid","grid_count"].indexOf(e.target.dataset.f)>=0) S.totalManual=false;
updateSectionSummaries(st);
renderLegs(st); renderPreview(st);
});
root.addEventListener("change",()=>{updateSectionSummaries(st); loadFundingInfo(st)});
root.querySelector(".runBtn").addEventListener("click",()=>runStrategy(st));
}

function pageSymbol(S){
return S.symSel?S.symSel.value:(S.selSymbol||"");
}

function numField(S, name){
const el=S.root.querySelector('[data-f="'+name+'"]');
if(el&&el.value!==""){const v=Number(el.value); return isNaN(v)?null:v}
const f=fieldOf(S.spec, name);
return f?f.default:null;
}

// 保证金输入框（三策略统一，第十二轮）：仓位设置里填的保证金
// （USD）；没填/非法按 0。仓位名义 = 保证金 × 基准腿杠杆
function marginOf(S){
const el=S.root.querySelector(".marginInp");
if(!el||el.value==="") return 0;
const v=Number(el.value); return isNaN(v)?0:v;
}

// ---------- 运行模式（第十轮：四值三模式） ----------
// manual 自主 / semi_fixed 半自动·固定标的 / semi_auto 半自动·
// 自动选标 / full_auto 全自动；旧值 auto 一律归一为 semi_auto。
// 切换只影响新建任务的提交参数与本页显隐，不刷新页面。
const MODE_DESCS={
manual:"自主：两腿、标的、金额都你定；点「开仓」立即开仓、不等开仓条件；平仓条件照常生效（到达自动平仓），你也可随时手动平仓；这一笔平完任务结束。",
semi_fixed:"固腿固标（半自动·固定标的）：你定两腿/标的/金额和条件，系统在这个标的上按条件开仓→平仓→再开仓，循环到你停止。",
semi_auto:"固腿优标（半自动·自动选标）：你定两腿/金额和条件，每轮系统重新选标，自动开仓→平仓→再选标，循环到你停止。",
full_auto:"优腿优标（全自动）：只选策略和保证金，两腿/标的/参数系统全定（没填的参数按内置中性档），循环到你停止。"};
const MODE_BADGE_LABELS={manual:"自主",semi_fixed:"固腿固标",
semi_auto:"固腿优标",full_auto:"优腿优标"};
function normalizeModeClient(mode){
if(mode==="auto") return "semi_auto";
return ["manual","semi_fixed","semi_auto","full_auto"].indexOf(mode)>=0
?mode:"manual";
}
// manual 隐藏开仓条件与平仓后怎么办两段、平仓条件段显示；
// 豁免标记：网格的 out_of_range_action 是唯一退出方式、资金费
// 的 min_edge_bps_per_hour 是进出共用一条线（manual 下作退出线），
// 给它们的字段打了 data-manual-keep 标记，隐藏时它所在段只显示
// 它一个字段、段总结句隐藏，其余字段藏起（见 configFieldHtml/此处逻辑）
function applySectionVisibility(S){
const hideSecs=["开仓条件","平仓后怎么办"];
S.root.querySelectorAll(".cfgSec").forEach(secEl=>{
const sec=secEl.getAttribute("data-section");
const sum=secEl.querySelector("[data-sec-summary]");
if(S.runMode==="manual"&&hideSecs.indexOf(sec)>=0){
const keep=secEl.querySelector("[data-manual-keep]");
if(keep){
secEl.style.display="";
secEl.querySelectorAll(".fld").forEach(l=>{
l.style.display=l.hasAttribute("data-manual-keep")?"":"none"});
secEl.querySelectorAll(".fhelp").forEach(h=>{h.style.display="none"});
if(sum) sum.dataset.forceHide="1";
}else{
secEl.style.display="none";
}
}else{
secEl.style.display="";
secEl.querySelectorAll(".fld").forEach(l=>{l.style.display=""});
secEl.querySelectorAll(".fhelp").forEach(h=>{h.style.display=""});
if(sum) delete sum.dataset.forceHide;
}
});
// zone 渲染的字段没有 .cfgSec 容器（扁平 .pform）：按字段自身
// 的 data-section 逐个显隐，口径与上面段容器一致；.fhelp 是
// label 的紧随兄弟元素，一并显隐
S.root.querySelectorAll(".fld[data-section]").forEach(l=>{
if(l.closest(".cfgSec")) return;
const sec=l.getAttribute("data-section");
const hide=S.runMode==="manual"&&hideSecs.indexOf(sec)>=0
&&!l.hasAttribute("data-manual-keep");
l.style.display=hide?"none":"";
const help=l.nextElementSibling;
if(help&&help.classList.contains("fhelp"))
help.style.display=hide?"none":"";
});
}
function setRunMode(st, mode){
const S=SPAGES[st]; if(!S) return;
S.runMode=normalizeModeClient(mode);
if(S.runModes&&S.runModes.length&&!S.runModes.some(m=>m.value===S.runMode))
S.runMode=S.runModes[0].value;   // 本策略没有这个模式时回退第一档
S.root.querySelectorAll("[data-mode]").forEach(b=>b.classList.toggle("on", b.dataset.mode===S.runMode));
const desc=S.root.querySelector(".modeDesc");
if(desc){
let txt=MODE_DESCS[S.runMode];
if(st==="hedged_grid"&&S.runMode==="manual")
txt+=" 网格例外：网格在区间内自动铺格交易，开/停由你手动。";
desc.textContent=txt;
}
const note=S.root.querySelector(".autoPickNote");
if(note) note.style.display=S.runMode==="semi_auto"?"":"none";
if(S.symSel) S.symSel.disabled=S.runMode==="semi_auto";
const full=S.runMode==="full_auto";
const legs=S.root.querySelector(".legsBlock");
if(legs) legs.style.display=full?"none":"";
const symW=S.root.querySelector(".symWrap");
if(symW) symW.style.display=full?"none":"";   // 全自动连币种也由系统选
const faNote=S.root.querySelector(".fullAutoNote");
if(faNote) faNote.style.display=full?"":"none";
const cfgF=S.root.querySelector(".configFields");
if(cfgF) cfgF.style.display=full?"none":"";
const faCfg=S.root.querySelector(".fullAutoCfg");
if(faCfg) faCfg.style.display=full?"":"none";
applySectionVisibility(S);
const rb=S.root.querySelector(".runBtn");
if(rb) rb.textContent=S.runMode==="manual"?"▶ 开仓":"▶ 运行";
updateSectionSummaries(st);
renderPreview(st);
applyPremiumEntry(st);   // 模式变了：中枢三字段的只读/回填口径跟着变
}
// 任务的模式徽标（任务列表与策略记录共用）：四值文案；旧值
// auto 显示为固腿优标
function modeBadge(name){
const t=TASKS.find(x=>x.name===name);
if(!t) return "";
const mode=normalizeModeClient(t.run_mode);
const txt=MODE_BADGE_LABELS[mode]||"自主";
return ' <span class="tag">'+esc(txt)+"</span>";
}

// ---------- 共同标的（pairs）：选两所后拉全量共同上市并排序 ----------
async function loadPairs(st){
const S=SPAGES[st]; if(!S) return;
const seq=++S.pairSeq;
const va=S.vA.value, vb=S.vB.value;
try{
const d=await (await fetch("/api/pairs?venue_a="+encodeURIComponent(va)+
"&venue_b="+encodeURIComponent(vb)+"&strategy="+st)).json();
if(seq!==S.pairSeq) return;   // 期间又切了场所：只认最新一次
S.pairsRows=d.rows||[]; S.pairsMeta=d; S.pairKey=va+"|"+vb+"|"+st;
S.pairsStale=false;
fillPairSymbols(st);
}catch(e){
if(S.pairsMeta){
S.pairsStale=true;   // 刷新失败：保留旧数据，只把时间戳标黄注明
setUpd(S.pairsUpd, S.pairsMeta.updated_at, true);
}else if(S.listNote) S.listNote.textContent="行情暂时取不到，请稍后重试";
}
}

// ---------- 中枢库 / 费差库（/api/libraries，服务缺失时降级隐藏） ----------
let LIBRARIES=null;
let TASK_WARNINGS={};   // task name -> 启动警告（资金费强行启动的黄字提醒）
function libEntryFor(st){
const S=SPAGES[st]; if(!S||!LIBRARIES||!LIBRARIES.premium) return null;
const sym=pageSymbol(S);
return LIBRARIES.premium[S.vA.value+"__"+S.vB.value+"__"+sym]||
LIBRARIES.premium[S.vA.value+"|"+S.vB.value+"|"+sym]||null;
}
function libUpdatedText(entry){
const ts=entry&&(entry.updated_at!=null?entry.updated_at:entry.updated_ts);
if(ts==null) return "";
if(typeof ts==="number") return updClock(ts);
return String(ts).replace("T"," ").slice(0,19);
}
// 溢价中枢三字段接库：两腿/标的变化时按三元组重新查一条
async function loadPremiumEntry(st){
const S=SPAGES[st]; if(!S||st!=="premium_arb") return;
const va=S.vA.value, vb=S.vB.value, sym=pageSymbol(S);
if(va&&vb&&sym){
try{
const d=await (await fetch("/api/libraries?venue_a="+encodeURIComponent(va)+
"&venue_b="+encodeURIComponent(vb)+"&symbol="+encodeURIComponent(sym))).json();
if(d&&d.premium) LIBRARIES=d;
}catch(e){/* 库服务取不到：保留上次状态，下个周期再试 */}
}
applyPremiumEntry(st);
}
function applyPremiumEntry(st){
const S=SPAGES[st]; if(!S||st!=="premium_arb") return;
const note=S.root.querySelector(".libNote"); if(!note) return;
const entry=libEntryFor(st);
const ro=S.runMode==="semi_auto"||S.runMode==="full_auto";
["midline_bps","upper_bps","lower_bps"].forEach(name=>{
const el=S.root.querySelector('[data-f="'+name+'"]'); if(!el) return;
el.readOnly=ro;
el.style.opacity=ro?"0.72":"";
if(entry&&entry[name]!=null){
if(ro||!(S.libEdited&&S.libEdited[name])) el.value=entry[name];
}else if(ro) el.value="";
});
note.style.display="";
if(entry){
const t=libUpdatedText(entry);
note.textContent="来源：中枢库"+(t?" · 更新于 "+t:"")+
(entry.sample_hours!=null?" · 样本 "+fmt(entry.sample_hours,1)+" 小时":"");
}else{
note.textContent="中枢库暂无此组合（需录制满 3 小时）";
}
}
// 资金费表单取值（按新旧字段名兜底，不写死单一名字）
function fundingFormValue(S, names, fallback){
for(const n of names){const v=numField(S, n); if(v!=null) return v}
return fallback;
}
// 资金费库信息行：仓位设置下方一行（有库服务时显示）
async function loadFundingInfo(st){
const S=SPAGES[st]; if(!S||st!=="funding_carry") return;
const el=S.root.querySelector(".fundingLibInfo"); if(!el) return;
const va=S.vA.value, vb=S.vB.value, sym=pageSymbol(S);
if(!va||!vb||!sym){el.textContent=""; return}
// 字段名回退（min_entry_*/max_open_spread 为第十一轮改名前的旧名）：
// 与 server._funding_gate_warnings 同口径保留——防老面板任务的
// 已存参数在改名后读不到，不是残留（第十四轮核定保留）
const line=fundingFormValue(S,["min_edge_bps_per_hour","min_entry_edge_bps_per_hour"],0);
const mos=fundingFormValue(S,["max_open_spread_bps","max_open_spread"],300);
const row=pairRow(st);
let d=null;
try{
d=await (await fetch("/api/libraries/funding?va="+encodeURIComponent(va)+
"&vb="+encodeURIComponent(vb)+"&symbol="+encodeURIComponent(sym)+
"&line="+(line||0)+"&max_open_spread="+(mos==null?300:mos)+
(row&&row.edge_bps_h!=null?"&current_net="+row.edge_bps_h:""))).json();
}catch(e){/* 库服务取不到：这一行留空（降级隐藏） */}
if(!d||!d.service){el.textContent=""; return}
const f=d.funding;
if(!f){el.textContent="费差库暂无此组合（样本不足）"; return}
el.textContent="中位持续 "+(f.median_duration_h==null?"—":fmt(f.median_duration_h,1)+" 小时")+
" · 7 天平均费差 "+(f.avg_net_bps_per_hour==null?"—":fmt(f.avg_net_bps_per_hour,3)+" bps/小时")+
" · 预计回本 "+(f.breakeven_hours==null?"—":fmt(f.breakeven_hours,1)+" 小时")+
" · 样本 "+(f.sample_hours==null?"—":fmt(f.sample_hours,0)+" 小时");
}
// 库总览（设置页数据服务状态行 + 溢价页初始回填共用）
async function loadLibraries(){
try{
LIBRARIES=await (await fetch("/api/libraries")).json();
}catch(e){LIBRARIES=null}
renderLibStatus();
if(SPAGES["premium_arb"]) loadPremiumEntry("premium_arb");
if(SPAGES["funding_carry"]) loadFundingInfo("funding_carry");
}

function fillPairSymbols(st){
const S=SPAGES[st]; if(!S) return;
const rows=S.pairsRows, kind=S.spec.page.opportunity, meta=S.pairsMeta||{};
const cur=S.pendingSymbol||pageSymbol(S);
S.symSel.innerHTML=rows.map(r=>{
// 下拉只留两段：币种 · 关键指标（价格只在下方价格行显示）
let metric="—";
if(kind==="funding")
metric=r.apr_pct==null?"—":((r.apr_pct>=0?"+":"")+fmt(r.apr_pct,1)+"%");
else if(kind==="spread")
metric=r.signed_spread_bps==null?"—":((r.signed_spread_bps>=0?"+":"")+fmt(r.signed_spread_bps,1)+" bps");
else
metric=r.volatility_pct==null?"—":(fmt(r.volatility_pct,2)+"%");
return '<option value="'+esc(r.symbol)+'">'+esc(r.symbol)+" · "+esc(metric)+"</option>";
}).join("");
let target="";
if(cur&&rows.some(r=>r.symbol===cur)) target=cur;
else if(rows.length) target=rows[0].symbol;
if(target){S.symSel.value=target; S.selSymbol=target}
S.pendingSymbol="";
// 统计行（定版格式）：共同 N · A所上市 X · B所上市 Y；共同数
// 是原始交集，策略口径过滤后可交易数不同时才短注「可交易 M」
S.pairsStats.textContent="共同 "+(meta.common??"-")+" · "+S.vA.value+" 上市 "+
(meta.listed_a??"-")+" · "+S.vB.value+" 上市 "+(meta.listed_b??"-")+
(rows.length!==(meta.common??rows.length)?" · 可交易 "+rows.length+" 个":"");
setUpd(S.pairsUpd, meta.updated_at, S.pairsStale);
renderPosData(st);
const errs=meta.errors||{};
const errTxt=Object.keys(errs).map(v=>v+" "+errs[v]).join("；");
S.listNote.textContent=errTxt||(rows.length?"":"所选两所暂无共同上市标的，换一对场所试试。");
if(S.pendingPrefill){applyPrefill(S.root, S.pendingPrefill); S.pendingPrefill=null}
updateSectionSummaries(st);
renderLegs(st); renderPreview(st); updateGridSug(st); syncGridMargin(st);
loadPremiumEntry(st); loadFundingInfo(st);
}

// 仓位设置数据块（第十二轮定版，两行）：第 1 行「数据：
// 基准腿价：X · 对冲腿价：Y · 价差：+Z bps」、第 2 行「基准
// 腿资金费：+A bps/时 · 对冲腿资金费：+B bps/时」；腿名用本
// 页两腿标签，价差口径随策略（溢价用有符号价差，资金费/
// 网格用价差），资金费页第 2 行末尾补费差。随选币与 pairs
// 刷新（20 秒）更新；数据全部取 pairs 行的原始价格与资金
// 费字段，口径逻辑不变，某项无数据显「—」
function renderPosData(st){
const S=SPAGES[st]; if(!S||!S.posData) return;
const row=pairRow(st), pg=S.spec.page, kind=pg.opportunity;
const px=x=>x==null?"—":fmt(x, x<10?4:2);
const bpsH=x=>x==null?"—":((x>=0?"+":"")+fmt(x,3)+" bps/时");
if(!row){
S.posData.innerHTML='<span class="sdT">数据</span>：暂无所选标的的数据';
return;
}
const spreadVal=kind==="spread"?row.signed_spread_bps:row.spread_bps;
let line1='<span class="sdT">数据</span>：'+
'<span class="sdLab">'+esc(pg.venue_labels[0])+"价</span>："+px(row.price_a)+" · "+
'<span class="sdLab">'+esc(pg.venue_labels[1])+"价</span>："+px(row.price_b)+" · "+
'<span class="sdLab">价差</span>：'+(spreadVal==null?"—":((spreadVal>=0?"+":"")+fmt(spreadVal,2)+" bps"));
if(row.suspect) line1+=' <span class="tag">疑似同名不同资产</span>';
let line2='<span class="sdLab">'+esc(pg.venue_labels[0])+"资金费</span>："+bpsH(row.rate_a_bps_h)+" · "+
'<span class="sdLab">'+esc(pg.venue_labels[1])+"资金费</span>："+bpsH(row.rate_b_bps_h);
if(kind==="funding") line2+=' · <span class="sdLab">费差</span>：'+(row.edge_bps_h==null?"—":fmt(row.edge_bps_h,3)+" bps/时");
S.posData.innerHTML=line1+"<br>"+line2;
}

// 腿的杠杆行文案：最高杠杆 + 保证金模式（数据来自 pairs 行，
// 各所公开接口；拿不到显示未知，绝不猜）
function legLevText(ml, mm){
if(ml==null) return "杠杆上限未知";
const lv=Number.isInteger(ml)?String(ml):fmt(ml, 2);
const mode=mm==="isolated_only"?"逐仓 only"
: mm==="cross"?"全仓/逐仓":"保证金模式未知";
return "最高杠杆 "+lv+"x · "+mode;
}

function pairRow(st){
const S=SPAGES[st]; if(!S) return null;
const sym=pageSymbol(S);
return S.pairsRows.find(r=>r.symbol===sym)||null;
}
// 所选标的的实时派生值（来自 pairs 行）：两腿价、价差（带符号 =
// A 腿减 B 腿）、费率与方向（多低费率腿/空高费率腿）、费差、混口径
function pairRates(st){
const row=pairRow(st); if(!row) return null;
return {pA:row.price_a, pB:row.price_b,
spreadBps:row.spread_bps||0,
signedBps:row.signed_spread_bps||0,
rA:row.rate_a_bps_h, rB:row.rate_b_bps_h,
edge:row.edge_bps_h, mixed:!!row.mixed,
longV:row.long_venue, shortV:row.short_venue};
}

// （第十二轮：预设已全砍——不再有预设按钮与档位切换；优腿
// 优标没填的参数由系统按内置中性档补。）

// 网格保证金：总名义 = 保证金 × 网格腿杠杆 → 每格量（qty =
// 总名义 / (格数 × 参考价)，参考价优先网格腿现价，取不到用
// 区间中价）；反向只更新保证金输入框的约合显示
function gridMarginToQty(st){
const S=SPAGES[st]; if(!S) return;
const inp=S.root.querySelector(".marginInp"); if(!inp) return;
const margin=Number(inp.value); if(!(margin>0)) return;
const levGrid=numField(S,"leverage_grid")||1;
const total=margin*levGrid;
const row=pairRow(st);
let price=row?row.price_a:null;
if(!price){
const lo=numField(S,"lower_price"), hi=numField(S,"upper_price");
if(lo&&hi) price=(lo+hi)/2;
}
const cnt=numField(S,"grid_count");
const qEl=S.root.querySelector('[data-f="qty_per_grid"]');
if(price&&cnt&&qEl) qEl.value=Number((total/(cnt*price)).toPrecision(6));
}
function syncGridMargin(st){
const S=SPAGES[st]; if(!S||st!=="hedged_grid") return;
const inp=S.root.querySelector(".marginInp"); if(!inp) return;
if(S.totalManual) return;   // 用户手填过保证金：自动刷新不许冲掉
const q=numField(S,"qty_per_grid"), cnt=numField(S,"grid_count");
const levGrid=numField(S,"leverage_grid")||1;
const row=pairRow(st);
let px=row?row.price_a:null;
if(!px){const lo=numField(S,"lower_price"), hi=numField(S,"upper_price"); if(lo&&hi) px=(lo+hi)/2}
if(q&&cnt&&px) inp.value=Math.round(q*cnt*px/levGrid*100)/100;
}

// 网格建议区间小提示（选市场区下方）：网格腿是 HL 家族时拉
// candles 给近 24h 高低与建议区间，可一键填入
function updateGridSug(st){
const S=SPAGES[st]; if(!S||st!=="hedged_grid"||!S.gridSug) return;
const venue=S.vA.value, sym=pageSymbol(S);
if(!sym){S.gridSug.innerHTML=""; return}
if(HL_FAMILY.indexOf(venue)<0){
S.gridSug.innerHTML='<span class="muted">网格腿 '+esc(venue)+' 没有公开 K 线接口，建议区间请按现价自行设定。</span>';
return;
}
const key=venue+"|"+sym, cd=S.candles[key];
if(cd&&cd.ok){
const d4=x=>fmt(x, x<10?4:2);
S.gridSug.innerHTML='<span class="muted">近 24h 高 '+d4(cd.high)+" / 低 "+d4(cd.low)+
"，建议区间 "+d4(cd.suggested_lower)+" ~ "+d4(cd.suggested_upper)+'</span> '+
'<button class="mini" data-sug="1">填入区间</button>';
S.gridSug.querySelector("[data-sug]").addEventListener("click",()=>{
applyPrefill(S.root, {lower_price:cd.suggested_lower, upper_price:cd.suggested_upper});
updateSectionSummaries(st); renderLegs(st); renderPreview(st);
});
}
else if(cd){S.gridSug.innerHTML='<span class="muted">'+esc(cd.error||"无 K 线数据")+"</span>"}
else{S.gridSug.innerHTML='<span class="muted">K 线查询中…</span>'; loadSugCandles(st, sym)}
}
async function loadSugCandles(st, sym){
const S=SPAGES[st]; if(!S) return;
const venue=S.vA.value, key=venue+"|"+sym;
if(S.candles[key]||S.candleFetching[key]) return;
S.candleFetching[key]=true;
try{
const d=await (await fetch("/api/market/candles?venue="+encodeURIComponent(venue)+"&symbol="+encodeURIComponent(sym))).json();
S.candles[key]={ok:!!d.ok, high:d.high, low:d.low,
suggested_lower:d.suggested_lower, suggested_upper:d.suggested_upper,
error:d.error};
}catch(e){S.candles[key]={ok:false, error:"网络异常，K线暂时取不到"}}
S.candleFetching[key]=false;
updateGridSug(st);
renderPosData(st);
}

// ---------- 选择两腿 / 仓位设置联动（腿行 + 杠杆行 + 保证金旁注） ----------
// renderLegs 保持函数名（刷新/切标的/输入的公共调用点不变）：
// 第十三轮在第十二轮的两处联动（仓位设置每腿杠杆输入框上方
// 的 levInfo 行、保证金行旁的 marginInfo，两者原样保留）之上
// 恢复第十一轮腿行填充——每腿方向徽标、金额/数量、可用余额
//（权益）；grid 的约合总名义仍走 marginInfo，不另加回旧的
// gridAmtInfo。
function renderLegs(st){
const S=SPAGES[st]; if(!S) return;
const row=pairRow(st);
[0,1].forEach(i=>{
const el=S.root.querySelector('[data-levinfo-leg="'+i+'"]');
if(!el) return;
const vid=i===0?S.vA.value:S.vB.value;
const ml=row?(i===0?row.max_leverage_a:row.max_leverage_b):null;
const mm=row?(i===0?row.margin_mode_a:row.margin_mode_b):"unknown";
el.textContent=vid+" · "+legLevText(ml, mm);
});
const pr=pairRates(st), kind=S.spec.page.opportunity, sym=pageSymbol(S);
const legLevNames=st==="hedged_grid"?["leverage_grid","leverage_hedge"]
:st==="premium_arb"?["leverage_base","leverage_hedge"]
:["leverage","leverage"];
const legLevBase=numField(S, legLevNames[0])||1, legMargin=marginOf(S);
[0,1].forEach(i=>{
const vid=i===0?S.vA.value:S.vB.value;
const price=row?(i===0?row.price_a:row.price_b):null;
const balEl=S.root.querySelector('[data-legbal="'+i+'"]');
if(balEl){
const v=LAST_OV?LAST_OV.venues.find(x=>x.venue_id===vid):null;
balEl.textContent=(!v||v.available==null)?"可用 —"
:("可用 $"+fmt(v.available)+"（权益 $"+fmt(v.equity)+"）"+
(v.mode==="paper"?" 模拟余额":""));
}
const dirEl=S.root.querySelector('[data-dir="'+i+'"]');
if(dirEl){
let txt="—", cls="";
if(kind==="grid"){txt=i===0?"网格双向":"反向对冲"}
else if(kind==="funding"){
if(pr&&pr.longV){
const isLong=pr.longV===vid;
txt=isLong?"做多":"做空"; cls=isLong?"long":"short";
}
}else if(kind==="spread"){
if(pr){
const isShort=(pr.signedBps>=0)===(i===0);
txt=isShort?"做空":"做多"; cls=isShort?"short":"long";
}
}
dirEl.textContent=txt;
dirEl.className="dirB"+(cls?" "+cls:"");
}
const amtEl=S.root.querySelector('[data-legamt="'+i+'"]');
const qtyEl=S.root.querySelector('[data-legqty="'+i+'"]');
if(amtEl&&qtyEl){
let legAmt=null, qty=null;
if(kind==="grid"){
const q=numField(S,"qty_per_grid"), cnt=numField(S,"grid_count");
if(q&&cnt&&price){qty=q*cnt; legAmt=qty*price}
}else if(legMargin>0&&price){
legAmt=legMargin*legLevBase; qty=legAmt/price;
}
if(legAmt==null||qty==null){amtEl.value=""; qtyEl.textContent="-"}
else{
amtEl.value=fmt(legAmt);
qtyEl.textContent=fmt(qty, qty<1?6:4)+(sym?" "+sym:"");
}
}
});
const mi=S.root.querySelector(".marginInfo");
if(mi){
const margin=marginOf(S);
const levNames=st==="hedged_grid"?["leverage_grid","leverage_hedge"]
:st==="premium_arb"?["leverage_base","leverage_hedge"]
:["leverage","leverage"];
const levB=numField(S, levNames[0])||1, levH=numField(S, levNames[1])||1;
if(st==="hedged_grid"){
const q=numField(S,"qty_per_grid"), cnt=numField(S,"grid_count");
const px=row?row.price_a:null;
mi.textContent=(q&&cnt&&px)
?("当前约合总名义 $"+fmt(q*cnt*px)+"（保证金 $"+fmt(q*cnt*px/levB)+"）")
:(margin>0?("总名义 $"+fmt(margin*levB)+" = 保证金 × 网格腿杠杆 "+levB):"");
}else if(margin>0){
const notional=margin*levB;
mi.textContent="仓位名义 $"+fmt(notional)+"（基准腿占用保证金 $"+fmt(margin)+
" · 对冲腿占用保证金 $"+fmt(notional/levH)+"）";
}else mi.textContent="";
}
}

// ---------- 交易预览（按平台分列 + 入场条件指示） ----------
function renderPreview(st){
const S=SPAGES[st]; if(!S||!S.previewEl) return;
setUpd(S.prevUpd, S.pairsMeta?S.pairsMeta.updated_at:null, S.pairsStale);
const pg=S.spec.page, kind=pg.opportunity;
const levNames=st==="hedged_grid"?["leverage_grid","leverage_hedge"]
:st==="premium_arb"?["leverage_base","leverage_hedge"]
:["leverage","leverage"];
const row=pairRow(st), pr=pairRates(st);
const marginIn=marginOf(S);
const levBase=numField(S, levNames[0])||1;
const legs=[0,1].map(i=>{
const vid=i===0?S.vA.value:S.vB.value;
const lev=numField(S, levNames[i])||1;
const mark=row?(i===0?row.price_a:row.price_b):null;
let notional=null;
if(kind==="grid"){
const q=numField(S,"qty_per_grid"), cnt=numField(S,"grid_count");
let px=mark;
if(!px){const lo=numField(S,"lower_price"), hi=numField(S,"upper_price"); if(lo&&hi) px=(lo+hi)/2}
if(q&&cnt&&px) notional=q*cnt*px;
}else if(marginIn>0) notional=marginIn*levBase;
// 占用保证金 = 名义 ÷ 该腿杠杆：基准腿即所填保证金，对冲腿
// 开相同名义、占用 = 名义 ÷ 对冲腿杠杆，都在下面分列列清
return {vid:vid, lab:pg.venue_labels[i], mark:mark, lev:lev,
notional:notional, margin:notional!=null?notional/lev:null};
});
const markTxt=(L, i)=>!row?'<span class="muted">未在共同上市</span>'
:L.mark==null?'<span class="muted">价格暂缺</span>'
:fmt(L.mark, L.mark<10?4:2);
const rowsHtml=legs.map((L, i)=>"<tr><td>"+esc(L.lab)+"</td><td>"+esc(L.vid)+"</td><td>"+markTxt(L, i)+"</td><td>"+
(L.mark!=null?fmt(L.mark, L.mark<10?4:2)+' <span class="muted">市价预估</span>':"-")+"</td><td>"+
(L.notional!=null?"$"+fmt(L.notional):"-")+"</td><td>"+
(L.margin!=null?"$"+fmt(L.margin):"-")+' <span class="muted">（杠杆 '+L.lev+'×）</span>'+"</td></tr>").join("");
const totN=legs.every(L=>L.notional!=null)?legs[0].notional+legs[1].notional:null;
const totM=legs.every(L=>L.margin!=null)?legs[0].margin+legs[1].margin:null;
const avail=legs.map(L=>{
const v=LAST_OV?LAST_OV.venues.find(x=>x.venue_id===L.vid):null;
if(!v||v.available==null) return esc(L.lab)+" <b>"+esc(L.vid)+"</b> 可用：-";
const short=L.margin!=null&&L.margin>v.available;
return esc(L.lab)+" <b>"+esc(L.vid)+"</b> 可用 $"+fmt(v.available)+
(v.mode==="paper"?' <span class="tag">模拟余额</span>':"")+
(short?' <span class="bad">所需保证金不足</span>':"");
}).join("　");
let ind="";
if(st==="funding_carry"){
const th=numField(S,"min_edge_bps_per_hour")??numField(S,"min_entry_edge_bps_per_hour");   // 旧字段名回退，同 loadFundingInfo 处口径
if(!pr||pr.edge==null) ind='<p class="muted">入场条件：暂无所选标的的费差数据。</p>';
else if(pr.mixed) ind='<p style="color:var(--yel)">入场条件：当前费差 '+fmt(pr.edge,3)+' bps/小时为混口径（仅参考），与门槛不可直接比较；策略开仓时也会拒绝混口径组合。</p>';
else if(th!=null&&pr.edge>=th) ind='<p class="ok">入场条件：已达入场门槛（费差 '+fmt(pr.edge,3)+" ≥ 门槛 "+fmt(th,3)+' bps/小时）；方向：多 '+esc(pr.longV)+" / 空 "+esc(pr.shortV)+"。</p>";
else ind='<p style="color:var(--yel)">入场条件：未达门槛（费差 '+fmt(pr.edge,3)+" < 门槛 "+fmt(th,3)+' bps/小时），启动后等待机会。</p>';
}else if(st==="premium_arb"){
const mid=numField(S,"midline_bps"), up=numField(S,"upper_bps"), lo=numField(S,"lower_bps");
if(!pr) ind='<p class="muted">入场条件：暂无所选标的的价差数据。</p>';
else if(mid!=null&&up!=null&&pr.signedBps>=mid+up) ind='<p class="ok">入场条件：已进入入场带上方（价差 '+(pr.signedBps>=0?"+":"")+fmt(pr.signedBps,2)+" ≥ 中枢+上带 "+fmt(mid+up,2)+' bps）；方向：空基准腿 / 多对冲腿。</p>';
else if(mid!=null&&lo!=null&&pr.signedBps<=mid-lo) ind='<p class="ok">入场条件：已进入入场带下方（价差 '+fmt(pr.signedBps,2)+" ≤ 中枢−下带 "+fmt(mid-lo,2)+' bps）；方向：多基准腿 / 空对冲腿。</p>';
else ind='<p style="color:var(--yel)">入场条件：未进入入场带（价差 '+(pr.signedBps>=0?"+":"")+fmt(pr.signedBps,2)+' bps，中枢 '+fmt(mid,2)+" ± 上带 "+fmt(up,2)+"/下带 "+fmt(lo,2)+")，启动后等待偏离。</p>";
}else{
const lo=numField(S,"lower_price"), hi=numField(S,"upper_price"), cnt=numField(S,"grid_count");
if(lo&&hi&&cnt&&hi>lo){
const step=(hi-lo)/cnt, midP=(lo+hi)/2;
ind='<p class="muted">每格间距 ≈ '+fmt(step/midP*10000,1)+" bps（等距 "+fmt(step, step<10?4:2)+"）；总名义 ≈ "+(totN!=null?"$"+fmt(totN):"-")+"。</p>";
}else ind='<p class="muted">填好区间上下限与格数后，这里显示每格间距与总名义。</p>';
}
// 杠杆超限警告：所填杠杆超过任一腿该标的在该所的最高杠杆 →
// 逐腿黄字警告；保证金仍按所填杠杆算，警告与口径注并列展示
const levWarns=legs.map((L, i)=>{
const ml=row?(i===0?row.max_leverage_a:row.max_leverage_b):null;
if(ml==null||!(L.lev>ml)) return "";
const lv=Number.isInteger(ml)?String(ml):fmt(ml, 2);
return '<p style="color:var(--yel)">⚠ '+esc(L.vid)+" 该标的最高 "+lv+
"x，你填了 "+L.lev+"x，实际执行可能被该所拒绝或按上限成交"+
"（逐仓/全仓见仓位设置的杠杆行）。</p>";
}).join("");
const levModes=legs.map((L, i)=>{
const mm=row?(i===0?row.margin_mode_a:row.margin_mode_b):"unknown";
return mm==="isolated_only"
? '<p style="color:var(--yel)">⚠ '+esc(L.vid)+" 该标的仅逐仓（isolated only），账户须为逐仓模式。</p>"
: "";
}).join("");
const modeTxt=S.runMode==="full_auto"
?"运行模式：优腿优标（全自动）——只选策略和保证金，系统定两腿/标的/参数，自动选标、开仓、平仓、再选标循环，点停止才停。"
:S.runMode==="semi_auto"
?"运行模式：固腿优标（半自动·自动选标）——你定两腿/保证金/条件，系统每轮重新选标，自动开仓、平仓、再选标循环，点停止才停。"
:S.runMode==="semi_fixed"
?"运行模式：固腿固标（半自动·固定标的）——在你选的标的上按条件开仓、平仓、再开仓，循环到你点停止。"
:"运行模式：自主——点「开仓」立即开仓、不等开仓条件；平仓条件到达自动平仓，也可手动平仓，平完这一笔任务结束。";
// 溢价仓位上限（第十三轮）：仓位 = 保证金 × 基准腿杠杆派生，
// 本身就是上限——第十二轮的「名义超最大持仓」预览警告构造上
// 不再可能，已删除，不再有单独的最大持仓概念
S.previewEl.innerHTML="<table><thead><tr><th>腿</th><th>平台</th><th>标记价</th><th>预估执行价</th><th>名义</th><th>占用保证金</th></tr></thead><tbody>"+
rowsHtml+"</tbody></table>"+
'<p>总名义：<b>'+(totN!=null?"$"+fmt(totN):"-")+'</b>　总占用保证金：<b>'+(totM!=null?"$"+fmt(totM):"-")+"</b></p>"+
"<p>"+avail+"</p>"+
'<p class="muted">名义 = 保证金 × 基准腿杠杆（网格按每格量 × 格数 × 现价）；占用保证金 = 名义 ÷ 该腿杠杆。程序不自动改杠杆，各所实际上限见仓位设置的杠杆行。</p>'+
'<p class="muted">'+modeTxt+"</p>"+
levWarns+levModes+ind;
}

// 运行中仓位表（本策略任务：状态/持仓/今日成交/操作）
function renderStratTasks(st){
const S=SPAGES[st]; if(!S) return;
const list=TASKS.filter(t=>t.strategy===st);
S.tbody.innerHTML=list.map(t=>{
const tMode=normalizeModeClient(t.run_mode);
let sym;
if(tMode==="semi_auto"||tMode==="full_auto"){
sym=(t.current_symbol||"选标中")+"（已循环 "+(t.cycles_completed||0)+" 轮）";
if(tMode==="full_auto"&&t.current_venues&&t.current_venues.length)
sym+=" · 两腿 "+t.current_venues.join(" / ");
}else{
sym=((t.params&&t.params.params&&t.params.params.symbol)||"-");
}
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
const warns=TASK_WARNINGS[t.name];
const warnHtml=warns&&warns.length?'<br><span style="color:var(--yel)">⚠ '+esc(warns.join("；"))+"</span>":"";
return "<tr><td>"+esc(t.name)+modeBadge(t.name)+warnHtml+"</td><td>"+esc(sym)+"</td><td>"+
(t.source==="panel"?"面板":"配置文件")+"</td><td>"+statusHtml(t.status)+"</td><td>"+posHtml+
"</td><td>"+(tf.n?tf.n+" 笔 / $"+fmt(tf.vol):"-")+"</td><td>"+esc(t.last_error||"")+"</td><td>"+btns+"</td></tr>";
}).join("")||'<tr><td colspan="8" class="muted">暂无该策略任务，用上方配置填好后点运行。</td></tr>';
}

// 一键运行：组装参数 → 自动任务名 → 创建并启动
async function runStrategy(st){
const S=SPAGES[st]; if(!S) return;
const spec=S.spec, pg=spec.page;
const params={};
params[pg.venue_fields[0]]=S.vA.value;
params[pg.venue_fields[1]]=S.vB.value;
Object.assign(params, collectFrom(S.root, spec));
if((S.runMode==="semi_auto"||S.runMode==="full_auto")&&!params.symbol)
params.symbol=(S.pairsRows&&S.pairsRows.length&&S.pairsRows[0].symbol)||"BTC";  // 自动选标时 symbol 只是占位，后端选标时忽略
const now=new Date();
const pad=n=>String(n).padStart(2,"0");
const name=pg.name_prefix+"-"+(params.symbol||"NA")+"-"+pad(now.getHours())+pad(now.getMinutes())+pad(now.getSeconds());
S.runErr.textContent=""; S.runOk.textContent=""; if(S.runWarn) S.runWarn.textContent="";
const body={name:name, strategy:st, params:params, run_mode:S.runMode};
// 保证金（三策略统一，第十二轮）：仓位设置填的保证金走顶层
// amount_usd（后端按保证金 × 基准腿杠杆换算名义/额度）
const mg=marginOf(S);
if(mg>0) body.amount_usd=mg;
if(S.runMode==="full_auto"&&st==="hedged_grid"){
// 全自动网格：params 里的 qty_per_grid 不提交（后端按保证金
// × 网格腿杠杆得到的总名义反推每格量）
delete params.qty_per_grid;
}
const r=await postJson("/api/tasks/create",body);
if(!r.ok){S.runErr.textContent=r.error||"创建失败"; return}
S.runOk.textContent="已创建并运行："+r.task;
if(r.warnings&&r.warnings.length){
TASK_WARNINGS[r.task]=r.warnings;
if(S.runWarn) S.runWarn.textContent="⚠ "+r.warnings.join("；")+"（已按你的要求强行启动）";
}
refresh();
}

// 币种下拉选项：每个币后面带当前最优机会值（按页面类型）与上市所数
function bestValueText(spec, row){
if(!row) return "";
const mv=spec?spec.page.market_value:"price";
if(mv==="funding_apr"&&row.best_funding)
return " · 预计年化 +"+fmt(row.best_funding.edge_bps_h*87.6,1)+"%";
if(mv==="spread_bps"&&row.best_spread)
return " · 价差 "+fmt(row.best_spread.spread_bps,1)+" bps";
const cells=Object.values(row.venues||{});
const ok=cells.find(c=>c.status==="ok"&&c.price);
return ok?" · 现价 "+fmt(ok.price, ok.price<10?4:2):"";
}
function fillSymbolSelects(){
const list=(MKT&&MKT.watchlist&&MKT.watchlist.length)?MKT.watchlist:DEFAULT_WATCHLIST;
document.querySelectorAll("select.symSel").forEach(el=>{
const sec=el.closest("section.page");
if(sec&&sec.id.indexOf("page-strat-")===0) return;  // 策略页币种走 pairs，不归这里管
const cur=el.value;
const spec=null;
const vals=list.slice();
if(cur&&vals.indexOf(cur)<0) vals.unshift(cur);
el.innerHTML=vals.map(s=>{
const row=MKT&&MKT.symbols?MKT.symbols.find(x=>x.symbol===s):null;
let extra="";
if(row){
extra=bestValueText(spec, row);
if(row.listed_count!=null) extra+=" · "+row.listed_count+"/7 所上市";
}
return '<option value="'+esc(s)+'">'+esc(s)+esc(extra)+"</option>"}).join("");
if(cur) el.value=cur;
});
}

// ---------- 行情（无独立页面：/api/market 只供弹窗的币种 ----------
// 下拉与总览/策略页的 pairs、best 数据；策略页币种下拉走 pairs）
// 实时节奏：策略页打开期间 pairs 每 20 秒重拉（与扫描缓存
// 对齐），排序/预览/腿数量随新数据重算，已选标的与表单不被
// 冲掉；总览最佳机会同为 20 秒；总览统计/场所/记录走 refresh
// 的 5 秒节奏（见文件尾）。
async function loadMarket(force){
try{
const d=await (await fetch("/api/market"+(force?"?force=1":""))).json();
MKT=d;
fillSymbolSelects();
}catch(e){/* 行情暂时取不到：下拉保留观察列表默认值，下个周期重试 */}
}
setInterval(()=>{
if(CURPAGE.indexOf("strat-")===0){
const st=CURPAGE.slice(6);
if(SPAGES[st]) loadPairs(st);
}
if(CURPAGE==="overview") loadBest();
},20000);

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
'<fieldset class="sec"><legend>'+esc(g.title)+"</legend>"+
(GUIDES[g.key]?'<details><summary>怎么获取？</summary><div>'+GUIDES[g.key]+"</div></details>":"")+
g.fields.map(f=>'<label class="fld"><span class="fname">'+esc(f.label)+
(f.required===true?' <span class="bad">*</span>':"")+(f.required==="one_of"?' <span class="muted">(二选一必填)</span>':"")+
' <span class="'+(f.filled?"ok":"bad")+'">'+(f.filled?"已设置":"未设置")+'</span></span>'+
(f.desc?'<span class="fdesc">'+esc(f.desc)+"</span>":"")+
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

// ---------- 数据库页：数据服务状态行与启停（/api/libraries） ----------
function renderLibStatus(){
const el=$("#libStatus"); if(!el) return;
const s=LIBRARIES&&LIBRARIES.service;
if(!s){
el.innerHTML='<p class="muted">数据服务：未运行（中枢库 / 费差库不可用，相关功能已降级隐藏）</p>'; return;
}
const tsTxt=ts=>ts==null?"—":updClock(ts);
el.innerHTML='<p class="muted">中枢库 / 费差库录制服务：'+(s.running?"运行中":"未运行")+
" · 最近录制 "+tsTxt(s.last_record_ts)+
" · 最近重算 "+tsTxt(s.last_recompute_ts)+
" · 中枢库条目数 "+(s.premium_entries==null?"—":s.premium_entries)+
(s.last_error?' · <span style="color:var(--yel)">最近错误：'+esc(s.last_error)+"</span>":"")+"</p>";
}
async function loadLibraryStatus(){
try{ LIBRARIES=await (await fetch("/api/libraries")).json(); }
catch(e){ LIBRARIES=null }
renderLibStatus();
}
$("#libStart").addEventListener("click",async()=>{
const r=await post("/api/libraries/start");
$("#libMsg").textContent=r.ok?"已启动数据服务":("启动失败："+(r.error||""));
$("#libMsg").className=r.ok?"ok":"bad";
loadLibraryStatus();
});
$("#libStop").addEventListener("click",async()=>{
const r=await post("/api/libraries/stop");
$("#libMsg").textContent=r.ok?"已停止数据服务":("停止失败："+(r.error||""));
$("#libMsg").className=r.ok?"ok":"bad";
loadLibraryStatus();
});

loadSpecs().then(()=>{refresh(); loadBest(); loadMarket(false); loadLibraries()}); setInterval(refresh, 5000);
</script>
</body>
</html>
"""
