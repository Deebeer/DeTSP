# DeTSP 设计文档

统一永续合约交易系统：一套代码、一个引擎、一个账本，承载三种策略（中性网格、溢价套利、资金费套利），接入 7 个交易场所。本项目为个人自用、非商业用途。

## 1. 目标与边界

- 本地单机运行（默认只监听 127.0.0.1），单用户，不托管、不对外提供服务。
- 三种策略共用交易所适配、风控、账本与配置，不再各带一套签名/下单/状态。
- 先 paper（模拟）跑通，再小额实盘；任何策略不得绕过全局风控直接下单。

## 2. 交易所清单（7 个）

| venue_id | 交易所 | 实现文件 | 备注 |
|---|---|---|---|
| arcus | Arcus | venues/arcus.py | Ed25519 API 密钥签名；全仓 |
| lighter | Lighter 主网 | venues/lighter.py | 与 Robinhood 链共用参数化实现（deployment=mainnet） |
| lighter_rh | Lighter Robinhood 链 | venues/lighter.py | deployment=robinhood；计价 USDG；独立账户与密钥 |
| hyperliquid | Hyperliquid 本体 | venues/hl.py | dex=""（默认主 dex） |
| entropy | Entropy | venues/hl.py | dex="io"，Hyperliquid builder 子所；只逐仓 |
| tradexyz | trade.xyz | venues/hl.py | dex="xyz"，Hyperliquid builder 子所 |
| aster | Aster | venues/aster.py | API key/secret 签名；适配为本项目自写 |

Hyperliquid 家族（本体 / Entropy / trade.xyz）共用 hl.py：同一套 Hyperliquid SDK、同一 API 钱包机制，仅 dex 参数、市场命名与保证金模式不同。
Lighter 两个部署共用 lighter.py：仅端点、chain id、账户与密钥不同。

## 3. 三种策略（strategies/）

三个策略全部是双腿/对冲结构：方向风险靠第二条腿对锁，不留单边裸仓。每个策略注明来源仓库与重组关系；普通网格与单所刷量已移除（单边裸仓不符合本系统的中性原则）；轮换对冲曾为第四个策略，2026-11-05 第十一轮按用户定案整删。

### 3.1 中性网格 hedged_grid.py
- 网格腿在区间 [lower_price, upper_price] 内等差铺单：long / short / long_short 三模式，成交后在相邻格挂反向单（数量按实际成交量镜像，部分成交只挂已成部分）；对冲腿每个 tick 读网格腿真实净持仓，目标 = −网格净持仓，差额超 hedge_deadband_qty（死区）时用 taker 市价调平。
- 初始铺单跳过贴价档位（skip_band_frac×格距内不挂，WGALL skipBand 口径）；记账为增量口径（回执/poll/对账三路只记增量，不重复计数）；限价单下单结果未知时登记待确认单，后续认领真实订单号或对账定案，不重复挂。
- 残差达不到对冲所最小下单量时等累积；裸露持续超过 max_unhedged_seconds 时报警并强制对冲；残差低于最小量但持续超过 max_residual_seconds（独立开关）时同样强制对冲。对冲调平前查两所价格偏离（>3% 跳过）与上一笔调平单是否已结算定案（未定案不叠加）。
- 启动预检（WGALL 口径）：双腿所需保证金（单腿估算名义 ÷ 各自杠杆）超过任一所可用权益拒绝启动、超过 80% 告警；现价远离区间（超出 [lower×0.5, upper×2]）拒绝启动；格距毛利不大于往返手续费估算时告警。60 秒内自有单被撤/被拒 ≥5 次触发撤单风暴保护，暂停开仓类铺单 60 秒（只减仓单不拦）。
- 区间外动作二选一：close（撤双腿单、平双腿停机）或 reduce_only（网格腿只挂只减仓回收阶梯，不加仓、不当止损，对冲腿继续跟随调平）；回收阶梯先落在现价与被破边界之间（区间外），再沿价格返回方向逐格延伸覆盖全部剩余持仓。
- 双腿都做强平距离监控；caps 启动硬校验按双腿总名义估算；stop 平仓后显式释放双腿归属（不依赖持仓查询返回空）。
- 来源：网格铺单语义来自 WGALL-ZJ005（ZAIJIN88，MIT）的网格逻辑；对锁调平来自 Parallax Hedge（ZAIJIN88，MIT）的双腿结构。两者重组为一个策略。

### 3.2 溢价套利 premium_arb.py
- 一腿为基准场所，另一腿为对冲场所；溢价 premium_bps = (A 价 / B 价 − 1) × 10000。
- 信号带由三参数定义：midline_bps（溢价中枢，必须先录制数据实测，不假设为 0）、upper_bps、lower_bps；自动档（固腿优标/优腿优标）的三带由中枢库供给（见第 12 节统计库），固腿固标默认填库值、可改，运行中任务不换中枢；触发判断用可成交价（bid/ask）且扣除双边 taker 手续费，信号须持续 persist_seconds 才触发；仓位由保证金 × 基准腿杠杆派生（引擎内部 max_position_usd）、单笔切片受 order_cap_pct（占仓位 %，默认 50）约束，并受库存阶梯约束；定期对账修正净 delta。
- 执行模式 execution_mode：taker（默认）双腿 taker 立即开平；maker 时**开仓**的基准腿先以 post-only 限价挂在盘口内侧（买贴 bid、卖贴 ask，可配 maker_offset_ticks）等成交，全成后对冲腿 taker 跟上，部分成交先对冲已成部分再撤余单结束本轮，超时未成撤单放弃本轮。**平仓与反向减仓永远走双腿 taker**，保证退出确定性。
- 结算宽限期（settle_grace_seconds，默认 20 秒）：taker 双腿或 maker 对冲腿下出后，宽限期内定期对账不做 delta 削腿（异步结算的腿还没显形，削腿会误伤已成腿），期满后第一次对账再修正；delta 修正本身用带滑点上限（max_slippage_bps）的 reduce-only 限价单。maker 开仓单下单结果未知时登记待确认单并后续认领；maker 等成交期间强平距离监控与等待并行，已有持仓跌破门槛照样双平。参数校验已补齐（两腿成对且不同所、杠杆/金额/周期为正、费率非负）。
- 来源：价差带逻辑移植自 entropy-arb（your-quantguy，MIT）；maker 主腿执行方式源自 perp-dex-tools（非商业许可，个人自用）的 hedge 模式。盘口录制与分析工具思路同样来自 entropy-arb。

### 3.3 资金费套利 funding_carry.py
- 不刷量、不轮换：选两所资金费差最大的一组方向（多腿在费率低侧、空腿在费率高侧），双腿 taker 等量开仓长期持有，赚组合每小时净收的资金费差。选向与持有期退出判断都先过费率闸门（见第 6 节），再统一换算到每小时口径。
- 正面五项配置（第十一轮定案）：最大开仓价差 max_open_spread_bps（默认 300 bps，开仓时两腿价差超此值不开）、最低资金费率差 min_edge_bps_per_hour（默认 1.0，进出共用一条线：净费差低于此值并连续确认 3 次才双平）、强平保护 close_corridor_pct（默认 1.5，任一腿距强平近于此值双平）、数量偏差自动平仓 qty_dev_close_pct（默认 0 = 关；小偏差先削腿修平、超此值直接平双腿）、自动重开 reopen（平仓后冷却 60 秒，系统值）。自动档选标还要过费差库持续性硬关（见第 12 节统计库）。
- 结算宽限期（settle_grace_seconds，默认 10 秒）：开仓下出后先立即回读确认双腿成形，未确认进入宽限等待（opening 阶段），宽限内不做孤腿定性，期满按真实持仓定案。双腿不等量时用带滑点上限（max_slippage_bps）的限价单削腿。
- 启动对账定案：已有双腿先做净 delta 修正（不等量削多余腿、孤腿平掉），等量双腿按真实持仓方向接管只继续监控，不因当前最优方向不同而强制平仓。
- 来源：Parallax Hedge 的资金费选向模块（轮换对冲里只用它选开仓方向）独立成策略。

## 4. 统一适配接口（core/venue.py）

所有场所实现同一抽象接口 VenueAdapter：

- markets()：市场列表与规则（精度、最小量、最大杠杆）
- book(symbol)：盘口（bid/ask）
- place_order(req) / cancel_order(order_id) / cancel_all(symbol)
- positions()：持仓；balance()：权益/可用
- funding(symbol)：资金费率
- 每个适配器带 mode：paper 时走模拟撮合，live 时走真实签名下单

策略层只依赖此接口，不感知任何交易所的签名细节。

## 5. 归属登记与冲突拦截（core/registry.py）

多策略共用同一账户是本系统最大风险，必须在引擎层硬拦截：

- 归属键 = (venue, account, symbol)，标签 = 任务名。某策略要接管某键前必须 claim；已被其他策略占用则抛 ConflictError，拒绝启动。
- 订单登记表记录每笔自有订单的归属：撤单/记账只认自有订单（cancel_all_own / cancel_unregistered 都以登记表为准）。
- 归属只在**进程内**有效：登记表不落盘，进程重启后为空，靠各任务的启动对账与交易所真实状态重新对齐（见第 12 节「启动恢复对账语义」）。
- 如实说明（不要误以为有更细的隔离）：启动对账处理的是该 (venue, account, symbol) 下的**全部**仓位与挂单，包括同一账户里手工开的仓位、其他程序留下的仓位——进程无法区分「本程序遗留」与「外部/手动」。因此**运行前必须把要用的账户/币种清空**（无手工仓位、无其他程序在跑），被启动对账接管或平掉的仓位明细会逐项记入系统事件（engine.events，面板总览可见）。Lighter 的 api_key_index 由各场所配置固定指定，登记表不做 index 级占用管理（旧设计曾承诺，未接线，已删除该承诺与死代码）。

## 6. 全局风控（core/risk.py）

安全动作永远压过交易动作，风控为纯函数优先、便于单测：

- 强平距离（corridor）：开仓门槛为全局 risk.min_corridor_pct（默认 3.0）；平仓线不在全局 risk 里，由各策略自己的参数管（如资金费的强平保护 close_corridor_pct）。
- 单腿检测：对冲类仓位任一腿缺失/数量不平 → 立即触发减腿流程。
- 仓位上限：每所、每策略、全局三层 notional 上限。
- 全局停机：KillSwitch 触发后引擎拒绝一切新开仓，只允许撤单/平仓；连续错误达阈值（risk.max_consecutive_errors，默认 3）自动停机。
- 价差闸门：资金费开仓价差上限是策略参数 max_open_spread_bps（默认 300 bps，防同名不同标的）；网格对冲调平前的两所价格偏离检查在策略内（>3% 跳过本轮调平）。
- 资金费闸门（选向防呆，移植 Parallax 口径）：单期费率绝对值 >5% 判数据错误；两腿费率口径（rate_kind：下期预测/当期/上一期已结算）不一致时拒绝相减选向。carry 选向与费差库录制层共用 core/risk.py 的 evaluate_funding_pair。另有费率周期自检：用全市场费率快照的地板价年化反查结算周期假设是否被整倍数搞错，实盘预检逐所执行（见第 12 节费率口径表）。

## 7. 统一账本（core/ledger.py）

现状（如实口径，不做未实现的承诺）：

- SQLite 单库（data/detsp.db），fills 表记录每笔成交：时间、场所、账户、币种、策略、方向、数量、价格、手续费、订单号。成交发现有四路：下单回执、poll 终态、Executor 对异步市价单的成交发现（poll_pending_fills，Lighter 这类回执未终结的市价单靠它补记）、策略层对账推断，四路统一增量记账不重复。
- 已实现的聚合：按策略/全局的成交笔数、成交量（名义额）、手续费合计（count / total_volume / total_fees / stats_since），面板与统计直接可用。
- **手续费字段现状**：各交易所回执的手续费口径未统一核定，Executor 记账时 fee 恒记 0——账本里的手续费合计目前恒为 0，不要用它算成本；真实手续费以交易所账单为准。待逐所核定回执 fee 字段后补记。
- **盈亏（估算）聚合（第五轮已补总览口径）**：面板总览的今日/累计盈亏 = closed_positions 的已实现盈亏（(平仓价−开仓价)×量×方向，fills 与 mark 口径都按记录价计、unknown 不计）+ 当前持仓未实现盈亏，由 Ledger.realized_pnl_since 聚合。
- **未实现（计划项，不要当已交付）**：按策略拆分的已实现盈亏（PnL）聚合、每万美元磨损聚合尚未做；当前评估磨损只能看成交量与交易所账单。策略层与交易所持仓的定期对账在各策略内实现（套利的定期对账、网格/carry 的安全检查），账本层没有独立的全局对账聚合。

## 8. 引擎（core/engine.py）

每轮 tick 的真实流程（与代码一致）：

1. 串行执行面板命令队列（任务启停、停机/复位都经队列，不在 HTTP 线程下单）；
2. 推进市价单成交发现（Executor.poll_pending_fills：异步结算场所的市价单按 poll 终态或持仓差定案补记账本，并及时注销订单登记）；
3. 逐任务 tick（任务自带安全检查→状态机），全局停机时以 allow_open=False 运行（撤单/平仓/孤腿处理照常，开仓禁止）。

异常处理：单任务异常只隔离该任务并计数，不拖停其他任务；停机在 tick 中途触发导致的 RiskHaltError 是安全机制的正常结果，**不计**错误数；同一任务连续错误达阈值 → 停用该任务、触发全局停机，并立即调用该任务的 stop() 收尾（撤单 + 平仓），不让它的持仓脱离自动管理；stop 收尾失败会把明细记入系统事件要求人工处理。

## 9. 运行模式

- paper：本地模拟撮合，不发真实订单（venues/paper.py 通用模拟适配）。paper 价格驱动（H6）：优先公开行情只读接口（venues/quotes.py，HL 家族/Aster/Lighter），失败或无公开端点（Arcus）回退随机游走，再回退静态价。
- DRY_RUN=true：走全部真实行情与决策、记录意图，不下单。
- live：DRY_RUN=false 且对应场所 mode=live 才真实下单；切换前必须过实盘预检（签名、时钟、余额、精度），且主入口会校验预检通过标记 data/preflight-ok.json 的指纹与当前 config/密钥一致（M3），不一致拒绝启动。
- 全局停机状态落盘 data/killswitch.json（M3）：重启保持停机，须面板 reset 或 `python -m detsp --reset-killswitch` 人工解除。
- entropy-arb 原设计无模拟模式，本系统为溢价套利补齐 paper/DRY_RUN。

## 10. 配置

- .env 只放密钥与连接参数（见 .env.example，7 所字段分开，值留空）。
- config.yaml 放策略与风控参数，加载时严格校验：未知键、未知 venue_id、非法 mode 直接报错退出（core/config.py）。任务名必填唯一、同一 (venue, symbol) 被两个任务占用在加载时直接报错（H4）。
- caps：config.yaml 顶层三层名义上限（USD：global_usd / venues / strategies，键为任务名），经 Engine.from_config 接线进 Executor；策略层口径统一为「该策略全部腿的名义合计」（开仓检查时把其余腿现有持仓名义一并计入，不再只看新开的这一腿）；中性网格启动另按双腿总名义估算硬校验，超限拒绝启动。

## 11. 许可

- 本项目为**个人自用、非商业用途**；主体 MIT（见 LICENSE）。
- 借鉴/移植来源（署名保留，见 NOTICE）：
  - Parallax Hedge（ZAIJIN88/Arcus-Entropy-Lighter-，MIT）：Arcus、RHC Lighter 的签名、下单、风控、账本实现。
  - WGALL-ZJ005（ZAIJIN88，MIT）：网格逻辑（Node 版逻辑移植到 Python，未复制 Node 代码）。
  - entropy-arb（your-quantguy，MIT）：Hyperliquid 家族与 Lighter 主网适配、溢价套利引擎、盘口录制/分析。
  - perp-dex-tools（your-quantguy，**Non-Commercial License 非商业许可**，全文见 LICENSES/perp-dex-tools.txt）：溢价套利的 maker 主腿执行方式、Aster/Lighter 适配的对照校准（轮换对冲的 max_position_qty 逐步建仓语义已随该策略于第十一轮删除）。本项目为个人自用、非商业，依其许可使用并保留署名；若转商业用途须先取得作者单独授权或移除上述部分。

## 12. 实现状态

| 模块 | 状态 |
|---|---|
| core（配置/接口/登记/风控/账本/引擎） | 已完成（第一批）；审查清理：旧「骨架模式」（Engine.run_once/decide 快照式流程）无调用方且与实路径不符，已删除；claim_api_key（api_key_index 占用登记）从未接线，已删除（见第 5 节如实说明） |
| core/execution 统一执行层 | 已完成（第三批）：claim 归属校验、KillSwitch/强平距离/三层上限、成交记账、结果未知不重发先对账、部分成交按实际量。审计修复（S1）：异常翻译统一口径（unknown_outcome_guard）+ Executor 兜底对账定性；强平距离公共函数 position_corridor/estimate_open_corridor（带 mmr，H7/M5）；cancel_unregistered 启动对账撤单（S3） |
| core/engine 执行调度 | 已完成（第三批）：按 config.yaml 的 tasks 组装三策略，单任务异常隔离、连续错误停用并触发全局停机、安全动作优先（停机时 allow_open=False 照常撤单/平仓）。审计修复：caps 从 config 接线（H1）、state_dir 停机落盘（M3）、任务启动 S3 对账事件汇总 events、面板命令队列串行执行（M4）。第十四轮修复：统计库与 risk.max_consecutive_errors 此前未接线到任务组装，已补透传 |
| venues/paper 通用模拟 | 已完成 |
| venues/hl（Hyperliquid 本体 / Entropy / trade.xyz） | live 已完成（第二批）：基于官方 hyperliquid-python-sdk 0.24 的 Info/Exchange；签名/请求构造有 mock 单测，未做实盘小额验证 |
| venues/lighter（主网 / Robinhood 链） | live 已完成（第二批）：REST 读 + lighter-sdk 1.1.2 签名下单；市价单异步结算，成交以持仓对账为准；未做实盘小额验证。第五批对照 perp-dex-tools（SDK 1.0.4 时代实现，仅吸收不冲突的逻辑）：数量换算改截断、订单状态字段归一化 |
| venues/arcus | live 已完成（第二批）：Ed25519 签名（含载荷字节级单测）、精度、强平价复算、REST 下单/撤单/setLeverage；未做实盘小额验证 |
| venues/aster | live 已完成（第二批）：依据官方 V1（Legacy）文档自写，HMAC-SHA256 签名有官方示例向量对拍；未做实盘小额验证。第五批按 perp-dex-tools 实盘实现对照校准：签名串/端点/参数一致，补市价单 avgPrice 成交价与 reduceOnly 回读 |
| strategies（三策略） | **策略层推倒重写（用户定案）**：删除普通网格 grid 与单所刷量 volume_loop；轮换对冲 rotation_hedge 于第十一轮整删，现为三策略，全部为双腿对冲结构——中性网格（网格腿铺单/成交补单/区间外 close 与 reduce_only 回收/人工补格 + 对冲腿按净持仓调平、死区与裸露超时强制对冲、双腿强平监控、双腿总名义 caps 硬校验）；溢价套利（价差带/持续门/库存阶梯/对账净 delta 修正，execution_mode=maker 主腿开仓：全成对冲跟上、部分成按最终量对冲、超时撤单放弃，平仓仍双腿 taker；仓位保证金派生、单笔上限 order_cap_pct）；资金费套利（正面五项配置：最大开仓价差/最低资金费率差进出共用/强平保护/数量偏差平仓/自动重开，启动对账净 delta 修正后接管）。三策略面板停止语义统一为撤自有挂单 + 平全部持仓。审查修复（2026-11-04，三批全修，见审查汇总.md 修复记录）：费率闸门与逐所口径标注、结算宽限期、市价单成交发现、削腿滑点保护、网格启动预检/skipBand/增量记账、引擎停用收尾等全部落地。385 个测试全绿（2026-11-08 第十四轮）。尚未做实盘小额验证 |
| core/stats_library 两个统计库 | 已完成（第十一轮）：同一批量快照录制层——中枢库落全部组合价差分钟行、每小时按 analyze 同口径重算过去 24 小时（录满 3 小时准入）；费差库落净费差小时行、7 天窗口算持续性（硬关：中位持续 ≥ 回本 × 2，样本不足 24 小时自动档不选）。费率口径为代码内置常量全所统一 taker 5 / maker 2 bps（core/fee_table，第十二轮撤场所费率表）。面板进程内 LibraryService 后台线程录制，状态在「数据库」页 |
| core/market_scanner 行情扫描 | 已完成：只读公开接口批量快照（7 所价格/资金费/上市/最高杠杆与保证金模式），单所失败隔离；供各策略页机会区、最佳机会与两个统计库的录制层共用 |
| venues/paper | 第三批增强：限价单挂单撮合（改价穿价成交）、poll_order 终态查询、资金费/杠杆可设。审计修复（H6）：价格驱动三级——公开行情只读（venues/quotes）→ 随机游走兜底（可设种子）→ 静态价；未知 symbol 不再 KeyError |
| venues/dryrun（DRY_RUN 代理） | 已完成（第四批）：live 场所读实盘行情/账户、下单走本地 PaperVenue 模拟并按实盘中价同步撮合价，落实第 9 节 DRY_RUN 语义（组装见 venues/factory.py 的 dry_run 参数） |
| web 网页面板 | 已完成（第四批）：与引擎**同进程共享对象**（Engine/venues/Ledger 同一批实例，不另开进程读状态文件），标准库 http.server，强制只绑回环地址；接口 /api/overview、/api/tasks、任务 start/stop、/api/killswitch(+reset)、/api/fills；账本连接已做线程安全（引擎线程写、面板线程读）。审计修复（H2/M4）：随机 token（POST 必带 X-API-Token）、Host 回环字面校验、POST Origin 同源校验；启停任务经引擎命令队列串行执行，不在 HTTP 线程组装/下单。**交互化（见第 13 节）**：参数规格单一事实源（strategies/specs）驱动动态表单与校验；面板任务增删改 + 持久化（data/panel_tasks.yaml）与合并加载；总览持仓归属标注与手动平仓；场所 mode 与密钥有意不放面板 |
| 程序入口与一键启动 | 已完成（第四批）：`python -m detsp`（--config/--env/--paper/--no-web/--db/--reset-killswitch）；一键启动.bat（纯 ASCII）+ start.ps1、start.sh，首次自动建 venv 装依赖并生成 .env/config.yaml。审计修复（M3）：live 真金模式启动前校验预检通过标记（指纹绑定 config+密钥） |
| tools 盘口录制/溢价分析 | 已完成（第四批）：detsp.tools.recorder 每秒采样、每分钟一行 CSV（双边盘口与溢价 OHLC/均值、可成交 edge）；detsp.tools.analyze 输出溢价分布、候选带触发次数与建议 midline/upper/lower（扣费口径） |
| core/preflight 实盘预检 | 已完成（第四批）：`python -m detsp.preflight`，逐所查凭据齐全（不打印值）、签名自检（只签不发）、时钟偏差（>30 秒失败）、行情连通、账户快照（live）、精度与最小量；paper 只跑离线项；任一失败退出码非 0。审计修复（M3）：含 live 场所且预检通过时写 data/preflight-ok.json（时间 + config 指纹），供主入口 live 启动门校验。审查修复：新增资金费专项——费率周期自检（全市场快照地板价年化反查结算周期，异常按失败处理）与费率符号抽查提示（见下方费率口径表注） |

**启动恢复对账语义**：进程重启后内存归属登记为空，但交易所的挂单/持仓还在。各任务 start 时先对账再动作，事件记入 engine.events 并在面板总览可见：
- 中性网格：先撤销双腿该 (venue, symbol) 下未登记的遗留挂单再铺单（防新旧两套单叠加）；网格腿发现已有持仓且任务未开 takeover_position 时，持仓登记为「未托管」，不铺单、不动对冲腿，直到人工处理或显式接管；接管时对冲腿按目标自动调平。全局停机时未托管持仓仍会被平（安全优先）。
- 溢价套利：按已有持仓初始化，孤腿/不平由安全检查在下一 tick 起处理；净 delta 由定期对账自动修正、强平距离由 tick 监控接管。
- 资金费套利：已有双腿先做净 delta 修正（不等量削多余腿、孤腿平掉），等量双腿按真实方向接管并继续监控，不因当前最优方向不同而强制平仓。
- 「重启会重新 claim 并对账」自此成立：claim 解决进程内互斥，对账解决与交易所真实状态的一致性。

**资金费口径表（逐所，funding() 的 rate_kind）**：选向只允许同口径相减（第 6 节费率闸门），下表是 venue 层的实现结果：

| 场所 | 取值来源 | rate_kind | 结算周期 | 备注 |
|---|---|---|---|---|
| Hyperliquid 本体 / Entropy / trade.xyz | predictedFundings 接口的 Hyperliquid 条目（下期预测）；接口不可用时回退 metaAndAssetCtxs 的 funding 字段并改标 current | predicted（回退时 current） | 1 小时 | 预测接口为原始 POST /info 调用，待实盘只读验证 |
| Arcus | /v1/markets 的 nextFundingRate；缺失时回退 fundingRate 并改标 current | predicted（回退时 current） | 1 小时 | — |
| Lighter 主网 / Robinhood 链 | /api/v1/funding-rates 费率表（当期费率，无预测字段） | current | 1 小时（两部署统一按本地常量口径） | **符号约定未经真实接口实证**：见下方待实证项 |
| Aster | premiumIndex 的 lastFundingRate（上一期已结算值，该接口无预测字段，已据此判定） | last_settled | 8 小时 | 因口径不同，与预测/当期口径的场所组对时闸门会拒绝选向开仓——这是有意行为：不拿过期费率选方向 |

待实证项（实盘前必验，不要当已解决）：
- **费率符号约定**：全系统约定「费率为正 = 多头付费」。各所原生符号（尤其 Lighter）无法离线实证，代码层只能防御：预检固定输出「费率符号抽查」提示项，首次小额实盘前必须逐所把交易所官网显示的资金费方向与本系统 funding() 返回值核对一致，核对不上先停用该所的 carry 任务。
- 费率周期自检只能发现「整倍数级」的周期误读（地板价年化反查），发现不了符号错误，也代替不了逐所小额实盘验证。

**计划内功能至此全部完成。** 遗留项（均为计划外/需真实账户验证）：
- 7 所实盘小额验证（全部 live 适配只过了 mock 单测与本地签名自检，未用真实账户下过单）。
- Aster V3（EIP-712）迁移评估：当前实现 V1（Legacy）HMAC，官方主推 V3，V1 密钥可用性以平台政策为准。
- Arcus 按 clientId 撤单的 HTTP body 形状核实（签名载荷已验证，body 按 orderId 版对称组装）。
- 溢价套利深度切片：Book 接口无深度数据，切片名义暂以 order_cap_pct × 派生仓位代入 slice_notional，take_fraction 暂不生效，接深度数据后再改。

已知未核实/限制：
- Aster：官方现主推 V3（EIP-712），本项目按既定方案实现 V1 HMAC；V1 密钥可用性以平台当前政策为准。exchangeInfo 不含最大杠杆（记 0=未知）；按 clientId 撤单未实现（官方 V1 撤单按 orderId+symbol，已实现）。
- Arcus：按 clientId 撤单的 HTTP body 形状未在参考代码中出现（其只用 orderId 撤单），签名载荷已验证、body 按对称形式组装，上实盘前需小额验证。
- Lighter：accountActiveOrders 需要认证令牌（已用 signer 生成）；市价单成交确认走对账，不做同步等待。
- Hyperliquid：markets 的 price_step 用官方价格规则允许的最细小数位近似（HL 无统一 tick 概念）。


## 13. 面板任务与交互操作（web/）

面板是可交互的操作台，不只是状态展示：

- **参数规格单一事实源**（strategies/specs.py）：三个策略的 Params
  dataclass 是字段与默认值的唯一定义，规格在其之上叠加中文说明、
  分区（zone：选择两腿/仓位设置/策略配置/更多配置/系统值）与
  枚举/场所类型；面板表单（GET /api/strategy-specs）与参数
  校验（validate_params，config 组装 build_task 与面板新建/编辑
  共用）都从这份规格生成，杜绝两处口径漂移。一致性测试保证规格
  字段与 Params 字段/默认值/必填性恒等。
- **两类任务**：config.yaml 里的任务（来源 config）面板只能启停；
  面板新建的任务（来源 panel）可编辑/删除。面板任务独立存
  data/panel_tasks.yaml（原子写：临时文件 + rename），面板不回写
  config.yaml。
- **合并加载规则**：引擎启动先组装 config 任务，再由
  Engine.load_panel_tasks 合并面板任务。面板任务与 config 任务
  重名 → 跳过并在系统事件报错（面板列表里以来源 panel、状态
  error 并存展示，可编辑/删除）；占用 (venue,symbol) 冲突、参数
  非法、超 caps → 只跳过该条并记 errors + 事件，绝不拖垮 config
  任务。
- **运行时增删**：Engine.add_task_dynamic / remove_task_dynamic
  与启动组装走同一条 build_task 路径（同一套规格校验、归属冲突、
  caps、启动对账语义）；删除时统一释放该任务的全部归属 claim。
  面板侧守卫：运行中不许删（先停）、有持仓不许删（先平）。
  编辑运行中任务 = 先停（撤单+平仓）再按新参数重组；重组失败不
  保存新参数，旧参数保留可一键启动恢复。全部面板写操作经引擎
  命令队列串行执行，并记入系统事件（engine.events）。
- **手动平仓**：总览持仓带归属标注。平仓走 Executor.close_position：
  有归属按任务名义平（任务仍在运行时界面明确提示可能被任务重新
  开仓/对账处理）；无归属是手动操作，界面明确标注「平掉该场所
  该币种全部持仓」，平后立即释放临时 claim。
- **安全边界（有意不做）**：场所 mode（paper/live）切换不放面板
  （切实盘仍走改配置 + 预检门，防手滑）；密钥可经设置页填写写回
  .env，但任何接口都不返回密钥值（只报已填/未填）。
- **数据库页与设置页**：行情扫描由 core/market_scanner
  只读公开接口（无密钥、20 秒缓存、单所失败隔离），给每币种的
  7 所价格/资金费（bps/小时 + rate_kind 口径）与两个计算列——最大
  价差组合、最优资金费组合（优先同口径组合，混口径明确标注）；
  中性网格的 24h 高低走 HL candleSnapshot。扫描数据供各策略页
  机会区与两个统计库录制层使用（/api/market 接口保留）。
  「数据库」页（边栏，设置上面）展示中枢库/费差库状态并可手动
  启停数据服务；设置页只剩密钥：字段清单在
  core/secret_fields（与 preflight/env 检查同一口径），保存走原子
  写回 .env 并记系统事件（只记字段名）。
- **左侧边栏 + 每策略一页**：边栏顺序为 总览 / 中性网格 / 溢价
  套利 / 资金费套利 / 策略记录 / 数据库 / 设置（无数据入口、无
  通用新建任务，新建只能从各策略页发起）。策略页
  由通用渲染器按 specs 的 PAGE_META 与字段 zone 生成，自上而下：
  运行模式 → 选择两腿 → 仓位设置（币种 | 保证金、杠杆 | 杠杆
  横排两列，统计行在顶、每腿最高杠杆与保证金模式、数据块在底）
  → 策略配置（正面只留核心项）→ 更多配置（折叠）→ 交易预览 →
  运行按钮 → 本策略任务列表。运行模式四值：manual（自主）/
  semi_fixed（固腿固标）/ semi_auto（固腿优标）/ full_auto
  （优腿优标），网格只有自主；溢价与资金费的三档显示名即固腿
  固标/固腿优标/优腿优标。仓位金额语义为保证金：仓位 = 保证金
  × 基准腿杠杆。预设档位已全砍（第十二轮），优腿优标没填的
  参数按内置中性档（NEUTRAL_DEFAULTS）组装。极客终端风（近黑
  底、终端绿、直角、等宽字体、高密度），三策略默认值逐项
  对齐原数据（见《原仓配置对照表.md》）。
