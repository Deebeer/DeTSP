"""盘口录制与溢价分析工具（溢价套利定阈值用）。

recorder：对一对场所每秒采样盘口、每分钟聚合一行 CSV；
analyze：读 CSV 给出溢价中枢实测值与建议入场带。

入口：
  python -m detsp.tools.recorder --base-venue entropy --hedge-venue lighter_rh --symbol SNDK
  python -m detsp.tools.analyze [--csv logs/minutes.csv] [--fees-bps 1.0]
"""
