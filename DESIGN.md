# DeTSP 设计文档

统一永续合约交易系统：一套代码、一个引擎、一个账本，承载四种策略（中性网格、轮换对冲刷量、溢价套利、资金费 carry），接入 7 个交易场所。本项目为个人自用、非商业用途。

## 1. 目标与边界

- 本地单机运行（默认只监听 127.0.0.1），单用户，不托管、不对外提供服务。
- 四种策略共用交易所适配、风控、账本与配置，不再各带一套签名/下单/状态。
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

## 3. 四种策略（strategies/）

四个策略全部是双腿/对冲结构：方向风险靠第二条腿对锁，不留单边裸仓。每个策略注明来源仓库与重组关系；普通网格与单所刷量已移除（单边裸仓不符合本系统的中性原则）。

### 3.1 中性网格 hedged_grid.py
- 网格腿在区间 [lower_price, upper_price] 内等差铺单：long / short / long_short 三模式，成交后在相邻格挂反向单；对冲腿每个 tick 读网格腿真实净持仓，目标 = −网格净持仓，差额超 hedge_deadband_qty（死区）时用 taker 市价调平。
- 残差达不到对冲所最小下单量时等累积；裸露持续超过 max_unhedged_seconds 时报警并强制对冲。
- 区间外动作二选一：close（撤双腿单、平双腿停机）或 reduce_only（网格腿只挂只减仓回收阶梯，不加仓、不当止损，对冲腿继续跟随调平）。
- 双腿都做强平距离监控；caps 启动硬校验按双腿总名义估算。
- 来源：网格铺单语义来自 WGALL-ZJ005（ZAIJIN88，MIT）的网格逻辑；对锁调平来自 Parallax Hedge（ZAIJIN88，MIT）的双腿结构。两者重组为一个策略。

### 3.2 轮换对冲刷量 rotation_hedge.py
- 在两个场所开方向相反、数量相同的永续仓位，按持有时长轮换开平，以低磨损产生交易量，顺带收取两所资金费差。
- 开仓方向按两边净资金费比较选边（费率统一换算到每小时口径）；双 taker 开仓与 Arcus 腿 maker 挂单（ALO）两种模式，maker 部分成交按最终量对冲。
- 风控：开仓前强平距离门槛、两所价差闸门、开仓后复核、持仓监控双腿同平、孤腿无条件市价平。
- 可选 max_position_qty（>0）：首轮开仓后按 accumulate_interval_seconds 间隔逐档同向加仓（每档仍为 notional 口径、末档截断），两腿达到上限后停止加仓、转入正常持有→到期平仓循环；持有期从首轮开仓起算。逐步建仓语义源自 perp-dex-tools 的 hedge --max-position（非商业许可，个人自用）。
- 实现移植自 Parallax Hedge（ZAIJIN88/Arcus-Entropy-Lighter-，MIT）。

### 3.3 溢价套利 premium_arb.py
- 一腿为基准场所，另一腿为对冲场所；溢价 premium_bps = (A 价 / B 价 − 1) × 10000。
- 信号带由三参数定义：midline_bps（溢价中枢，必须先录制数据实测，不假设为 0）、upper_bps、lower_bps；触发判断用可成交价（bid/ask）且扣除双边 taker 手续费，信号须持续 persist_seconds 才触发；仓位受 max_position_usd 与库存阶梯约束；定期对账修正净 delta。
- 执行模式 execution_mode：taker（默认）双腿 taker 立即开平；maker 时**开仓**的基准腿先以 post-only 限价挂在盘口内侧（买贴 bid、卖贴 ask，可配 maker_offset_ticks）等成交，全成后对冲腿 taker 跟上，部分成交先对冲已成部分再撤余单结束本轮，超时未成撤单放弃本轮。**平仓与反向减仓永远走双腿 taker**，保证退出确定性。
- 来源：价差带逻辑移植自 entropy-arb（your-quantguy，MIT）；maker 主腿执行方式源自 perp-dex-tools（非商业许可，个人自用）的 hedge 模式。盘口录制与分析工具思路同样来自 entropy-arb。

### 3.4 资金费 carry funding_carry.py
- 不刷量、不轮换：选两所资金费差最大的一组方向（多腿在费率低侧、空腿在费率高侧，费率统一换算到每小时），双腿 taker 等量开仓长期持有，赚组合每小时净收的资金费差。
- 入场门槛 min_entry_edge_bps_per_hour（bps/小时）；退出带迟滞：优势连续 exit_confirm_checks 次检查低于 exit_edge_bps_per_hour 才双平；max_hold_seconds 到期也平；平仓后冷却 cooldown_seconds，reopen 决定是否自动重开。
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

- 每笔订单、每个持仓在登记表里打 strategy 标签（任务名）与 (venue, account, symbol) 键。
- 某策略要接管某 (venue, account, symbol) 前必须 claim；已被其他策略占用则抛 ConflictError，拒绝启动。
- 撤单/平仓只能操作自己名下的订单与持仓：网格启动时的「撤销该市场全部挂单」只限自有订单；对冲的「孤腿抢救」只认自有腿，不会误平网格仓。
- 同一 Lighter 账户下各策略必须用不同 api_key_index（nonce 隔离），登记表记录占用。

## 6. 全局风控（core/risk.py）

安全动作永远压过交易动作，风控为纯函数优先、便于单测：

- 强平距离（corridor）：多腿取最近值；开仓门槛 MIN_CORRIDOR_PCT、平仓线 CLOSE_CORRIDOR_PCT。
- 单腿检测：对冲类仓位任一腿缺失/数量不平 → 立即触发减腿流程。
- 仓位上限：每所、每策略、全局三层 notional 上限。
- 全局停机：KillSwitch 触发后引擎拒绝一切新开仓，只允许撤单/平仓；连续错误达阈值自动停机。
- 价差闸门：两所中价相差超阈值不开仓（防同名不同标的）。

## 7. 统一账本（core/ledger.py）

- SQLite 单库（data/detsp.db），fills 表记录每笔成交：时间、场所、账户、币种、策略、方向、数量、价格、手续费、订单号。
- 四策略盈亏、磨损（每万美元）、交易量统一从账本聚合；与交易所持仓定期对账（reconcile）。

## 8. 引擎（core/engine.py）

每轮循环：快照（行情+持仓）→ 风控裁决 → 策略决策 → 归属校验 → 执行 → 记账。
单策略/单币种异常只隔离该任务，不拖停其他任务的风控与孤腿处理。

## 9. 运行模式

- paper：本地模拟撮合，不发真实订单（venues/paper.py 通用模拟适配）。paper 价格驱动（H6）：优先公开行情只读接口（venues/quotes.py，HL 家族/Aster/Lighter），失败或无公开端点（Arcus）回退随机游走，再回退静态价。
- DRY_RUN=true：走全部真实行情与决策、记录意图，不下单。
- live：DRY_RUN=false 且对应场所 mode=live 才真实下单；切换前必须过实盘预检（签名、时钟、余额、精度），且主入口会校验预检通过标记 data/preflight-ok.json 的指纹与当前 config/密钥一致（M3），不一致拒绝启动。
- 全局停机状态落盘 data/killswitch.json（M3）：重启保持停机，须面板 reset 或 `python -m detsp --reset-killswitch` 人工解除。
- entropy-arb 原设计无模拟模式，本系统为溢价套利补齐 paper/DRY_RUN。

## 10. 配置

- .env 只放密钥与连接参数（见 .env.example，7 所字段分开，值留空）。
- config.yaml 放策略与风控参数，加载时严格校验：未知键、未知 venue_id、非法 mode 直接报错退出（core/config.py）。任务名必填唯一、同一 (venue, symbol) 被两个任务占用在加载时直接报错（H4）。
- caps：config.yaml 顶层三层名义上限（USD：global_usd / venues / strategies，键为任务名），经 Engine.from_config 接线进 Executor；中性网格启动另按双腿总名义估算硬校验，超限拒绝启动。

## 11. 许可

- 本项目为**个人自用、非商业用途**；主体 MIT（见 LICENSE）。
- 借鉴/移植来源（署名保留，见 NOTICE）：
  - Parallax Hedge（ZAIJIN88/Arcus-Entropy-Lighter-，MIT）：Arcus、RHC Lighter 的签名、下单、风控、账本实现。
  - WGALL-ZJ005（ZAIJIN88，MIT）：网格逻辑（Node 版逻辑移植到 Python，未复制 Node 代码）。
  - entropy-arb（your-quantguy，MIT）：Hyperliquid 家族与 Lighter 主网适配、溢价套利引擎、盘口录制/分析。
  - perp-dex-tools（your-quantguy，**Non-Commercial License 非商业许可**，全文见 LICENSES/perp-dex-tools.txt）：溢价套利的 maker 主腿执行方式、轮换对冲的 max_position_qty 逐步建仓语义、Aster/Lighter 适配的对照校准。本项目为个人自用、非商业，依其许可使用并保留署名；若转商业用途须先取得作者单独授权或移除上述部分。

## 12. 实现状态

| 模块 | 状态 |
|---|---|
| core（配置/接口/登记/风控/账本/引擎骨架） | 已完成（第一批） |
| core/execution 统一执行层 | 已完成（第三批）：claim 归属校验、KillSwitch/强平距离/三层上限、成交记账、结果未知不重发先对账、部分成交按实际量。审计修复（S1）：异常翻译统一口径（unknown_outcome_guard）+ Executor 兜底对账定性；强平距离公共函数 position_corridor/estimate_open_corridor（带 mmr，H7/M5）；cancel_unregistered 启动对账撤单（S3） |
| core/engine 执行调度 | 已完成（第三批）：按 config.yaml 的 tasks 组装四策略，单任务异常隔离、连续错误停用并触发全局停机、安全动作优先（停机时 allow_open=False 照常撤单/平仓）。审计修复：caps 从 config 接线（H1）、state_dir 停机落盘（M3）、任务启动 S3 对账事件汇总 events、面板命令队列串行执行（M4） |
| venues/paper 通用模拟 | 已完成 |
| venues/hl（Hyperliquid 本体 / Entropy / trade.xyz） | live 已完成（第二批）：基于官方 hyperliquid-python-sdk 0.24 的 Info/Exchange；签名/请求构造有 mock 单测，未做实盘小额验证 |
| venues/lighter（主网 / Robinhood 链） | live 已完成（第二批）：REST 读 + lighter-sdk 1.1.2 签名下单；市价单异步结算，成交以持仓对账为准；未做实盘小额验证。第五批对照 perp-dex-tools（SDK 1.0.4 时代实现，仅吸收不冲突的逻辑）：数量换算改截断、订单状态字段归一化 |
| venues/arcus | live 已完成（第二批）：Ed25519 签名（含载荷字节级单测）、精度、强平价复算、REST 下单/撤单/setLeverage；未做实盘小额验证 |
| venues/aster | live 已完成（第二批）：依据官方 V1（Legacy）文档自写，HMAC-SHA256 签名有官方示例向量对拍；未做实盘小额验证。第五批按 perp-dex-tools 实盘实现对照校准：签名串/端点/参数一致，补市价单 avgPrice 成交价与 reduceOnly 回读 |
| strategies（四策略） | **策略层推倒重写（用户定案）**：删除普通网格 grid 与单所刷量 volume_loop，新四策略全部为双腿对冲结构——中性网格（网格腿铺单/成交补单/区间外 close 与 reduce_only 回收/人工补格 + 对冲腿按净持仓调平、死区与裸露超时强制对冲、双腿强平监控、双腿总名义 caps 硬校验）；溢价套利（保留价差带/持续门/库存阶梯/对账净 delta 修正，新增 execution_mode=maker 主腿开仓：全成对冲跟上、部分成按最终量对冲、超时撤单放弃，平仓仍双腿 taker）；轮换对冲（行为等价保留：双 taker 与 maker+对冲、到期轮换、孤腿平、开仓后复核、逐步建仓）；资金费 carry（全新：资金费优势入场门槛 + 退出迟滞确认、检查周期、最长持有、冷却重开、启动对账净 delta 修正后接管）。四策略面板停止语义统一为撤自有挂单 + 平全部持仓。191 个测试全绿。尚未做实盘小额验证 |
| venues/paper | 第三批增强：限价单挂单撮合（改价穿价成交）、poll_order 终态查询、资金费/杠杆可设。审计修复（H6）：价格驱动三级——公开行情只读（venues/quotes）→ 随机游走兜底（可设种子）→ 静态价；未知 symbol 不再 KeyError |
| venues/dryrun（DRY_RUN 代理） | 已完成（第四批）：live 场所读实盘行情/账户、下单走本地 PaperVenue 模拟并按实盘中价同步撮合价，落实第 9 节 DRY_RUN 语义（组装见 venues/factory.py 的 dry_run 参数） |
| web 网页面板 | 已完成（第四批）：与引擎**同进程共享对象**（Engine/venues/Ledger 同一批实例，不另开进程读状态文件），标准库 http.server，强制只绑回环地址；接口 /api/overview、/api/tasks、任务 start/stop、/api/killswitch(+reset)、/api/fills；账本连接已做线程安全（引擎线程写、面板线程读）。审计修复（H2/M4）：随机 token（POST 必带 X-API-Token）、Host 回环字面校验、POST Origin 同源校验；启停任务经引擎命令队列串行执行，不在 HTTP 线程组装/下单 |
| 程序入口与一键启动 | 已完成（第四批）：`python -m detsp`（--config/--env/--paper/--no-web/--db/--reset-killswitch）；一键启动.bat（纯 ASCII）+ start.ps1、start.sh，首次自动建 venv 装依赖并生成 .env/config.yaml。审计修复（M3）：live 真金模式启动前校验预检通过标记（指纹绑定 config+密钥） |
| tools 盘口录制/溢价分析 | 已完成（第四批）：detsp.tools.recorder 每秒采样、每分钟一行 CSV（双边盘口与溢价 OHLC/均值、可成交 edge）；detsp.tools.analyze 输出溢价分布、候选带触发次数与建议 midline/upper/lower（扣费口径） |
| core/preflight 实盘预检 | 已完成（第四批）：`python -m detsp.preflight`，逐所查凭据齐全（不打印值）、签名自检（只签不发）、时钟偏差（>30 秒失败）、行情连通、账户快照（live）、精度与最小量；paper 只跑离线项；任一失败退出码非 0。审计修复（M3）：含 live 场所且预检通过时写 data/preflight-ok.json（时间 + config 指纹），供主入口 live 启动门校验 |

**启动恢复对账语义**：进程重启后内存归属登记为空，但交易所的挂单/持仓还在。各任务 start 时先对账再动作，事件记入 engine.events 并在面板总览可见：
- 中性网格：先撤销双腿该 (venue, symbol) 下未登记的遗留挂单再铺单（防新旧两套单叠加）；网格腿发现已有持仓且任务未开 takeover_position 时，持仓登记为「未托管」，不铺单、不动对冲腿，直到人工处理或显式接管；接管时对冲腿按目标自动调平。全局停机时未托管持仓仍会被平（安全优先）。
- 轮换对冲 / 溢价套利：按已有持仓初始化——轮换直接进入 open 阶段（opened_at 重置），孤腿/不平由安全检查在下一 tick 起处理；套利的净 delta 由定期对账自动修正、强平距离由 tick 监控接管。
- 资金费 carry：已有双腿先做净 delta 修正（不等量削多余腿、孤腿平掉），等量双腿按真实方向接管并继续监控，不因当前最优方向不同而强制平仓。
- 「重启会重新 claim 并对账」自此成立：claim 解决进程内互斥，对账解决与交易所真实状态的一致性。

**计划内功能至此全部完成。** 遗留项（均为计划外/需真实账户验证）：
- 7 所实盘小额验证（全部 live 适配只过了 mock 单测与本地签名自检，未用真实账户下过单）。
- Aster V3（EIP-712）迁移评估：当前实现 V1（Legacy）HMAC，官方主推 V3，V1 密钥可用性以平台政策为准。
- Arcus 按 clientId 撤单的 HTTP body 形状核实（签名载荷已验证，body 按 orderId 版对称组装）。
- 溢价套利深度切片：Book 接口无深度数据，切片名义暂以 max_order_notional_usd 代入 slice_notional，接深度数据后再改。

已知未核实/限制：
- Aster：官方现主推 V3（EIP-712），本项目按既定方案实现 V1 HMAC；V1 密钥可用性以平台当前政策为准。exchangeInfo 不含最大杠杆（记 0=未知）；按 clientId 撤单未实现（官方 V1 撤单按 orderId+symbol，已实现）。
- Arcus：按 clientId 撤单的 HTTP body 形状未在参考代码中出现（其只用 orderId 撤单），签名载荷已验证、body 按对称形式组装，上实盘前需小额验证。
- Lighter：accountActiveOrders 需要认证令牌（已用 signer 生成）；市价单成交确认走对账，不做同步等待。
- Hyperliquid：markets 的 price_step 用官方价格规则允许的最细小数位近似（HL 无统一 tick 概念）。

