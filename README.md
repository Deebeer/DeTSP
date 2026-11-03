# DeTSP

统一永续合约交易系统：一套系统，三种策略，7 个交易所。

- 策略：网格、轮换对冲刷量、溢价套利
- 交易所：Arcus、Lighter 主网、Lighter Robinhood 链、Hyperliquid 本体、Entropy、trade.xyz、Aster
- 设计说明见 [DESIGN.md](DESIGN.md)

## 状态

第二批：7 个交易所的 live（实盘）适配已全部实现——Hyperliquid 家族（本体/Entropy/trade.xyz，官方 SDK）、Lighter 主网/Robinhood 链（lighter-sdk 1.1.2）、Arcus（Ed25519 签名移植）、Aster（依据官方 V1 文档自写）。paper（模拟）保持可用。全部适配有 mock 单元测试（不联网），共 89 个测试通过；尚未做实盘小额验证，上实盘前务必按 DESIGN.md 第 12 节的未核实清单逐项小额验证。

第一批：设计文档 + 项目骨架 + 核心层（配置/适配接口/归属登记/风控/账本/引擎骨架）+ 模拟适配 + 三策略决策骨架。

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"        # 只要 paper/测试时这样即可
pip install -e ".[live]"       # 实盘再加这个（官方 SDK 等）
cp .env.example .env          # 填密钥（paper 模式可留空）
python -m pytest              # 运行测试
```

## 安全

- 只填 API 钱包/签名私钥，永远不要填主钱包私钥或助记词。
- 面板默认只监听 127.0.0.1，不要暴露公网。
- 实盘前先用 paper / DRY_RUN 充分验证，再用最小金额试跑。

## 许可

MIT，见 LICENSE；借鉴的开源项目与署名见 NOTICE。
