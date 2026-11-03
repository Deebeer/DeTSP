# DeTSP

统一永续合约交易系统：一套代码、一个引擎、一个账本。

## 功能

**三种策略**
- 网格
- 轮换对冲刷量
- 溢价套利

**七个交易所**
- Arcus
- Lighter 主网、Lighter Robinhood 链
- Hyperliquid 本体、Entropy、trade.xyz
- Aster

**配套**
- 中文网页面板（本机访问）
- 盘口录制与溢价分析工具
- 实盘预检
- 一键启动

## 当前状态

- 功能已完成，161 个自动化测试全部通过
- 已通过一轮独立代码审计，严重与高优先级问题已修复（见 [AUDIT.md](AUDIT.md)）
- ⚠ 尚未用真实账户验证过：切实盘前必须按使用说明走预检，并用最小金额先试一家

## 快速开始

Windows 双击 `一键启动.bat`；macOS / Linux 运行 `./start.sh`。首次启动自动安装依赖并生成配置。

手工启动：

```bash
pip install -e ".[live]"          # 只跑模拟可改装 .[dev]
cp .env.example .env              # 填密钥，模拟模式可留空
cp config.example.yaml config.yaml
python -m detsp                   # 启动引擎和面板，地址见启动日志
```

完整流程（填密钥 → 跑模拟 → 定参数 → 预检 → 切实盘）见 [使用说明.md](使用说明.md)。

## 文档

- [使用说明.md](使用说明.md)：从安装到实盘的操作手册
- [DESIGN.md](DESIGN.md)：架构与设计说明
- [AUDIT.md](AUDIT.md)：独立审计报告与修复记录

## 安全要点

- 只填 API 钱包私钥，绝不填主钱包私钥或助记词
- 面板只在本机打开，不要改绑公网、不要外传面板地址
- 上实盘的顺序固定：模拟 → 预检通过 → 最小金额试跑
- 全局停机后重启不会自动恢复，必须人工解除

## 许可

MIT，见 [LICENSE](LICENSE)。借鉴的开源项目与署名见 [NOTICE](NOTICE)。
