# DeTSP 独立审查报告（策略层重写后）

审查日期：2026-11-03　审查方式：独立代码审阅（只审不改）+ 实跑测试
范围：`src/detsp/` 全部源码、tests/、config.example.yaml、README / DESIGN / 使用说明 / NOTICE / AUDIT.md
背景：策略层刚推倒重写，现四策略为 hedged_grid（中性网格）、premium_arb（溢价套利，含 maker/taker 开关）、rotation_hedge（轮换对冲）、funding_carry（资金费 carry）；旧 grid / volume_loop 已删。

## 结论先行

- **测试现状**：本次实跑 `pytest` 共 **191 个全部通过**，与宣称一致，无跳过。
- **逻辑成立性**：四个策略的语义闭环在 paper（即时结算）口径下基本成立——对冲目标符号、carry 选向数学、maker 部分成交的增量记账、平仓恒 taker、轮换逐步建仓不重置计时等关键口径均经代码与测试核对无误（见文末「验证无误」清单）。
- **主要风险不在策略纯逻辑，而在 live 结算语义与安全闭环**：共发现问题 **13 条（高 2、中 6、低 5）**。两条高优先级都是同一类根源——多处代码按「下单后立即回读持仓即为真相」行事，但 Lighter 市价单是异步结算、回执 `filled_qty=0`；以及任务被引擎停用后其持仓脱离一切自动管理。这两条在 paper 下都测不出来，是实盘前必须先处理的部分。
- **无关残留**：引擎骨架模式整套死代码仍在（上一轮审计 L2 已记录、重写后未清）；AUDIT.md 全文针对已删除的旧代码，README 仍把它当现行审计结论引用，易误导。详见「无关残留」专节。

---

## 高

### G1. 开仓后「立即回读持仓」定性，与 Lighter 市价单异步结算冲突（轮换 / carry / 套利三处）

- 证据：
  - `venues/lighter.py:363-364`：live 市价单下单成功也只返回 `Order(status="open", filled_qty=0)`，成交要等异步结算后才反映在 `positions()`；DESIGN.md 第 12 节自述「Lighter 市价单异步结算，成交以持仓对账为准」「市价单成交确认走对账，不做同步等待」。
  - `strategies/rotation_hedge.py` `_open_round`：双腿下单后立刻 `_legs_qty()` 回读，据此判断「只成一腿 → `_close_both` 双平」或「两腿都没成 → 留在 idle」。Lighter 腿尚未结算时回读为 0，会被误判。
  - `strategies/funding_carry.py` `_open_pair`：同一模式，立即回读定性。
  - `strategies/premium_arb.py` `_fire_taker` 与 `_hedge_after_maker`：双腿发出后**立即**调用 `_fix_delta()` 按回读持仓削腿——对冲腿（常见为 Lighter）未结算可见时，刚开好的基准腿会被当成「多出的腿」以 reduce-only 全量削掉，随后对冲腿结算落地又成裸腿，等 15 秒后下一次定期对账再平掉。整轮变成开平空转 + 双倍手续费。
  - 连带卡死态：轮换对冲若开仓回读时双腿都未结算可见 → 判「两腿都没成」留在 idle；下一 tick 持仓已落地、`has_position=True`，而 tick 只在 `not has_position` 时才调 `_open_round`，phase 永远停在 idle——仓位被安全检查看着（强平距离在 `_safety` 内），但**永不轮换、opened_at 永不设置**。
- 影响：仅 live 且腿含 Lighter（或任何异步结算场所）时触发；paper 即时结算无法复现，现有测试全盲。后果从「套利单轮被误削」到「轮换仓位僵死」不等。
- 建议方向（不在本次范围）：开仓定性改为以回执 + 延时复核为准（如结算宽限期内不做 delta 削腿、idle 且有仓时按 open 接管并补设 opened_at）。

### G2. 任务连续错误被停用后，其持仓脱离一切自动管理

- 证据：`core/engine.py` `tick()`：`if task.disabled: continue` 直接跳过；错误达阈值时先置 `task.disabled = True` 再 `kill_switch.halt(...)`。其余任务靠后续 tick 以 `allow_open=False` 自行平仓，但被停用任务**再也不会收到 tick**，其 `stop()` 也不会被引擎调用——它的持仓（哪怕是强平距离正在恶化的那一腿）从此无人监控、无人平仓，只能等用户在面板手动点停止（`web/server.py` `_do_stop_task` 对 disabled 任务仍可调 `runtime.stop()`，但需人工发现）。
- 影响：触发条件正是「该任务正在连续出错」的危险时刻；四个策略都是双腿结构，裸露的其实是整组仓位的强平不对称风险。安全链条在最需要它的一环断开。
- 建议方向：停用任务时由引擎顺带调用其 `runtime.stop()` 收尾，或停用后仍以 `allow_open=False` 继续 tick 该任务若干轮直至仓位清空。

---

## 中

### M1. maker / 限价挂单路径不处理 Executor 的 `unknown` 结果，未跟踪的挂单可能已在交易所生效

- 证据：`core/execution.py` `place()` 对结果未知返回 `ExecResult(status="unknown", order=None, position=对账持仓)`，设计口径是「可能已生效，绝不重发」。但三个策略的挂单路径都不看这个状态：
  - `premium_arb.py` `_place_maker_open`：`res.order is None` 一律记 `last_error` 返回——unknown 与 rejected 同等对待，实际可能已挂出的 post-only 单不被跟踪，其后续成交不会被对冲（靠 `_fix_delta` 定期削腿兜底，变成误开误平）。
  - `rotation_hedge.py` `_place_maker`：只处理 `open/partial/filled`，unknown 无分支，phase 留在 idle；下一 tick 若仍无持仓会再次 `_open_round` 重挂一张。
  - `hedged_grid.py` `_place`：同样把 unknown 当普通失败，该层级留空，挂单若实际生效则不在 `_orders` 跟踪内（成交不补反向单）。
- 影响：live 网络抖动时出现未跟踪挂单/重复挂单；三策略程度不同（套利有 delta 修正兜底、轮换有安全检查削腿兜底、网格只能靠重启时 `cancel_unregistered` 清理）。

### M2. Lighter 市价单回执 `filled_qty=0` 导致账本漏记成交，且订单登记永不注销

- 证据：`venues/lighter.py:363-364` 回执恒 `filled_qty=0, status="open"`；`core/execution.py` `place()` 只在 `filled > 0` 时写账本。于是轮换 / carry / 套利 / 中性网格对冲腿只要走 Lighter 市价单，该腿成交**永远不进账本**（策略侧只有 maker 路径会 `record_fill` 补记，taker 路径不补）。面板「今日成交量/笔数」对含 Lighter 腿的任务系统性失真（DESIGN 第 12 节的 Lighter 说明未提此口径）。
- 连带：这类订单在 `registry` 里以 `open` 登记，而场所 `open_orders` 里查不到（IOC 已终结），`cancel_all_own` 遍历的是场所挂单、不会清它，登记条目在进程内只增不减（`release` 时才按 key 批量清）。
- 影响：不影响持仓安全（持仓以场所回读为准），影响账本/统计真实性与长跑内存。

### M3. hedged_grid 限价单「下单时部分成交」会在 poll 终态路径重复记账

- 证据：`core/execution.py` `place()` 在回执 `filled_qty>0` 时已按该量记账；`hedged_grid.py` `_process_fills` 在 poll 返回终态时按 `o.filled_qty`（累计口径）**全量** `record_fill`，没有扣减下单时已记的量；`_reconcile_missing` 的对账式发现同样按基线差全量记。轮换与套利的 maker 路径都有 `recorded` 增量扣减（`rotation_hedge.py` 的 `_maker_recorded`、`premium_arb.py` 的 `mk.recorded`），唯独中性网格的网格腿没有。
- 影响：live 限价单在下单瞬间部分成交（HL 等场所常见）时，账本成交量被重复计入；paper 挂单下单时恒 filled=0，测不出来。持仓本身以场所回读为准，不受影响。

### M4. PremiumParams 缺场所与关键参数校验，与其余三策略不一致

- 证据：`premium_arb.py` `PremiumParams.__post_init__` 只校验带宽/比例/执行模式/maker 参数，**没有**校验 `base_venue != hedge_venue`、venue 非空、`persist_seconds >= 0`、杠杆为正；而 `rotation_hedge.py`、`funding_carry.py`、`hedged_grid.py` 的参数类都校验了两腿不同所。config 层（`core/config.py`）也不验 task 的场所取值（只验顶层 `venues` 段的 venue_id 合法性，task 内的 venue 字段不在校验范围）。
- 影响：配成同所自对冲时，`_fix_delta` 的 legs dict 以 venue_id 为键会塌成一条腿、delta 恒等错误口径，策略在自相矛盾的状态机里运行；配空 venue 时在组装期以 `KeyError: ''` 这类晦涩错误被隔离，排障成本高。

### M5. caps 的「策略层」口径在四策略间不统一，双腿策略实际可达 2 倍

- 证据：`core/execution.py` `place()` 的策略上限按**单个 (venue,symbol) 持仓名义**校验；`hedged_grid.py` `start()` 却按**双腿总名义**（单腿估算 ×2）硬校验。rotation / carry / premium 没有启动总名义校验，只受 Executor 的逐腿校验约束。
- 影响：同一个 `caps.strategies[任务名] = X`，对中性网格是双腿合计口径，对其余三个双腿策略是单腿口径——后者实际总敞口可达 2X。DESIGN 第 10 节与 `config.example.yaml` 只写「策略上限」，未注明此差异，用户按任务总额理解会低估敞口一倍。

### M6. 归属登记只在进程内有效，启动对账会把同账户同币种的外部/手动仓位与挂单按自有处理

- 证据：`core/registry.py` 的归属是纯内存登记，无法区分「本进程的仓」与「用户手动/其他程序在同一账户同一币种的仓」。而启动对账是按仓位直接动手的：
  - `funding_carry.py` `start()`：孤腿/同向直接平掉、不等量削腿（已定案口径，面板事件有记录）；
  - `rotation_hedge.py` `start()`：任何已有持仓按 open 接管，到期会被轮换平仓；
  - `hedged_grid.py` `start()`：`cancel_unregistered` 撤掉该 (venue,symbol) 下**全部**未登记挂单——重启后登记表为空，等于撤掉该币种所有存量挂单（含手动单）。
- 影响：DESIGN 第 5 节「孤腿抢救只认自有腿，不会误平」的表述只对进程内多任务成立，对外部仓位不成立；目前靠使用说明第 7 节「确认没有其他程序在跑同一账户同一币种」的操作约定兜底。属已定案设计与文档表述的落差，实盘共用账户时需特别注意。

---

## 低

### L1. 中性网格：死区与最小下单量叠加时，小残差可无限期裸露

- 证据：`hedged_grid.py` `plan_hedge_adjustment` 在残差达不到对冲所 `min_qty` 时返回 0（等累积），绕过最小量的强制对冲要 `max_unhedged_seconds > 0` 才启用，而参数默认与示例都是 0（禁用）。网格腿 `qty_per_grid` 小于对冲所最小下单量时，每格成交产生的残差永远凑不齐最小量（除非多格同向累积），组合长期带 ≤ min_qty 名义的方向裸露。
- 影响：金额被 min_qty 封顶，风险有界，但「不留单边裸仓」的设计宣称（DESIGN 第 3 节）在默认参数下并不严格成立。

### L2. premium_arb：maker 开仓单待成交期间，强平监控与定期对账被整 tick 跳过

- 证据：`premium_arb.py` `tick()`：`if self._maker is not None: self._tick_maker(now); return`——位于强平距离监控与 `_fix_delta` 之前。maker 单最长挂 `maker_timeout_seconds`（默认 30 秒），期间已有持仓的强平距离跌破门槛也不会触发双平（停机信号在此之前处理，不受影响）。
- 影响：最多延迟一个 maker 超时窗口的安全动作；窗口可被用户调大（如 300 秒）而放大。

### L3. KillSwitch 在 tick 中途被触发时，开仓异常会误伤任务错误计数

- 证据：面板停机走 HTTP 线程直接 `kill_switch.halt()`（`web/server.py` 的 `/api/killswitch`，不经命令队列）；`core/execution.py` `place(intent="open")` 内的 `kill_switch.assert_allowed()` 抛 `RiskHaltError`，不在 `place` 的异常兜底范围内，会穿透策略 tick 被 `core/engine.py` 计为任务错误。恰逢任务已有连续错误时，可能成为压垮它的最后一次「错误」并触发 G2 的停用路径。
- 影响：概率低、后果与 G2 叠加；本质是停机竞态下的误分类。

### L4. hedged_grid `stop()` 的归属释放条件依赖场所不再返回零量持仓行

- 证据：`hedged_grid.py` `stop()` 平仓后以 `executor.position(...) is None` 判断是否 `release`；`core/execution.py` `position()` 返回该 symbol 的第一条持仓记录，不判 `qty==0`。若某 live 场所返回 qty=0 的持仓行，claim 不释放——同任务名重启不受影响（owner 相同可重 claim），但改任务名或换策略接管同一 (venue,symbol) 会被 ConflictError 挡住。
- 影响：未实证（取决于各所持仓接口是否返回零量行），属边界隐患。

### L5. 资金费符号约定未经实证：某所原生费率符号若相反，carry 与轮换选向会系统性反向（未核实项）

- 证据：全系统约定「费率为正 = 多头付费」（`core/venue.py` `FundingInfo` 注释），carry 的净收公式与 rotation 的选向都建立在此约定上。各 venue 的 `funding()` 直接透传原生值：HL `ctx.funding`、Aster `lastFundingRate`、Arcus `nextFundingRate` 均为「多头付费为正」的通行约定；Lighter `/api/v1/funding-rates` 的 `rate`（`venues/lighter.py:244-255`）符号约定未经真实接口核对，代码与文档均无说明。paper 的 `set_funding` 按系统约定注入，任何符号错误都测不出来。
- 影响：标为未核实——若 Lighter 符号相反，含 Lighter 腿的 carry 会长期持有净付方向、轮换对冲每轮选反方向。实盘小额验证时应首先核对这一项。

---

## 无关残留专节

### R1. 引擎骨架模式整套死代码（上一轮审计 L2 已记录，重写后仍在）

- `core/engine.py` 的 `run_once` / `add_strategy` / `strategies` 字段、`Context` / `Action` / `Strategy` Protocol：全仓 grep 无任何生产或测试调用方（`__main__` 与面板只用 `from_config` + `tick`）。
- 四个策略文件末尾的 `decide()` 空实现（各策略均有，恒返回 `[]`）：只为已无人用的骨架 Protocol 而存在。
- 影响：读者会误以为系统有两套调度路径；DESIGN 第 8 节描述的正是这套已不用的流程（见 R4）。

### R2. `registry.claim_api_key` 无生产调用方，且 DESIGN 第 5 节的对应承诺未接线

- 证据：`core/registry.py:84` 的 `claim_api_key` 仅被 `tests/test_registry.py` 调用；`venues/factory.py` 每所只建一个适配器实例（一组账户/一个 api_key_index），策略层从不 claim key index。DESIGN 第 5 节「同一 Lighter 账户下各策略必须用不同 api_key_index（nonce 隔离），登记表记录占用」在组装出的系统里并不存在——多任务共用同一 Lighter 适配器与同一签名器，nonce 由适配器内部计数器串行分配（`lighter.py` 的 `_next_coi`），实际不会冲突，但文档描述的机制与代码不符。

### R3. AUDIT.md 全文已过时，README 的引用方式会误导新读者

- 证据：AUDIT.md 审计对象是旧代码（总评写 127 个测试、结论「小额实盘能不能跑：现在不能」，问题证据指向已删除的 `strategies/grid.py`、`volume_loop.py`）。文末有逐项修复记录，但正文结论未做任何「已被后续批次取代」的标注。README「当前状态」仍写「已通过一轮独立代码审计，严重与高优先级问题已修复（见 AUDIT.md）」——读者点进去先看到的是针对已删代码的否定结论与过时数字（现为 191 个测试），无法判断哪些还适用。
- 建议：在 AUDIT.md 头部加一行「本报告针对 2026-11-03 策略重写前的代码，结论已被后续修复与本轮 REVIEW 取代」或将其更名为历史归档。

### R4. DESIGN 若干章节与代码不符（骨架时代遗留表述）

- 第 7 节（账本）：写「四策略盈亏、磨损（每万美元）、交易量统一从账本聚合；与交易所持仓定期对账（reconcile）」——`core/ledger.py` 只有成交量/手续费/最近成交/今日统计，没有盈亏与磨损聚合，账本层也没有 reconcile（对账在各策略与 Executor 内）；且手续费恒记 0（AUDIT 的 M1 已知未修，DESIGN 第 12 节有记录，但第 7 节表述为既成口径）。
- 第 8 节（引擎）：「快照 → 风控裁决 → 策略决策 → 归属校验 → 执行 → 记账」是骨架模式流程；执行模式实际是引擎逐任务调 `runtime.tick()`，策略内部自行取行情/校验/下单。
- 第 5 节：关于「只认自有腿」的表述问题见正文 M6。
- 第 12 节状态表：内容与现状基本一致，但通篇「第一批/第三批/第五批」批次叙述对使用者无意义，且 strategies 行称「191 个测试全绿。尚未做实盘小额验证」是唯一与现状完全对应的部分。

### R5. 不算残留的两处（避免误报，特此说明）

- `core/config.py` 的 `_TASK_REMOVED` 与 `core/engine.py` 对 `grid` / `volume_loop` 的拦截报错：是有意设计（旧配置给出明确迁移提示），且有专门测试（`tests/test_integration_strategies.py:258` 起），不是残留。
- `tests/test_registry.py`、`tests/test_execution.py` 中把字符串 `"grid"` 当任意策略标签使用：只是测试夹具的标签名，与旧策略无关。
- NOTICE 中 perp-dex-tools 条目提及 volume_loop：已改写为历史溯源说明（「曾按其语义实现、重写时已移除」），与代码现状一致，来源对应准确；其余来源条目（premium maker ← perp-dex-tools、carry/rotation ← Parallax、价差带 ← entropy-arb、网格 ← WGALL）与四策略文件头注释、DESIGN 第 3 节三方一致。
- `venues/_skeleton.py` 名字像骨架残留，实际是全部 live 适配的在用基类（paper 委托层），非死代码；`tools/`（recorder/analyze）仍是 premium_arb 定参流程的必需工具，非无关文件。

### R6. 配置示例与代码校验的细小不一致

- `config.example.yaml` 本身与四策略参数类逐项对得上（字段名、默认值口径一致）；但示例里 `rotation_hedge` 只列了 `max_position_qty` 与 `accumulate_interval_seconds`，未列 `arcus_maker`、`hold_min/max_seconds`、`notional_min/max_usd`、`maker_wait_seconds` 等可选参数——不是错误，只是示例不全，用户无从知道这些开关存在（DESIGN 第 3.2 节提到了 maker 模式）。

---

## 验证无误的关键项（抽查通过）

- **carry 方向数学**：`funding_carry.py` 的 `carry_edge_bps_per_hour`（空腿费率 − 多腿费率）、`best_carry_direction`（多腿恒在费率低侧）、`current_edge_bps_per_hour`（按已持方向计，费率反转为负）与 `core/risk.py` 的 `funding_hourly_rate` 逐所 interval 归一口径一致；`tests/test_funding_carry.py` 含跨结算周期（3600 vs 28800）换算断言。入场门槛 + 退出迟滞 + 连续确认次数的状态机无卡死态：平仓的每条路径（迟滞退出/最长持有/孤腿/强平/停机）都配 `_enter_cooldown` 或置 flat，`reopen=False` 用 `_opened_once` 正确短路。
- **中性网格对冲口径**：`hedge_target = −grid_qty`、`hedge_residual = 两腿之和`、`plan_hedge_adjustment` 返回 `−residual`，符号正确；对冲调平每 tick 以双腿**真实持仓**回读计算，网格腿平仓单成交后下一 tick 自动同步减对冲，不依赖成交事件记账，无方向漂移。对冲腿被纳入 tick 内双腿强平监控（任一腿跌破门槛双平）。caps 启动硬校验按双腿总名义，严于运行时逐腿校验（方向保守）。
- **premium_arb maker 时序**：成交量只从 poll 终态或「持仓 − 基线」对账取**最终量**，等待期不提前对冲；`_finalize_maker` 用 `recorded` 扣减增量记账，不重复；部分成交只对冲最终量一次，无重复对冲路径。`tests/test_premium_maker.py` 四个用例（全成/超时无成/部分成超时按最终量/平仓走 taker）断言均为具体数量，非弱断言。
- **平仓恒 taker 在所有退出路径成立**：premium 的信号减仓由 `_fire` 的 `closing` 判定强制走 `_fire_taker`；强平路径用 `Executor.close_position`（市价）；停机/面板 stop 走 `_flatten`（撤单 + 市价全平）。无 maker 平仓路径。
- **轮换对冲重写保真**：资金费选向先归一到每小时（H3 口径保留）、maker 部分成交保留跟踪终局按最终量（M2）、启动按 open 接管（S3）、逐步建仓 `accumulation_step_qty` 截断与 `_post_open(reset_timer=False)` 不重置持有计时——与 Parallax 语义及旧测试口径一致，旧用例（`test_audit_fixes.py`、`test_rotation_accumulate.py`、`test_integration_strategies.py`）在新实现上全过。
- **跨策略闭环（进程内）**：config 层对四策略全部腿做 (venue,symbol) 占用查重；Executor 每次 place/cancel 都过 registry 归属；KillSwitch 落盘恢复与预检指纹门（`__main__.py`）与使用说明第 7/8 节描述一致；四策略 `stop_note` 与停止行为（撤自有单 + 平全部持仓）与使用说明第 5 节一致。
- **新策略测试覆盖面**：funding_carry 14 个用例覆盖纯函数、入场/迟滞退出/最长持有/冷却重开/reopen 关/停机/孤腿/三种启动对账；hedged_grid 10 个用例覆盖纯函数、caps 拒绝、任务间 claim 冲突、未托管与接管、死区、裸露超时强制对冲。未发现 `assert True` 式空断言或 skip/xfail。
- **测试盲区（与正文问题对应，不另列条目）**：所有策略级测试都跑在即时结算的 paper/假场所上——G1 的异步结算时序、M1 的 unknown 挂单路径、M3 的下单瞬间部分成交、L5 的真实费率符号，均无任何测试覆盖；这是 live 与 paper 的结构性差距，不是测试写得不用心。

---

*本报告只审查未修复。问题分级以「live 真金模式下的资金安全与状态机正确性」为标尺；paper 模式下 13 条中仅 M4（参数校验）与 G2 的监控缺失（paper 同样存在）可复现，其余均为 live 路径问题。*
