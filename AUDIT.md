# DeTSP 独立审计报告

> **⚠ 本文件已过时（2026-11-04 注记）**：本报告针对的是策略层推倒重写**之前**的旧代码，其问题清单与结论已被后续两轮审查取代。当前权威的问题总账与逐条修复记录见 [审查汇总.md](审查汇总.md)（含 REVIEW.md 自查、REVIEW2.md 对照复核与三批修复销账）。本文件仅作历史留档，不要按它评估当前代码状态。

审计日期：2026-11-03　审计方式：独立代码审阅（未参与开发）+ 实跑测试验证
范围：`src/detsp/` 全部源码（约 5,400 行）、tests/（约 1,560 行）、启动脚本、配置模板、DESIGN.md / README / 使用说明 / NOTICE / LICENSE

## 总评

- **测试独立验证**：`pytest` 实跑 **127 个全部通过**（与开发方宣称一致），无跳过、无联网用例。
- **paper 能不能跑**：**能跑，但只能验证流程、不能验证策略表现**。paper 行情是写死的静态价（见高 H6），网格永不成交、套利永无信号，跑通的是状态机骨架，不是市场行为。
- **小额实盘能不能跑**：**现在不能**。存在 3 个严重问题，其中「结果未知保护失效」（S1）和「网格实盘无法感知成交」（S2）是功能性硬伤：网格 live 开了也不会按设计工作，轮换/套利在网络超时下有重复开仓风险。按文末修复顺序修完 S1–S3、H1 后，轮换对冲（双 taker 模式）与溢价套利可先行小额验证；网格须等 S2 修好；Aster 须等 H5 修好。

问题计数：严重 3、高 7、中 8、低 5。

---

## 严重

### S1. 「结果未知不重发」保护对真实网络异常失效（核心安全不变量名存实亡）
- 证据：`core/execution.py:153` 只捕获 `(UnknownOutcomeError, TimeoutError, ConnectionError)`（后两者是 Python 内置类）；但全仓**没有任何 venue 抛出过 UnknownOutcomeError**（grep 零命中）。各 venue 实际抛的是第三方库异常，且经实测验证：`requests.exceptions.ConnectionError/ReadTimeout`（HL 的 SDK 底层）**不是**内置 `ConnectionError/TimeoutError` 的子类，`httpx.ReadTimeout/ConnectError`（Arcus/Aster/Lighter 读接口）同样不是。
- 后果：下单请求超时（订单可能已在交易所成交）时，异常穿透 Executor → 策略 tick 抛错 → 引擎只计错误数；下一 tick 轮换对冲（`rotation_hedge.py` 的 `_open_round`，phase 仍为 idle）与溢价套利会**重新下单**，形成重复仓位/裸腿。这正是原设计明文要防的第一风险。
- 修复建议：各 venue 的 `place_order` 统一把超时/断连类异常翻译为 `UnknownOutcomeError` 再抛；或 Executor 改为捕获所有异常后先对账、无法确认才返回 unknown（同时保留对明确拒单的区分）。

### S2. 网格（及轮换 maker 模式）实盘无法感知成交，策略 live 不工作
- 证据：`core/venue.py:123` 的默认 `poll_order` 只查当前挂单列表；HL / Lighter / Arcus / Aster 四个 live 适配**均未覆盖** `poll_order`。限价单成交后从挂单列表消失 → `poll_order` 返回 None → `strategies/grid.py:187` 的 `_process_fills` 对 None 直接 `continue`，该订单永远留在策略的 `_orders` 里：不补反向单、不记账、层级被永久占用。
- 后果：网格 live 铺完初始单后即僵死；轮换对冲的 maker 模式同理（`rotation_hedge.py` 的 `_tick_opening` 等不到成交，超时撤单后把已成交的 maker 腿当孤腿平掉，变成开平空转）。开发方在第三批汇报中提过此限制，但 DESIGN 第 12 节仍把网格标为「执行状态机已完成」、venue 标为「live 已完成」，与实际可用程度不符。
- 修复建议：各 venue 实现真正的订单终态查询（HL 用 orderStatus、Arcus 用订单查询、Aster 用 GET order、Lighter 用成交/账户推送或历史订单）；或网格改为「挂单消失 + 持仓变化」的对账式成交发现，并补 live 路径的集成测试（mock 终态）。

### S3. 进程重启后孤儿挂单/持仓无对账恢复，网格会重复铺单
- 证据：`core/registry.py` 的归属登记纯内存、无持久化；`core/engine.py` 的 `from_config`/`build_task` 启动任务时不读交易所现有挂单/持仓做恢复对账；`strategies/grid.py:130` 的 `start()` 拿到 claim 后直接按计划重新铺单。`Executor.cancel_all_own` 只认 registry 登记过的订单，上一进程留下的挂单不在登记内，撤不掉也看不见。
- 后果：实盘网格只要重启一次（崩溃、断电、手动重启都算），交易所里就是两套网格单叠加，旧单无人管理地继续成交。与 `使用说明.md` 第 114 行「重启程序后，任务会重新 claim 并对账」的说法**不符**（轮换/套利每 tick 读真实持仓，勉强算对账；网格没有）。
- 修复建议：启动时做恢复对账——按 (venue, account, symbol) 拉取交易所现有挂单/持仓，与任务配置比对：要么接管（登记归属并纳入策略跟踪），要么拒绝启动并报警；使用说明同步改掉不实表述。

---

## 高

### H1. 三层仓位上限在真实入口完全未接线，网格无任何名义金额保护
- 证据：`core/engine.py:146` `from_config(..., caps=None)`，`__main__.py` 调用时不传 caps，`config.example.yaml` 也没有 caps 配置项 → Executor 的 `Caps` 全为 0 = 不限（`core/execution.py` 的 `Caps`/`within_cap`）。`grid.py` 的 `estimated_margin()` 算了但从未被执行层调用校验。
- 后果：网格任务的格数×每格量没有任何系统级拦截，配错数量级直接满额下单。
- 修复建议：config 增加 caps 配置（全局/场所/策略），`__main__` 传入；网格 start 前用 `estimated_notional` 做一次硬校验。

### H2. 面板 POST 接口无任何请求校验，本机浏览器恶意页面可操控交易
- 证据：`web/server.py:191` 的 `do_POST` 对 `/api/killswitch`、`/api/killswitch/reset`、`/api/tasks/{id}/start|stop` 不校验 token、Origin、Host 或 Content-Type。绑定回环只能挡局域网，挡不住：① DNS rebinding（不校验 Host 头，恶意域名解析到 127.0.0.1 后可读写全部 API）；② 跨站简单请求（HTML 表单/fetch no-cors 可向 127.0.0.1 发 POST，动作会执行）。
- 后果：用户开着面板浏览恶意网页时，可被远程触发：停网格任务（`stop_task` 会市价平仓、实现亏损）、重置全局停机、重新启动实盘任务。
- 修复建议：启动时生成随机 token，页面与 API 必须带 token；同时校验 Host 必须为回环字面值、POST 校验 Origin。

### H3. 资金费口径不一致，轮换对冲选向可系统性错误
- 证据：`core/venue.py:86` 的 `FundingInfo` 只有 `rate`（注释「当期费率」），没有结算周期字段。实际口径：HL 是**每小时**费率（`venues/hl.py` funding 注释）、Arcus 每小时（`venues/arcus.py:236`）、Lighter 是**每 8 小时**费率（`venues/lighter.py:46` `FUNDING_INTERVAL_SEC = 8*3600`，rate 原样返回）、Aster 返回的是**上一期已结算**费率（`venues/aster.py:174`，Binance 风格 8 小时）。`strategies/rotation_hedge.py` 的 `choose_direction(fa, fb)` 直接比较原始 rate 选向。
- 后果：Arcus（1h）对 Lighter（8h）的组合，选向判断可差出数倍量级，长期按错误方向收/付资金费；`net_funding_edge` 同样是混合口径。
- 修复建议：`FundingInfo` 增加 `interval_seconds`，各 venue 如实填写；选向前统一换算为每小时（或每 8 小时）口径。

### H4. 同策略类型的多个任务之间 claim 不互斥
- 证据：策略身份是写死的类属性——`grid.py:113` `name = "grid"`、`rotation_hedge.py:103`、`premium_arb.py:135`，Executor 的 claim/place/cancel 全部用这个名字作 owner。`registry.py` 的 `claim` 在 owner 相同则放行。任务名（config 的 `name`）从不参与归属。
- 后果：config 里写两个 grid 任务指向同一 (venue, symbol)，系统不会拦截，两个状态机互相把对方的单当「自有单」撤掉/补格。宣称的「同账户同币种互斥」只对**不同策略类型**成立。
- 修复建议：策略实例的归属身份用任务名（task name）而非策略类型；或在 config 校验层直接拒绝同 (strategy 无关) (venue,symbol) 的重复任务。

### H5. Aster 下单不做价格/数量精度对齐，实盘单大概率被拒
- 证据：`venues/aster.py` 的 `place_order`（约 199–216 行）直接把 `req.qty`/`req.price` 经 `_fmt_qty`（仅去掉浮点尾巴、保留 10 位小数）发出；`markets()` 解析出的 `price_step/qty_step` 从未被下单路径使用。策略算出的数量（如 名义/价格 的除法结果）几乎不可能恰好落在 stepSize 网格上，Binance 风格接口会以 LOT_SIZE / PRICE_FILTER 拒单。
- 修复建议：place_order 内按 markets 的 step 向下对齐数量、按 tick 对齐价格（方向按买卖取舍），并补对齐单测。

### H6. paper 模式在出厂入口下是静态死水，「先用 paper 跑通」不成立
- 证据：`venues/paper.py:39` 价格写死 `{"BTC": 60000, "ETH": 3000}`，`venues/factory.py` 组装时从不注入价格，也没有任何价格源/随机游走驱动 `set_price`；`book()` 对未注入的 symbol 直接 KeyError——`config.example.yaml` 里的示例套利任务用的就是 SNDK，paper 下一启动就报错被隔离。
- 后果：用 `python -m detsp` 真跑 paper 时：网格挂单永远等不到穿价成交、双静态价的套利永远溢价为 0、轮换对冲按假价开平。集成测试里靠手动 `set_price` 驱动的部分掩盖了这一点。
- 修复建议：paper 模式默认接公开行情只读价（或内置随机游走生成器），markets 覆盖示例用到的 symbol；或明确把 paper 定位降级为「测试夹具」并修正文档措辞。

### H7. 溢价套利没有任何强平距离监控，Executor 的强平门槛只对轮换对冲生效
- 证据：`core/execution.py` 的 corridor 检查只在调用方传入 `corridor_pct` 时执行；`strategies/premium_arb.py:196` 的 `_fire` 两腿都不传；套利 `tick` 里也没有持仓强平距离检查（只有 delta 修正）。网格同样从不传（`grid.py` 的 `_place`）。只有轮换对冲传了（且估算粗糙，见 M5）。
- 后果：Entropy 逐仓腿的实际杠杆由账户侧决定、程序不可见，套利仓位涨到 `max_position_usd` 的过程中无人看强平价；DESIGN 宣称的「全局风控：强平距离」实际只覆盖一个策略的一种路径。
- 修复建议：Executor 对 intent=open 强制要求 corridor_pct（不传则用持仓/账户数据自算）；套利 tick 增加与轮换对冲同款的强平监控段。

---

## 中

### M1. 账本手续费恒为 0、HL 成交价丢弃真实 avgPx，盈亏/磨损统计系统性失真
- `core/execution.py:105,172` 两处记账都硬编码 `fee=0.0`；`venues/hl.py:268` 解析成交时只取 `totalSz`，回执里的 `avgPx` 从未使用，市价单入账价由 Executor 回退到下单瞬间的盘口价近似。`core/ledger.py` 也没有任何已实现盈亏聚合函数，与 DESIGN/README「盈亏/磨损统一从账本聚合」的说法不符（面板显示的「手续费」恒为 0）。
- 修复建议：venue 层回传真实成交价与手续费（可得时），Ledger 增加按策略的已实现盈亏聚合，文档措辞与实现对齐。

### M2. 轮换对冲 maker 部分成交处理错误
- `strategies/rotation_hedge.py` 的 `_tick_opening`：只要 `filled_qty > 0` 就按该量对冲并清空 `_maker_oid`——若只是部分成交，剩余挂单仍留在交易所且无人跟踪，后续成交形成未对冲腿，只能靠安全检查事后削仓空转。
- 修复建议：部分成交时保留对订单的跟踪（继续等剩余或主动撤单后再对冲），对冲量以最终确认量为准。

### M3. KillSwitch 与任务状态纯内存，重启即「复活」实盘
- `core/risk.py` 的 KillSwitch 是内存布尔；`core/engine.py` 的 `build_task` 在组装时就调用 `runtime.start()`（网格会立即铺单）。因连续错误触发的停机，重启进程后无痕消失、任务自动重新开始下单。且整个系统没有「切 live 必须先过预检」的强制门（预检是独立命令，可跳过）。
- 修复建议：停机状态与原因落盘，重启后保持停机待人工 reset；live 模式启动要求预检通过标记或显式 `--i-understand-live` 类确认。

### M4. 共享状态无线程安全（账本除外）
- 面板与引擎同进程多线程：HTTP 线程里 `web/server.py` 的 `start_task` 会组装任务、触发下单、并替换 `engine.tasks` 列表，引擎主线程同时在迭代该列表 tick；`registry`、`Executor`、各策略状态均无锁。只有 Ledger 做了锁串行化。
- 修复建议：任务启停改为向引擎投递命令、由引擎线程串行执行；或对 registry/executor 加锁。

### M5. 开仓前强平距离估算过粗（方向偏乐观）
- `strategies/rotation_hedge.py:207` `_est_corridor = 100/杠杆`，忽略维持保证金率：真实距离 ≈ 100/杠杆 − mmr×100，估算永远偏大；杠杆 33 倍时估算 3.03% 刚过 3% 门槛、真实值更小，只能靠开仓后复核立即双平收场（白付两轮手续费）。`core/risk.py` 的 `estimate_liq_price` 本身有 mmr 参数，但调用方默认传 0；Lighter/Arcus 的 Position 又不带 leverage（默认 1），liq_price 缺失时回退估算会把距离高估到近 100%。
- 修复建议：估算统一带上各市场 mmr；Position 补齐 leverage 或在 liq 缺失时视为「不可评估」而非「安全」。

### M6. 杠杆从不由程序设置，策略参数与交易所实际杠杆脱节
- 全仓只有 `venues/arcus.py:444` 定义了 `set_leverage`，无任何调用方；HL/Lighter/Aster 适配没有杠杆设置能力，接口层也没有。策略的 `leverage_a/b`、网格 `leverage` 只参与本地估算，用户在交易所侧是什么杠杆就是什么杠杆。
- 修复建议：接口层增加可选 `set_leverage`，任务启动时按参数设置并回读确认；做不到的场所要在预检里明确报错而非静默。

### M7. config.example.yaml 内置可直接照抄的示例任务，与安全教学相悖
- 示例含静态网格区间（55000–65000）、`leverage 5`、以及 `midline_bps: 5` 的**猜测中枢**——而套利策略自己的文档反复强调中枢必须实测、猜错就系统性亏钱。用户按使用说明「复制为 config.yaml」再翻 live，就会按示例参数下单。
- 修复建议：示例任务默认注释掉，或把参数换成明显的占位符并让校验拒绝明显占位值。

### M8. 已知未核实项复核结论
- **Aster V1 签名**：`venues/aster.py` 的 `sign_params`（HMAC-SHA256 对 urlencode 后的 totalParams 签名）与官方示例向量对拍一致（test_venue_aster 11 个用例），签名构造本身未发现错误；端点路径与官方 V1 文档一致。风险不在签名对错，而在 V1 是 Legacy、官方主推 V3（EIP-712），V1 密钥能否新申请以平台政策为准——**上线前必须先用真密钥做一次只读签名接口验证**（如 GET /fapi/v2/balance），这是当前最便宜的核实办法。
- **Arcus 按 clientId 撤单 body**：`venues/arcus.py` 约 424–441 行，代码已如实标注「未核实」，签名载荷与 orderId 版同源、body 按对称形式猜测。补充发现：当撤单返回 False（body 被拒也会是 False 或非 200/202）时，Executor/策略层只是当作没撤掉，**没有任何告警升级**，网格 stop 会误以为已收尾。修复建议：撤单失败要进任务错误通道并在面板标红；body 形状用一次小额实盘核实后固化。

---

## 低

### L1. 三个策略文件头 docstring 仍是第一批的「骨架/不接实盘」措辞
`strategies/grid.py:1-6`、`rotation_hedge.py:1-6`、`premium_arb.py:1-6` 都写着「本批只做纯函数与决策骨架，不接实盘」，与第三批已接通的实现矛盾，误导后续维护与审计。建议随手更新。

### L2. 死代码与双轨残留
- `core/registry.py:73` 的 `claim_api_key`（Lighter nonce 占用登记）有定义、有单测，但生产路径从不调用——同进程内每部署只有一个 adapter 实例、coi 计数器共享，实际风险低，但 DESIGN 声称的隔离并未落地，要么接线要么删。
- `core/engine.py` 的骨架模式（`run_once` + `decide()` 协议）与三策略恒返回 `[]` 的 `decide()` 是第一批遗留双轨，已无实际用途。
- `venues/arcus.py` 的 `build_signed_order` 中 ALO 分支有一处 `ax.to_units(...)` 调用结果未使用（只剩校验副作用），易误读。

### L3. .gitignore 对私钥文件的覆盖靠命名约定
已覆盖 `.env`、`*.key`、`*.pem`、`secrets/`、`data/`、`*.db`、`logs/`（这点没问题）；但用户若把私钥文件命名为 `lighter.txt`、`keys.json` 放仓库根目录，不会被忽略。建议忽略 `*private*`、`*secret*` 类通配或在 README 强制约定 `secrets/` 目录。

### L4. 配置层把整个进程环境变量收进 secrets
`__main__.py` 把 `os.environ` 与 .env 合并后整体作为 env 传入，`core/config.py` 的 `build_config` 把所有非空 env 都塞进 `AppConfig.secrets`。目前未发现泄漏路径（字段 `repr=False`、预检只查存在性、面板不返回 secrets，这几点是对的），但最小化原则下应只收各 venue 需要的键。

### L5. 若干已注释自明的近似（可接受，记录在案）
Aster `max_leverage=0 表示未知`、HL `price_step` 用最细小数近似、paper 权益不随盈亏浮动、HL SDK 自身请求超时未单独核（适配层其余 httpx 客户端均有 15 秒超时）。这些在代码注释里都写明了，不构成隐瞒。

---

## 已验证没问题的关键不变量

1. **下单唯一通道成立**：grep 全仓，策略层与面板没有任何直接调用 `venue.place_order` 的路径，全部下单、撤单、平仓经 `core/execution.py` 的 Executor（venue 内部 `super()` 委托与 dryrun/paper 委托除外，属适配层内部）。
2. **venue 写接口无重试**：Arcus `_post_signed` 明示只发一次；HL/Lighter/Aster 的 place_order 均无重试循环（问题只在异常翻译，见 S1）。
3. **KillSwitch 在进程内的拦截成立**：`Executor.place` 对 `intent="open"` 强制 `assert_allowed`；引擎 tick 在停机时以 `allow_open=False` 调度，三策略都实现停机平仓路径；面板 stop 网格会撤单+平仓（语义在 `web/server.py` 里如实区分了各策略）。
4. **默认配置安全**：无 config 文件 → 全部 paper、零任务（`__main__.py` 空配置路径）；`VenueConfig.mode` 默认 `paper`；`DRY_RUN` 默认 `true`（`core/config.py` 的 `build_config`）。
5. **DRY_RUN 写路径真隔离**：`venues/dryrun.py` 的 place/cancel 只走内置 PaperVenue，代码上不存在转发到 live 场所下单的路径；读接口透传实盘（权益读实盘、持仓读模拟，口径在 docstring 写明，面板总览会混排这两者，使用者需知悉）。
6. **撤单归属（同进程内）成立**：`Executor.cancel` 校验 `owns_order`，`cancel_all_own` 只撤登记为自有的单（限制见 S3/H4）。
7. **密钥泄漏面干净**：grep 未发现任何打印/日志输出密钥值的路径；预检报告只报存在性（test_preflight 有「假密钥不出现在报告里」的断言）；面板 API 不返回配置与密钥。
8. **一键启动不下载不可信内容**：`start.ps1`/`start.sh` 只做 `pip install -e .[live]`（PyPI 官方源），Python 缺失时只提示去 python.org 手动安装，不自动下载二进制——比四个参考仓库的启动器都干净。
9. **精度处理（HL/Arcus/Lighter）有据**：HL 的 5 位有效数字 + `6−szDecimals` 规则、Arcus 的 tick 分段与数量对齐、Lighter 的 decimals 整数换算，均有针对性单测；Aster 签名与官方向量对拍一致（精度问题见 H5，与签名无关）。
10. **许可合规**：LICENSE 为 MIT；NOTICE 逐项列明 Parallax、WGALL、entropy-arb 三家 MIT 来源与各 venue 的移植明细，并声明 perp-dex-tools 仅参考设计。代码内未发现来自 perp-dex-tools 的痕迹：Aster 适配为 Binance 风格的通用写法、自带官方文档出处注释，结构（单文件 transport 注入）与该项目的多文件 client 拆分不同。（注：/tmp 下的参考源码已被清理，无法做逐行比对，此结论基于代码自述、结构与注释证据。）

## 测试覆盖映射（行数 vs 测试数）

| 模块 | 源码行数 | 专属测试 | 备注 |
|---|---|---|---|
| venues/arcus.py + _arcus_sign.py | 876 | 17 | 签名/精度覆盖好 |
| venues/lighter.py | 376 | 11 | mock 签名器 |
| venues/hl.py | 305 | 15 | mock Info/Exchange |
| venues/aster.py | 272 | 11 | 含官方签名向量 |
| venues/paper.py | 152 | 经集成测试 | 无专属文件 |
| venues/dryrun.py | 77 | **0** | 安全关键路径零测试 |
| venues/factory.py | 45 | **0** | dry_run 组装在此 |
| core/execution.py | 217 | 7 | unknown 路径的测试用的是假异常类，未覆盖真实库异常（S1 因此漏检） |
| core/registry.py / risk.py | 80 / 160 | 7 / 12 | 纯函数覆盖好 |
| core/engine.py / config.py / ledger.py | 187 / 157 / 139 | 集成间接覆盖 | ledger 无专属测试；盈亏聚合不存在（M1） |
| strategies（3 个） | 947 | 8 + 8 + 集成 14 | 纯函数与 paper 端到端为主 |
| web/server.py + page.py | 307 | 6 | 未测 CSRF/Host 校验（因为没有，见 H2） |
| tools（recorder/analyze） | 319 | 5 | 方法学与 entropy-arb 一致 |
| core/preflight.py | 298 | 6 | 含密钥不泄漏断言 |
| __main__.py | 82 | **0** | 入口组装（含 caps 缺失，见 H1）未测 |

## 建议修复顺序

1. **S1** 异常翻译统一（venue 层）+ 用真实异常类补测试 —— 所有实盘的前置条件
2. **S2** live 订单终态查询（四所）+ 网格/maker 的成交发现 —— 网格 live 的前置条件
3. **S3** 启动恢复对账 + 使用说明改稿
4. **H1** caps 接线（config + __main__ + 网格启动校验）
5. **H3** 资金费周期字段与换算
6. **H5** Aster 精度对齐（若要用 Aster）
7. **H7 / M5 / M6** 强平监控补齐（套利/网格）与杠杆设置/回读
8. **H4** 任务级归属身份
9. **H2** 面板 token + Host/Origin 校验
10. **H6** paper 行情源（决定 paper 的定位）
11. M1–M4、M7 与低级项批量清理；最后用「只读签名接口 → DRY_RUN → 单所小额」逐所做实盘验证，网格最后开放

## 修复记录（2026-11-03）

按上文「建议修复顺序」逐项修复。全部改动经测试验证：原 127 个测试保持全绿（其中 3 处账本计数断言因 H4 口径改为任务名、1 个冲突测试改为 config 层校验+引擎层隔离双断言），新增回归测试 34 个（tests/test_audit_fixes.py 33 个 + 集成测试新增 1 个），总计 **161 个测试全绿**。未做 git commit/push（待主线统一推送）。

### 严重

- **S1 已修**：core/execution.py 新增异常判定与 `unknown_outcome_guard`——动态收集 requests/httpx/aiohttp 的超时/断连类 + 类名启发式，覆盖 SDK 自有异常。四所 live venue 的 place_order/cancel_order 写路径全部经 guard 翻译为 UnknownOutcomeError。Executor 再兜底：place 中任何未分类异常先对账，持仓变化或对账失败 → unknown（绝不重发），确认未生效 → rejected；cancel 遇 UnknownOutcomeError 返回 False 保留登记下轮重试。回归测试使用真实库异常类（requests.exceptions.ReadTimeout、httpx.ReadTimeout、requests.ConnectTimeout）：HL venue 翻译、Executor 定性、轮换首腿超时只下单一次，均已覆盖。
- **S2 已修**：HL 用 Info.query_order_by_oid 解析终态；Aster 用 GET /fapi/v1/order（oid→symbol 映射在下单/挂单时记录）；Lighter 先查 accountActiveOrders 再查 accountInactiveOrders 历史端点（注：该端点名按 REST 命名规律采用，未在参考实现中实证，代码已注明，失败自动退回对账式口径）；Arcus 在参考实现中确无单订单终态接口，不臆造——poll 只返回挂单中订单，成交发现由策略层「挂单消失+持仓差」对账式完成（grid._reconcile_missing、rotation._maker_final_filled，均记录下单前持仓基线）。mock 终态集成测试：HL/Aster/Lighter poll 终态、网格对账式发现成交并挂反向单，均已覆盖。
- **S3 已修**：Engine 组装任务时各策略 start 先对账：网格先撤该 symbol 未登记的遗留挂单（Executor.cancel_unregistered）再铺单，已有持仓默认登记「未托管」不铺单、显式 takeover_position 参数才接管；轮换按已有持仓初始化为 open 阶段（opened_at 重置、安全检查接管）；套利记录已有持仓、净 delta 交定期对账修正。对账事件记入 engine.events，启动日志打印、面板总览「系统事件」可见。语义已写入 DESIGN 第 12 节与使用说明第 8 节，「重启会重新 claim 并对账」成立。

### 高

- **H1 已修**：config.yaml 新增 caps 顶层（global_usd/venues/strategies，USD 名义，strategies 键为任务名），build_config 严格校验；Engine.from_config 缺省即从 AppConfig.caps 换算传入 Executor（__main__ 不再漏传）；网格 start 前以 estimated_notional 对三层上限硬校验，超限 raise 拒绝启动并记入 engine.errors。测试覆盖 config 接线与硬校验。
- **H2 已修**：面板启动生成随机 token（secrets.token_urlsafe），启动日志打印带 token 的地址，页面内嵌 token、JS 的 POST 自动带 X-API-Token 头；所有 POST 校验 token（secrets.compare_digest），Host 头必须为回环字面值（全部请求），POST 带 Origin 时必须与 Host 同源。测试：无 token/错 token POST 403、跨源 Origin 403、坏 Host 连 GET 也 403。
- **H3 已修**：FundingInfo 增加 interval_seconds，四所如实填（HL 3600、Arcus 3600、Lighter 28800、Aster 28800 且 funding() 注释标明是上一期已结算值）；risk.funding_hourly_rate 统一换算；rotation 选向改用换算后费率。回归测试构造「原始 rate 直比会选反、换算后选对」的混合口径场景并断言方向。
- **H4 已修**：三策略实例新增 owner=任务名（engine.build_task 传入 config 的 name），claim/place/cancel/record_fill/账本 strategy 全部改用 owner；config 校验层新增：任务名必填且唯一、同一 (venue,symbol) 被两个任务占用直接 ConfigError（字段在顶层或 params 两处都查；account 由 venue 唯一决定，故该口径即 (venue,account,symbol)）。原集成测试的冲突用例相应改为「config 层报错」+「直组 AppConfig 时引擎层 claim 隔离」双断言；另加两任务同 (venue,symbol) claim 真互斥测试。
- **H5 已修**：Aster place_order 按 markets()（结果加缓存）的 qty_step 向下对齐数量、price_step 按方向就近对齐价格（买向下/卖向上，不比原价更激进）；对齐后为 0 抛错。单测断言 quantity/price 的实际发送值。
- **H6 已修**：新增 venues/quotes.py 的 PublicQuoteProvider（公开只读：HL 家族 allMids、Aster bookTicker、Lighter orderBooks+orderBookOrders，5 秒缓存、失败返 None；Arcus 公开端点名未核实，明确返 None 走游走并在代码/使用说明注明）；PaperVenue 取价顺序 provider → 随机游走（可设种子、每次 book ±5bps 并触发撮合）→ 静态价，未知 symbol 用兜底价不再 KeyError，内置价扩到 SNDK/SOL/HYPE；factory 为 paper 场所统一注入共享 provider + walk + 按所派生种子。文档（DESIGN 第 9 节、使用说明第 2 节）如实说明来源与局限。
- **H7 已修**：强平监控抽成 Executor 公共函数 position_corridor / estimate_open_corridor，三策略共用：premium_arb 与 grid 的 tick 均加持仓监控（跌破门槛双平/停机平仓；不可评估则暂停开仓不放行），_fire 双腿与 grid 开仓单都带 corridor 估算传 Executor；rotation 的 _safety/_post_open 同步改用公共口径。测试覆盖 premium 不可评估阻断、grid 距离塌陷停机平仓。

### 中

- **M1 未修**（按任务书可不修）：账本 fee 仍恒 0、HL 回执 avgPx 仍丢弃、无已实现盈亏聚合。原因：属统计口径增强，不影响下单安全与本次严重/高问题闭环；各所真实费率口径不一（maker/taker、结算币种），需结合实盘小额验证的真实回执再定口径，避免先写错口径。留待实盘验证批次处理。
- **M2 已修**：rotation maker 部分成交保留跟踪（_maker_partial/_maker_recorded），status=open 只更新已成交量不提前对冲；终态（filled/canceled/超时撤单后复查 poll 或持仓差）才按最终量一次性补记账本增量并对冲。集成测试以「部分成交 40% 后撤单终局」场景断言：等待期不对冲、终局按 40% 量对冲且双腿等量。
- **M3 已修**：KillSwitch 增加 persist_path，halt 写 data/killswitch.json、reset 删文件、构造时恢复，Engine.from_config(state_dir) 接线（__main__ 传账本同目录），恢复事件进 engine.events；人工恢复路径：面板 reset 按钮与 `python -m detsp --reset-killswitch`。预检门：preflight 通过且含 live 场所时写 data/preflight-ok.json（时间 + config 指纹，指纹 = 任务/模式/风控/caps 规范化哈希 + 各密钥值 sha256 拼接，不含明文）；__main__ 在有 live 场所 + 非 DRY_RUN + 未 --paper 时校验标记，缺失/指纹不符拒绝启动（退出码 2）并打印预检命令。测试覆盖落盘恢复、指纹失效。
- **M4 已修（最小集）**：Engine 增加命令队列（run_command/process_commands，tick 开头 drain），PanelState 的 start/stop_task 改为投递命令、在引擎主循环线程串行执行（组装、撤单、平仓都不再发生在 HTTP 线程），超时回错误 dict；OwnershipRegistry 全部 claim/release/查询加可重入锁。共享状态其余部分（策略内部状态、engine.tasks 列表）仍只由引擎线程写、HTTP 线程读，未做全面加锁——这正是命令队列要保证的写串行化，读侧为状态展示可容忍。测试覆盖命令在引擎线程执行；test_web 的启停用例加了命令泵线程模拟主循环。
- **M5 已修**：risk.DEFAULT_MMR=0.05 保守默认；Market 新增 maint_margin_rate（Arcus 由 maintenanceMarginFraction 填入，其余 None → 默认）；Position 新增 leverage_known（Lighter/Arcus 持仓置 False）；position_corridor_pct 在 liq 缺失且杠杆未知时返回 None=不可评估，各策略按「不可评估就阻止开仓/触发安全动作」处理；estimate_open_corridor 统一带 mmr（rotation 旧的 100/杠杆口径已替换）。测试覆盖估算值与不可评估判定。
- **M6 未修**（按任务书可不修）：杠杆仍不由程序设置，仅 arcus.set_leverage 存在且无调用方，接口层无统一 set_leverage。原因：各所杠杆设置接口与逐仓/全仓语义差异大，盲目接线风险高于收益；当前折中是策略杠杆参数必须与账户实际设置一致（已写入使用说明第 4 节），强平距离用真实持仓的 liq/杠杆回读校验（开仓后复核）。留待实盘验证批次逐所接线。
- **M7 已修**：config.example.yaml 示例任务全部注释（只留结构说明与占位符），文件头与 tasks 段注明区间/中枢不可照抄、中枢必须实测；新增 caps 段结构说明。
- **M8 部分修**：Aster 精度问题已随 H5 修复；两项实盘核实**未修**（按任务书可不修）：Aster V1 密钥可用性须用真密钥做只读签名接口验证（如 GET /fapi/v2/balance）、Arcus 按 clientId 撤单 body 须小额实盘核实——两者都需要真实账户，本地无法完成，仍列在 DESIGN 第 12 节遗留项中。补充发现的「Arcus 撤单返回 False 无告警升级」已部分缓解：Executor.cancel 保留登记、策略下轮重试，网格/轮换的停止路径会回报收尾失败信息；逐次撤单失败的独立告警通道仍待实盘阶段结合日志系统处理。

### 低

- **L1 已修**：三策略文件头 docstring 已更新为真实状态（执行状态机 + 本次修复点），不再写「骨架/不接实盘」。
- **L2 未修**（按任务书可不修）：死代码仍在——registry.claim_api_key 无生产调用方、engine 骨架模式 run_once/decide 与三策略 decide 空实现、arcus build_signed_order 的 ALO 分支一处 to_units 结果未用。原因：均为无害残留，删除骨架模式涉及第一批兼容接口的取舍，留待后续清理批一并处理。
- **L3 已修**：.gitignore 增加 *private*、*secret*、keys*.json、*api_key*、lighter*.txt、*.keystore（已用 git check-ignore 验证 keys.json 等命中）。
- **L4 未修**（按任务书可不修）：__main__ 仍把整个 os.environ 收进 AppConfig.secrets。审计已确认无泄漏路径（预检只报存在性、有断言测试），本次未动；后续可按各 venue 需要的键白名单收窄。
- **L5 未修**（按任务书可不修）：若干已在代码注释中自明的近似（HL price_step 近似、Lighter 市价异步结算口径等），审计结论为可接受、记录在案，本次未动。

### 仍未修清单（汇总）

M1（手续费记账与已实现盈亏聚合）、M6（杠杆设置接线）、M8 的两项实盘核实（Aster V1 只读签名验证、Arcus clientId 撤单 body 小额核实）、L2（死代码清理）、L4（secrets 白名单收窄）、L5（已注释自明的近似）。加上原遗留 4 项（实盘小额验证、Aster V3 迁移评估、Arcus clientId 撤单核实、溢价套利深度切片），均需真实账户或后续批次处理；本地 mock/paper 层面已无已知阻塞项。
