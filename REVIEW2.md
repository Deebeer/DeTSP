# DeTSP 对照审查报告（第二轮：对照四个原仓库）

> 修复状态：本报告的全部条目已于 2026-11-04 三批全修，逐条销账见 [审查汇总.md](审查汇总.md) 末尾「修复记录」。

审查日期：2026-11-03　审查方式：原仓源码与 DeTSP 逐项对照（只审不改）+ 实跑测试
原仓来源（本次重新克隆核查）：ZAIJIN88/WGALL-ZJ005（解压 ZIP，Node.js）、ZAIJIN88/Arcus-Entropy-Lighter-（Parallax Hedge Lighter 版 v1.5.1 与 Entropy 版）、your-quantguy/perp-dex-tools、your-quantguy/entropy-arb
与第一轮的关系：第一轮 REVIEW.md 是自查（代码↔文档↔逻辑闭环）；本轮只报对照才看得出的问题。与自查同源的新证据单独注明，不重复计数。

## 结论先行

- **测试现状**：本次实跑 `pytest` 共 **191 个全部通过**，与自查一致。
- **移植保真度总体不错**：网格档位与补单、carry 选向数学、轮换的走廊闸门组、套利的可成交信号口径、Arcus 签名与精度、HL 的 dex 参数化——逐项对照均与原仓一致（清单在文末）。
- **本轮新发现 12 条（高 2、中 5、低 5）**，集中在两类：一是 Parallax 在资金费上设的三道防护（合理性闸门、预测值口径、周期自检）DeTSP 只移植了「归一换算」一道；二是 WGALL 网格的一组启动守卫（保证金预检、贴价跳档等）在中性网格里没有对应物。
- **自查两条高优先级在本轮得到原仓侧的反证加强**：Parallax 对「下单后不能立即相信持仓」有成套设计（成交待确认标记 + 120 秒匹配宽限），且其调度器明确规定「任务停用时有仓必须平掉收尾、孤腿不停用也要平」——DeTSP 这两处与原仓差距见 G1/G2 同源证据（不重复计数）。

---

## 高

### H1. 资金费「合理性闸门」缺失：费率单位/解析出错时，carry 与轮换会按错误方向开仓且无拦截

- 原仓证据：`parallax_hedge/funding.py` 文件头自述这是「最容易出错、也最致命的一块」，并设硬闸门 `SANE_RATE_PER_INTERVAL = 0.05`：任一所单期费率绝对值超过 5% 即标 `suspect`，`DirectionChoice.tradable=False`，调度器 `decide()` 据此 `blocked` 拒绝开仓（「宁可不开，不能按错的方向开」）。
- DeTSP 证据：`strategies/funding_carry.py`、`strategies/rotation_hedge.py` 与 `core/risk.py` 全文无任何费率量级检查（grep `suspect/sane/合理区间` 无命中）；`venues/*` 的 `funding()` 把原生值直接透传进 `FundingInfo.rate`（`core/venue.py:91-97`），carry 的入场判断只看归一后的 bps/h 是否过门槛。
- 影响：某所接口把费率以百分数（1 表示 1%）或错误周期返回时，归一后会出现几十 bps/h 的假「优势」，carry 会满仓开进净付费方向并长期持有，轮换每轮选反方向。自查 L5 已提示 Lighter 符号未实证，本条是其结构性放大版：系统对费率数据没有任何防呆。

### H2. 费率取值口径跨所不一致：同一减法里混用「下期预测 / 上期已结算 / 当期」

- 原仓证据：`parallax_hedge/funding.py` 的 `FundingRow.effective_rate` 规则写明「开仓看的是下一期会收/付多少，有预测值就优先用预测值」，两所统一按此口径才进入 `choose_direction` 的减法。
- DeTSP 证据：四个场所 `funding()` 取的不是同一时点的量——Arcus 取 `nextFundingRate`（下期预测，`venues/arcus.py:236`，无值才退回当期）；Aster 取 `lastFundingRate`（**上一期已结算**，`venues/aster.py:182-185`，代码注释自认「官方无当期预测字段」）；HL 取 `ctx.funding`（当期，`venues/hl.py:181`）；Lighter 取费率表当前值（`venues/lighter.py:253`）。carry 与 rotation 直接把这些异口径值归一后相减选向。
- 影响：费率变盘点附近，「上期已结算」与「下期预测」可以符号相反。含 Aster 腿的组合会按已经过去的费率选向，选到的方向可能恰好是下一期净付的方向；原仓用统一预测口径正是为了避开这一点。此条 paper 测不出（paper 费率由测试按同一口径注入）。

---

## 中

### M1. 溢价分析工具触发计数口径偏离原仓：用分钟均值代替分钟最大值，建议带宽系统性偏窄

- 原仓证据：entropy-arb `tools/analyze.py:111-113` 用每分钟的**最大**可成交边际计触发次数（`sell_edge_max_bps - midline - fees`），理由是「那一分钟里真实可执行到的空间」；带宽建议按「候选带中第一个达到最少触发次数的带」选取。
- DeTSP 证据：录制器字段与原仓逐列一致（含 `sell_edge_max_bps`/`buy_edge_max_bps`，`tools/recorder.py:29-30`），但 `tools/analyze.py` 的 `analyze_rows` 触发计数用的是 `sell_edge_mean_bps`/`buy_edge_mean_bps`（**均值**列）。
- 影响：均值远小于最大值 → 同一候选带的触发次数被大幅低估 → 选取逻辑为了凑够最少触发次数会选到更窄的带 → 用户按建议参数跑套利时触发频率显著高于原方法预期，手续费磨损被低估。这是定参工具的口径错误，不是风格差异。

### M2. delta 修正（削腿）无滑点保护：原仓用带滑点上限的限价单，DeTSP 用市价单

- 原仓证据：entropy-arb `engine.py` 的 `_hedge()`（约 515-571 行）：削腿用 reduce-only **限价**单，限价 = 对手价 ×（1 ∓ `hedge_slippage_bps`）作滑点保护；且先挑「未被限流、持仓不平衡最大」的场所执行，跳过盘口陈旧/不可用的场所，数量按 step 向下对齐并校验最小名义额。
- DeTSP 证据：`strategies/premium_arb.py` `_fix_delta()` 发 `price=None` 的市价 reduce-only 单削大腿；`strategies/rotation_hedge.py` 的不平削腿同样是市价。两处都无滑点上限、无盘口新鲜度判断。
- 影响：削腿量通常不大，但在薄盘口/行情跳变时市价单的成交价无下限保护，一次异常滑点可吃掉多轮套利利润；原仓这层保护正是为该场景设的。

### M3. 中性网格缺 WGALL 的启动保证金预检：只查配置上限，不查账户真实可用权益

- 原仓证据：WGALL `src/bot.js` 启动段（约 370-381 行）：按网格名义敞口与杠杆算 `requiredMargin`，大于账户可用权益直接**拒绝启动**并给出差额，大于可用 80% 时告警。
- DeTSP 证据：`strategies/hedged_grid.py` 的 `start()` 只有 caps 硬校验（对照的是 `config.yaml` 里用户自填的上限数字），全文无 equity/available 读取（grep 无命中）；预检（`preflight`）只查权益**可读**，不做「本任务所需保证金 vs 可用」的对照。
- 影响：caps 填得宽松或忘填时，网格可以以远超账户承受力的总名义启动（中性网格还是双腿双倍占用），开仓中途因保证金不足被拒单/强平，状态比启动即拒绝更难收拾。

### M4. 网格初始铺单缺「贴价跳档」：现价附近档位会被挂上，原仓明确跳过

- 原仓证据：WGALL `src/grid.js` 的 `seedOrders` 有 `skipBand`（默认 0.25 × 间距）：距现价小于四分之一格距的层级一律不挂，「避免一挂出就贴住市场」。
- DeTSP 证据：`strategies/hedged_grid.py` 的 `plan_initial_orders`（约 102-120 行）对所有 `lv < current_price` / `lv > current_price` 的层级无条件铺单，无任何贴价跳过逻辑（grep `skip` 无命中）；回收结束 `_exit_recovery` 重铺同样走该函数。
- 影响：现价恰在某档下方 1 个 tick 时，该买单离 ask 只差一线：post-only 场所可能直接拒单（铺单部分失败、层级留空），或瞬间成交使补单节奏与预期错位。原仓用 1/4 格距的死区消掉这类边界噪声。

### M5. Arcus 成交发现精度低于原仓：原仓有按订单号匹配的成交feed，DeTSP 只有「挂单消失+持仓差」，且代码注释与原仓事实不符

- 原仓证据：Parallax `arcus.py` 的 `match_fills` 按订单号（或 `cid:clientId`）在 `/v1/fills` 里精确加总属于某张单的成交；`ledger.py` 以 `MATCH_GRACE_SEC = 120` 秒宽限做待确认成交的匹配（`exchanges.py:477` 起为 fills 读接口）。即原仓对 Arcus 有**逐单级**的成交确认通道。
- DeTSP 证据：`venues/arcus.py:457-472` 的 `poll_order` 只能查挂单列表，不在挂单中就返回 None，成交发现全部下放给策略层的「挂单消失 + 持仓差」推断；其 docstring 称「Parallax 参考实现里没有单订单终态查询接口」——单订单端点确实没有，但逐单匹配通道是有的，注释表述会误导后续维护者以为原仓也没有。
- 影响：同币种多张单（网格多档）或同账户有其他单子时，持仓差无法归属到具体订单，部分成交的档位识别只能靠总量推断（与自查 M3 的重复记账同属这条粗粒度通道的后果）；成交确认延迟也大于原仓的 fills 匹配。

---

## 低

### L1. 网格反向单不按实际成交量镜像：部分成交时按整格量挂反向单

- 原仓证据：WGALL `src/bot.js` `_handleFill`：`if (fillSize > 0) repl.sizeBase = fillSize`——反向单量 = 本笔**实际**成交量（注释明言为部分成交设计）。
- DeTSP 证据：`strategies/hedged_grid.py` 的 `_on_fill` 调 `counter_order(...)` 时不传成交量，`counter_order` 恒用 `qty_per_grid`（约 124-133 行）；`_reconcile_missing` 已算出 `filled = min(delta, po.qty)` 却只用于记账，不用于反向单定量。
- 影响：限价单部分成交后，反向止盈单量大于实际持仓增量：单向模式靠 reduce-only 会被场所削减或拒单，长短双向模式则会反向多开一截仓位，逐格累积后网格账面与真实持仓漂移（持仓以场所回读为准，不至失控，但补单结构变形）。

### L2. 回收阶梯落点口径与原仓不同：原仓挂在区间外（现价↔边界之间），DeTSP 只挂区间内

- 原仓证据：WGALL `_placeRecoveryLadder`：多头跌破下界时，在「现价 ↔ 下边界」之间（即**区间外**、被破边界下方一格起）逐格挂 reduce-only 卖单，价格每创新低继续向外延伸；价格只要小幅反弹、尚未回到区间内就开始分批减仓。
- DeTSP 证据：`strategies/hedged_grid.py` `_maintain_recovery`（约 457-495 行）候选层级取自 `grid_levels` 中 `lv > price` 的**区间内**层级（多头），最靠下的一档也在下边界上。
- 影响：DeTSP 的回收必须等价格先回到区间内才触发第一笔减仓，反弹幅度不及边界时仓位纹丝不动；原仓在边界外就开始回收。两者都只减仓不加仓，风险方向相同，但回收启动点明显后移——属语义口径差异，是否属有意收紧无文档记载，故列出待确认。

### L3. WGALL 的「撤单风暴暂停补单」缺失

- 原仓证据：WGALL `src/bot.js` 订单错误分类处（约 77-82 行）：60 秒内多笔订单被取消（疑似保证金不足或手动撤单）即暂停自动补单 60 秒并告警，避免反复被拒空耗手续费；另有 `_refillPausedUntil` 限流暂停机制配合。
- DeTSP 证据：中性网格的铺单/补单失败只记 `last_error` 并在后续 tick 继续尝试，无「连续被撤/被拒 → 暂停」的状态（自查 M1 的 unknown 处理是另一问题，此处指**明确被拒**后的节奏控制）。
- 影响：保证金不足等持续性拒因下，网格会每 tick 重试下单，白付请求与潜在费用、刷爆限流配额。

### L4. 网格经济性与启动价格 sanity 检查缺失

- 原仓证据：WGALL 启动段两道：一是手续费检查——格距百分比 ≤ 往返手续费率（默认费率 0.05%×2）时告警「每完成一格可能亏损」；二是价格偏离检查——最新价超出区间 [lower×0.5, upper×2] 时拒绝启动（防区间配错数量级）。
- DeTSP 证据：`HedgedGridParams.__post_init__` 只校验上下界、格数、杠杆、动作枚举，无格距/手续费对照、无启动价偏离拦截（启动后靠 tick 的区间外逻辑处理，配置数量级错误要到首个 tick 才暴露）。
- 影响：格距小于手续费的「必亏网格」可以无声启动；区间配错（如小数点错位）不能在启动瞬间被拦下。

### L5. Parallax 的费率周期自检（地板价年化反查）缺失

- 原仓证据：`parallax_hedge/funding.py` 的 `detect_period_anomaly`：统计一个所全部市场的费率众数（地板价），若其隐含年化超过 15% 则告警「多半是结算周期假设错了」——专门防「8 小时费率当 1 小时」这类整倍数错误（文件内记录了 2026-09-18 因此把 ANTH 方向判反的事故；自查 L5/H1 本轮 H1 与此同属费率防呆体系）。
- DeTSP 证据：各场所 `interval_seconds` 为硬编码常量（HL/Arcus 3600、Lighter/Aster 28800），无任何运行时自检；若某所更改结算周期或接口字段语义漂移，归一系数静默错误、选向整倍数偏差，无告警通道。
- 影响：诊断类防护缺失，与 H1 叠加构成「费率数据错了系统完全无感」的完整缺口。

---

## 与自查同源的新证据（不重复计数）

- **自查 G1（开仓后立即回读定性 vs 异步结算）**：Parallax `execution.py` 的 `LegResult.fill_confirmed` 字段与 `ledger.py` 的 120 秒匹配宽限表明，原仓明确把「刚下单的成交」置于**待证实**状态，要靠仓位变化或回执证实后才定性——从不拿下单后即时回读做开/平判定。DeTSP 轮换/carry/套利三处即时回读定性，与原仓这条纪律正好相反。
- **自查 G2（任务被停用后持仓无人管理）**：Parallax `scheduler.py` 的 `decide()` 把孤腿平仓放在优先级 1 并注明「连 enabled 都不看」，且「任务被停：有仓就平掉收尾」是显式分支——原仓专门设计了停用收尾，DeTSP 引擎 `tick()` 对 disabled 任务直接 `continue`，是移植时丢掉的一环。

---

## 有意差异（非问题）

- **套利 maker 模式的超时处理**：perp-dex-tools `hedge/hedge_mode_*.py` 的 maker 主腿被撤后会**重挂**（`place_backpack_post_only_order` 内 CANCELED → 立即重下，10 秒超时撤单后也进入重挂循环）；DeTSP premium_arb 超时**放弃本轮**进冷却、信号需重新持续才再触发。DeTSP 的做法避免在盘口已变时追单，更保守，属合理取舍。
- **网格状态恢复方式**：WGALL 用本地快照文件（`.state.json`，含挂单与盈亏）重启恢复；DeTSP 用启动对账（撤遗留单+回读持仓）恢复，不落盘策略状态。此系第一轮审计 S3 的定案设计，非丢失。
- **回收阶梯总量封顶**：DeTSP 按「剩余持仓 − 已挂回收量」封顶每档数量；WGALL 每档固定每格量、总量靠交易所 reduce-only 语义兜底。DeTSP 更严，属改进。
- **交易所侧兜底止损单**：Parallax `risk.py` 的 `protective_levels`（按强平距离比例算双腿止盈止损价）在原仓**只有定义与单元测试、无任何生产调用方**（已全仓 grep 确认），即原仓实际运行同样靠轮询监控。DeTSP 没有交易所侧兜底单与原仓实际行为一致，不算丢失（但仍是两边共同的离线裸露缺口）。
- **套利库存 surcharge 的口径近似**：entropy-arb 按**双所各自**仓位利用率（过 floor_frac 后线性 ramp，取两所最大值）上浮门槛；DeTSP `inventory_surcharge_bps` 只看 base 腿仓位相对上限。方向与目的相同，精细度略低，记为近似而非错误。

---

## 「原仓有而 DeTSP 缺失」专节

| # | 机制（原仓出处） | DeTSP 状态 | 定性 |
|---|---|---|---|
| 1 | 费率合理性闸门 SANE_RATE（Parallax funding.py） | 无（本轮 H1） | 疑似丢失 |
| 2 | 预测费率优先的统一取值口径（Parallax funding.py effective_rate） | 四所各取各的时点（本轮 H2） | 疑似丢失 |
| 3 | 费率周期自检 detect_period_anomaly（Parallax funding.py） | 无（本轮 L5） | 疑似丢失 |
| 4 | 网格启动保证金预检 + 80% 占用告警（WGALL bot.js） | 只有 caps 配置校验（本轮 M3） | 疑似丢失 |
| 5 | 铺单贴价跳档 skipBand（WGALL grid.js） | 无（本轮 M4） | 疑似丢失 |
| 6 | 手续费/格距检查、启动价偏离拒启、撤单风暴暂停（WGALL bot.js） | 无（本轮 L3/L4） | 疑似丢失 |
| 7 | delta 削腿的滑点保护限价 + 场所可用性挑选（entropy-arb engine.py `_hedge`） | 市价削腿（本轮 M2） | 疑似丢失 |
| 8 | Arcus /v1/fills 逐单成交匹配 + 宽限期（Parallax arcus.py/ledger.py） | 挂单消失+持仓差推断（本轮 M5） | 疑似丢失 |
| 9 | 深度切片定量（entropy-arb book.py crossable_base/walk_depth，按盘口深度逐片验边际） | 用单笔名义上限代替 | **已知未做**：DESIGN 已记录为遗留项，非暗丢 |
| 10 | 程序设置杠杆并回读确认（Parallax execution.py set_leverage + _confirm_arcus_leverage） | 程序不设杠杆，要求用户在交易所端预设且参数与之对齐 | **有意去掉**：第一轮审计 M6 已定案为文档口径 |
| 11 | 任务停用收尾与孤腿无条件优先（Parallax scheduler.py decide） | 引擎停用即跳过（自查 G2） | 疑似丢失（与自查同源） |
| 12 | 成交待证实纪律 fill_confirmed + 120s 宽限（Parallax execution.py/ledger.py） | 即时回读定性（自查 G1） | 疑似丢失（与自查同源） |

perp-dex-tools 的配置校验本身单薄（`exchanges/base.py` 仅校必填项），未发现可丢失的机制，不列。

---

## 对照无误清单（逐项核对通过）

- **网格核心**：等距档位（lower + i×spacing，共 count+1 档）与 WGALL `buildGrid` 一致（原仓亦为等距、非几何）；三模式 reduce-only 映射（long 的卖单/short 的买单只减仓、long_short 不带）与 `isReduceOnly` 一致；成交后相邻一格挂反向单与 `replacementFor` 一致；区间外 close = 撤单+平仓+停止，与 WGALL 一致。
- **carry 选向数学**：DeTSP `carry_edge = 空腿费率 − 多腿费率`（bps/h）与 Parallax `choose_direction` 的 `net_a = arcus_bps − lighter_bps`（多 lighter/空 arcus 方向）完全同构；「多腿恒在费率低侧」两侧一致；逐所 interval 归一到 bps/h 的换算口径一致。
- **轮换对冲风控组**：开仓前走廊估算门（DeTSP `estimate_open_corridor` + `min_corridor_pct`）、开仓后用真实强平价复核（`_post_open` 对照 Parallax `verify_corridor_after_open`）、持仓期相对触发（现走廊 < 开仓走廊约定比例时双平，对照 `evaluate` 的 monitor_close_fraction）、绝对平仓线、平仓冷却（DeTSP 60 秒，原仓 30 秒，同机制）、持有时长开仓时抽一次落定（`draw_hold_seconds` 在开仓调用，对照 `draw_hold_hours` 的「只抽一次」纪律，均未犯每 tick 重抽的错）、孤腿无条件平、双腿不平时削多余腿。
- **套利信号**：DeTSP 用可成交边际（base_bid/hedge_ask、hedge_bid/base_ask 交叉价）扣双边费后比带宽，与 entropy-arb 的 executable premium 口径一致（严于其状态展示用的 mid-to-mid premium）；persist 按方向计时、冷却、库存 surcharge 均在；基准腿恒为 base（对应原仓恒 Entropy 腿）。
- **套利录制器**：分钟级字段（premium OHLC/mean、双向 edge 的 mean/max、samples）与 entropy-arb recorder 逐列一致（分析口径问题见本轮 M1，在 analyze 不在 recorder）。
- **Arcus 适配**：签名载荷（place op=1 / cancel op=2、键序、g 纳秒换算、clientId 小写、orderId 与 clientId 二选一）、tickTiers 分段选档、价格按方向取整（买 ceil/卖 floor）、数量向下对齐、good_til 40 天——`venues/_arcus_sign.py` 与 Parallax `arcus.py` 逐项一致（本就是移植）。
- **HL 家族**：dex 参数化（""/io/xyz）、币名前缀（io:BTC）、市价走 IOC 语义（SDK market_open）、限价 tif 映射（post_only→Alo、否则 Gtc）、价格 5 位有效数字规则与 entropy-arb venue_hl.py 及官方口径一致；Exchange 构造传 `perp_dexs=["", "io", "xyz"]` 使 SDK 预加载三所资产表，子所 asset 解析不缺位；杠杆两边均不由程序设置（原仓 ea 亦然）。
- **Lighter**：chain/市场解析/资金费 interval 与第六批对 perp-dex-tools 的对照结论一致，本轮对 Parallax/entropy-arb 两份实现复核无新增出入（资金费**符号**仍属自查 L5 的未实证项，本轮无新证据）。
- **maker 挂价**：DeTSP `maker_open_price` 贴盘口内侧、不穿价（买封顶 ask−tick），与 perp-dex-tools BBO post-only 挂法口径一致；成交后对冲腿 taker 跟上、部分成交按最终量对冲，与原仓主循环按持仓差跟平的语义一致。

---

*本报告只审查未修复。分级以 live 真金模式下的资金安全与定参正确性为标尺；H1/H2 在 paper 下均不可见（paper 费率由测试同口径注入）。与自查合并后的总账：自查 13 条 + 本轮新增 12 条 + 同源加强 2 条，待用户定修复范围。*
