# DeTSP

统一永续合约交易系统：一套系统，三种策略，7 个交易所。

- 策略：网格、轮换对冲刷量、溢价套利
- 交易所：Arcus、Lighter 主网、Lighter Robinhood 链、Hyperliquid 本体、Entropy、trade.xyz、Aster
- 设计说明见 [DESIGN.md](DESIGN.md)

## 状态

第四批（计划内功能全部完成）：网页面板（同进程共享状态、只绑 127.0.0.1、无鉴权，含总览/任务启停/账本/KillSwitch 接口）、程序入口 `python -m detsp`（--paper/--no-web）、一键启动（Windows `.bat`+`start.ps1`、macOS/Linux `start.sh`）、盘口录制与溢价分析工具（`detsp.tools.recorder/analyze`）、实盘预检（`python -m detsp.preflight`）、DRY_RUN 代理场所（实盘只读 + 本地模拟下单）、《使用说明.md》。共 127 个测试通过。遗留项只有：实盘小额验证、Aster V3 迁移评估、Arcus 按 clientId 撤单核实、溢价套利深度切片（详见 DESIGN.md 第 12 节）。

第三批：三策略已从决策骨架接通为可执行状态机，统一在引擎内运行——新增 core/execution.py 统一执行层（归属校验、风控门槛、结果未知不重发先对账），config.yaml 新增 tasks 任务组装；网格（铺单/补单/区间外 close 与 recover/人工补格）、轮换对冲（双 taker 与 maker+对冲/到期轮换/孤腿平/开仓后复核）、溢价套利（双腿 taker/持续门/对账与净 delta 修正）均有 paper 端到端集成测试；尚未做实盘小额验证。

第二批：7 个交易所的 live（实盘）适配已全部实现——Hyperliquid 家族（本体/Entropy/trade.xyz，官方 SDK）、Lighter 主网/Robinhood 链（lighter-sdk 1.1.2）、Arcus（Ed25519 签名移植）、Aster（依据官方 V1 文档自写）。paper（模拟）保持可用。全部适配有 mock 单元测试（不联网）；尚未做实盘小额验证，上实盘前务必按 DESIGN.md 第 12 节的未核实清单逐项小额验证。

第一批：设计文档 + 项目骨架 + 核心层（配置/适配接口/归属登记/风控/账本/引擎骨架）+ 模拟适配 + 三策略决策骨架。

## 快速开始

Windows 双击 `一键启动.bat`，macOS/Linux 运行 `./start.sh`（首次自动装依赖并生成配置）。手工方式：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"        # 只要 paper/测试时这样即可
pip install -e ".[live]"       # 实盘再加这个（官方 SDK 等）
cp .env.example .env          # 填密钥（paper 模式可留空）
cp config.example.yaml config.yaml
python -m detsp               # 启动引擎 + 面板（http://127.0.0.1:8320/）
python -m detsp.preflight    # 实盘预检（切 live 前必跑）
python -m pytest              # 运行测试
```

完整操作流程（填密钥、paper 跑通、录制盘口定套利阈值、预检、切实盘、常见问题）见 [使用说明.md](使用说明.md)。

## 安全

- 只填 API 钱包/签名私钥，永远不要填主钱包私钥或助记词。
- 面板默认只监听 127.0.0.1，不要暴露公网。
- 实盘前先用 paper / DRY_RUN 充分验证，再用最小金额试跑。

## 许可

MIT，见 LICENSE；借鉴的开源项目与署名见 NOTICE。
