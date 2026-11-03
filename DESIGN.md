# DeTSP 设计文档

统一永续合约交易系统：一套代码、一个引擎、一个账本，承载三种策略（网格、轮换对冲刷量、溢价套利），接入 7 个交易场所。

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

### 3.1 网格 grid.py
- 等差网格：中性 / 做多 / 做空三模式；区间 [lower, upper] 切 grid_count 格。
- 成交后在相邻格挂反向单；区间外动作二选一：close（撤单平仓停机）或 recover（只减仓回收阶梯，不加仓、不当止损）。
- 逻辑移植自 WGALL 的 grid.js/bot.js（MIT），用 Python 重写，不复制 Node 代码。

### 3.2 轮换对冲刷量 rotation_hedge.py
- 在两个场所开方向相反、数量相同的永续仓位，按持有时长轮换开平，以低磨损产生交易量，顺带收取两所资金费差。
- 开仓方向按两边净资金费比较选边；Arcus 腿可选 maker 挂单（ALO）降费。
- 风控：开仓前强平距离门槛、两所价差闸门、开仓后复核、持仓监控双腿同平、孤腿无条件市价平。
- 实现移植自 Parallax Hedge（MIT）。

### 3.3 溢价套利 premium_arb.py
- 一腿为基准场所，另一腿为对冲场所；溢价 premium_bps = (A 价 / B 价 − 1) × 10000。
- 信号带由三参数定义：midline_bps（溢价中枢，必须先录制数据实测，不假设为 0）、upper_bps、lower_bps。
- 触发判断用可成交价（bid/ask）且扣除双边 taker 手续费；溢价回归时反向平仓。
- 附盘口录制（1 分钟 CSV）与分析工具思路，移植自 entropy-arb（MIT）。

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

- 每笔订单、每个持仓在登记表里打 strategy 标签（grid / rotation_hedge / premium_arb）与 (venue, account, symbol) 键。
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
- 三策略盈亏、磨损（每万美元）、交易量统一从账本聚合；与交易所持仓定期对账（reconcile）。

## 8. 引擎（core/engine.py）

每轮循环：快照（行情+持仓）→ 风控裁决 → 策略决策 → 归属校验 → 执行 → 记账。
单策略/单币种异常只隔离该任务，不拖停其他任务的风控与孤腿处理。

## 9. 运行模式

- paper：本地模拟撮合，不发真实订单（venues/paper.py 通用模拟适配）。
- DRY_RUN=true：走全部真实行情与决策、记录意图，不下单。
- live：DRY_RUN=false 且对应场所 mode=live 才真实下单；切换前必须过实盘预检（签名、时钟、余额、精度）。
- entropy-arb 原设计无模拟模式，本系统为溢价套利补齐 paper/DRY_RUN。

## 10. 配置

- .env 只放密钥与连接参数（见 .env.example，7 所字段分开，值留空）。
- config.yaml 放策略与风控参数，加载时严格校验：未知键、未知 venue_id、非法 mode 直接报错退出（core/config.py）。

## 11. 许可

- 本项目 MIT（见 LICENSE）。
- 借鉴/移植来源（均为 MIT，署名保留，见 NOTICE）：
  - Parallax Hedge（ZAIJIN88/Arcus-Entropy-Lighter-）：Arcus、RHC Lighter 的签名、下单、风控、账本实现。
  - WGALL-ZJ005（ZAIJIN88）：网格逻辑（Node 版逻辑移植到 Python，未复制 Node 代码）。
  - entropy-arb（your-quantguy）：Hyperliquid 家族与 Lighter 主网适配、溢价套利引擎、盘口录制/分析。
- perp-dex-tools（your-quantguy）为非商业许可：本项目**仅参考其适配接口设计思想，未使用其任何代码**；Aster 适配为依据官方文档自写。

## 12. 实现状态

| 模块 | 状态 |
|---|---|
| core（配置/接口/登记/风控/账本/引擎骨架） | 已完成（第一批） |
| venues/paper 通用模拟 | 已完成 |
| venues/hl（Hyperliquid 本体 / Entropy / trade.xyz） | live 已完成（第二批）：基于官方 hyperliquid-python-sdk 0.24 的 Info/Exchange；签名/请求构造有 mock 单测，未做实盘小额验证 |
| venues/lighter（主网 / Robinhood 链） | live 已完成（第二批）：REST 读 + lighter-sdk 1.1.2 签名下单；市价单异步结算，成交以持仓对账为准；未做实盘小额验证 |
| venues/arcus | live 已完成（第二批）：Ed25519 签名（含载荷字节级单测）、精度、强平价复算、REST 下单/撤单/setLeverage；未做实盘小额验证 |
| venues/aster | live 已完成（第二批）：依据官方 V1（Legacy）文档自写，HMAC-SHA256 签名有官方示例向量对拍；未做实盘小额验证 |
| strategies（三策略决策骨架） | 决策函数已完成（第一批）；与 live 适配的执行编排在后续批次 |

已知未核实/限制：
- Aster：官方现主推 V3（EIP-712），本项目按既定方案实现 V1 HMAC；V1 密钥可用性以平台当前政策为准。exchangeInfo 不含最大杠杆（记 0=未知）；按 clientId 撤单未实现（官方 V1 撤单按 orderId+symbol，已实现）。
- Arcus：按 clientId 撤单的 HTTP body 形状未在参考代码中出现（其只用 orderId 撤单），签名载荷已验证、body 按对称形式组装，上实盘前需小额验证。
- Lighter：accountActiveOrders 需要认证令牌（已用 signer 生成）；市价单成交确认走对账，不做同步等待。
- Hyperliquid：markets 的 price_step 用官方价格规则允许的最细小数位近似（HL 无统一 tick 概念）。

