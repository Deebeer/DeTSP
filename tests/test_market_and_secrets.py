"""数据页扫描器与设置页密钥的测试：计算正确性、单所失败隔离、
状态接口不泄漏值、写入只改目标键且保留注释。全部离线（假 HTTP）。"""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from detsp.core.config import build_config
from detsp.core.engine import Engine
from detsp.core.ledger import Ledger
from detsp.core.market_scanner import (
    MarketScanner, best_funding_pair, best_spread)
from detsp.core.secret_fields import save_secrets, secrets_status
from detsp.venues.paper import PaperVenue
from detsp.web.server import PanelState, serve

# ── 纯函数：价差组合 ──────────────────────────────────────

def test_best_spread_picks_min_buy_max_sell():
    out = best_spread({"a": 100.0, "b": 102.0, "c": 101.0, "d": None})
    assert out["buy_venue"] == "a" and out["sell_venue"] == "b"
    assert out["spread_bps"] == pytest.approx(2 / 101 * 10_000)


def test_best_spread_needs_two_venues():
    assert best_spread({"a": 100.0}) is None
    assert best_spread({"a": None, "b": None}) is None


# ── 纯函数：资金费组合（方向与口径）────────────────────────

def test_best_funding_pair_direction():
    # 费率为正 = 多头付费：多低费率所、空高费率所，净收 = 空 - 多
    out = best_funding_pair({"a": (1.0, "current"), "b": (4.0, "current")})
    assert out["long_venue"] == "a" and out["short_venue"] == "b"
    assert out["edge_bps_h"] == pytest.approx(3.0)
    assert out["mixed"] is False


def test_best_funding_pair_prefers_same_kind_over_bigger_mixed():
    # 混口径组合 edge 更大（9），但存在同口径组合（3）时必须选同
    # 口径——不同时点的费率相减没有意义（审查 H2）
    out = best_funding_pair({
        "a": (1.0, "predicted"), "b": (4.0, "predicted"),
        "c": (10.0, "last_settled")})
    assert out["mixed"] is False
    assert out["long_venue"] == "a" and out["short_venue"] == "b"
    assert out["edge_bps_h"] == pytest.approx(3.0)


def test_best_funding_pair_mixed_only_flags_mixed():
    out = best_funding_pair({"a": (1.0, "predicted"),
                             "b": (5.0, "last_settled")})
    assert out["mixed"] is True
    assert out["edge_bps_h"] == pytest.approx(4.0)


# ── 扫描器：假 HTTP 全所数据 + 单所失败隔离 ─────────────────

class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeHttp:
    """按 URL/载荷路由的假 httpx.Client（公开接口形状与适配一致）。"""

    def post(self, url, json=None, params=None):
        assert "hyperliquid" in url
        body = json or {}
        if body.get("type") == "metaAndAssetCtxs":
            dex = body.get("dex", "")
            if dex == "":
                return _Resp([
                    {"universe": [{"name": "BTC"}, {"name": "ETH"}]},
                    [{"markPx": "60000", "funding": "0.00001"},
                     {"markPx": "3000", "funding": "0.00002"}]])
            if dex == "io":
                return _Resp([
                    {"universe": [{"name": "io:AAPL"}, {"name": "io:SNDK"}]},
                    [{"markPx": "200", "funding": "0.00003"},
                     {"markPx": "250", "funding": "0.00001"}]])
            if dex == "xyz":
                return _Resp([
                    {"universe": [{"name": "xyz:AAPL"}]},
                    [{"markPx": "201", "funding": "0.00005"}]])
        if body.get("type") == "predictedFundings":
            # 场所名按活接口实测形状写 "HlPerp"（不是 "Hyperliquid"）
            return _Resp([["BTC", [["HlPerp", {
                "fundingRate": "0.00003",
                "nextFundingTime": 1_900_000_000_000}]]]])
        if body.get("type") == "candleSnapshot":
            coin = (body.get("req") or {}).get("coin")
            assert coin == "BTC"
            return _Resp([{"h": "61000", "l": "59000"},
                          {"h": "62000", "l": "59500"}])
        raise AssertionError(f"未预期的 POST {body}")

    def get(self, url, params=None):
        if "rh.lighter" in url:
            raise RuntimeError("RH 接口故障（模拟）")
        if "asterdex" in url and "premiumIndex" in url:
            return _Resp([{"symbol": "BTCUSDT", "markPrice": "60100",
                           "lastFundingRate": "0.0008",
                           "nextFundingTime": 1_900_000_000_000}])
        if "arcus" in url and "/v1/markets" in url:
            return _Resp({"markets": [{
                "baseAsset": "BTC", "marketDisplayName": "BTC-PERP",
                "nextFundingRate": "0.00002"}]})
        if "arcus" in url and "/v1/bbo/" in url:
            return _Resp({"bestBid": {"price": "60050"},
                          "bestAsk": {"price": "60060"}})
        if "zklighter" in url and "orderBooks" in url:
            return _Resp({"order_books": [
                {"symbol": "BTC", "market_id": 1, "status": "active"}]})
        if "zklighter" in url and "funding-rates" in url:
            return _Resp({"funding_rates": [
                {"market_id": 1, "exchange": "lighter", "rate": 0.00016}]})
        if "zklighter" in url and "orderBookOrders" in url:
            return _Resp({"bids": [{"price": "60010"}],
                          "asks": [{"price": "60020"}]})
        raise AssertionError(f"未预期的 GET {url}")


@pytest.fixture()
def scanner():
    return MarketScanner(http=FakeHttp(), watchlist=["BTC", "ETH", "AAPL"])


def test_scanner_full_table_and_failure_isolation(scanner):
    out = scanner.scan()
    # RH 所模拟故障：只标它一家，其余数据齐全
    assert "lighter_rh" in out["venue_errors"]
    assert set(out["venue_errors"]) == {"lighter_rh"}
    by_sym = {s["symbol"]: s for s in out["symbols"]}
    btc = by_sym["BTC"]
    # HL 本体：predictedFundings 升级口径为 predicted（0.00003/h）
    hl = btc["venues"]["hyperliquid"]
    assert hl["price"] == 60_000.0
    assert hl["rate_kind"] == "predicted"
    assert hl["funding_bps_h"] == pytest.approx(0.3)
    # Aster：last_settled，8 小时 0.0008 → 1 bps/h
    ast = btc["venues"]["aster"]
    assert ast["rate_kind"] == "last_settled"
    assert ast["funding_bps_h"] == pytest.approx(1.0)
    # Lighter 主网：current，8 小时 0.00016 → 0.2 bps/h；价格走 BBO 中间价
    lt = btc["venues"]["lighter"]
    assert lt["price"] == pytest.approx(60_015.0)
    assert lt["funding_bps_h"] == pytest.approx(0.2)
    # Arcus：BBO 中间价 + predicted
    assert btc["venues"]["arcus"]["price"] == pytest.approx(60_055.0)
    # 价差组合：最便宜 hyperliquid 买、最贵 aster 卖
    assert btc["best_spread"]["buy_venue"] == "hyperliquid"
    assert btc["best_spread"]["sell_venue"] == "aster"
    # 资金费组合优先同口径：predicted 组（HL 0.3 / Arcus 0.2）里选向
    bf = btc["best_funding"]
    assert bf["mixed"] is False and bf["rate_kind"] == "predicted"
    assert bf["long_venue"] == "arcus" and bf["short_venue"] == "hyperliquid"
    # Entropy 只有股票：BTC 在该所未上市（三态：not_listed，不是故障）
    ent_btc = by_sym["BTC"]["venues"]["entropy"]
    assert ent_btc["status"] == "not_listed"
    assert by_sym["BTC"]["listed_count"] == 4  # arcus/lighter/HL/aster；io、xyz 未上市，RH 故障不计
    assert by_sym["AAPL"]["venues"]["entropy"]["price"] == 200.0
    assert by_sym["AAPL"]["venues"]["entropy"]["status"] == "ok"
    # 排行已排序且含 BTC
    assert out["spread_ranking"][0]["symbol"] in ("BTC", "AAPL")


def test_scanner_cache(scanner):
    first = scanner.scan()
    second = scanner.scan()
    assert first is second  # 缓存命中直接返回同一结果对象


def test_candles_hl_suggested_range(scanner):
    out = scanner.candles("hyperliquid", "BTC")
    assert out["ok"] is True
    assert out["high"] == 62_000.0 and out["low"] == 59_000.0
    assert out["suggested_lower"] == pytest.approx(59_000 * 0.98)
    assert out["suggested_upper"] == pytest.approx(62_000 * 1.02)


def test_candles_unavailable_for_non_hl(scanner):
    out = scanner.candles("aster", "BTC")
    assert out["ok"] is False and "error" in out


# ── 密钥：状态不泄漏 + 写入保注释 ──────────────────────────

SECRET_VALUE = "0xTESTSECRET_DO_NOT_LEAK_1234567890"


@pytest.fixture()
def env_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "# DeTSP 配置示例注释（必须保留）\n"
        "DRY_RUN=true\n"
        "\n"
        "# ===== Aster =====\n"
        f"ASTER_API_KEY={SECRET_VALUE}\n"
        "ASTER_SECRET_KEY=\n"
        "HL_PRIVATE_KEY=\n", encoding="utf-8")
    return p


def test_secrets_status_never_leaks_values(env_file):
    groups = secrets_status(env_file)
    blob = json.dumps(groups, ensure_ascii=False)
    assert SECRET_VALUE not in blob  # 响应里绝不能出现已写入的密钥
    aster = next(g for g in groups if g["key"] == "aster")
    by_name = {f["name"]: f for f in aster["fields"]}
    assert by_name["ASTER_API_KEY"]["filled"] is True
    assert by_name["ASTER_SECRET_KEY"]["filled"] is False
    hl = next(g for g in groups if g["key"] == "hyperliquid")
    assert {f["name"] for f in hl["fields"]} >= {
        "HL_ACCOUNT_ADDRESS", "HL_PRIVATE_KEY"}


def test_save_secrets_only_target_keys_and_keeps_comments(env_file):
    changed = save_secrets(env_file, {
        "ASTER_SECRET_KEY": "new-secret-value",
        "HL_PRIVATE_KEY": "  padded-key  ",
        "ASTER_API_KEY": "",            # 空值 = 不修改
        "LIGHTER_ACCOUNT_INDEX": "42",  # 文件里没有的键 → 追加
    })
    assert changed == ["ASTER_SECRET_KEY", "HL_PRIVATE_KEY",
                       "LIGHTER_ACCOUNT_INDEX"]
    text = env_file.read_text(encoding="utf-8")
    assert "# DeTSP 配置示例注释（必须保留）" in text
    assert "# ===== Aster =====" in text
    assert f"ASTER_API_KEY={SECRET_VALUE}" in text      # 原值未被动
    assert "ASTER_SECRET_KEY=new-secret-value" in text  # 原地替换空值行
    assert "HL_PRIVATE_KEY=padded-key" in text          # 去空白后写入
    assert "LIGHTER_ACCOUNT_INDEX=42" in text           # 新键已追加
    assert "DRY_RUN=true" in text


def test_save_secrets_rejects_unknown_and_newline(env_file):
    with pytest.raises(ValueError):
        save_secrets(env_file, {"NOT_A_FIELD": "x"})
    with pytest.raises(ValueError):
        save_secrets(env_file, {"ASTER_API_KEY": "a\nb"})
    # 全部空值 → 无写入、无变化
    assert save_secrets(env_file, {"ASTER_API_KEY": "  "}) == []


# ── HTTP 层：/api/market 与 /api/settings/secrets ──────────

class _FakeScanner:
    def scan(self, force=False):
        return {"updated_at": 1.0, "watchlist": ["BTC"], "symbols": [],
                "spread_ranking": [], "funding_ranking": [],
                "venue_errors": {}}

    def candles(self, venue_id, symbol):
        return {"venue": venue_id, "symbol": symbol, "ok": False,
                "error": "不可用（假扫描器）"}


def _req(base, path, method="GET", token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, method=method, data=data)
    if token:
        req.add_header("X-API-Token", token)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


@pytest.fixture()
def panel(tmp_path):
    cfg = build_config(env={}, yaml_data={})
    venues = {"arcus": PaperVenue(venue_id="arcus")}
    engine = Engine.from_config(cfg, venues, ledger=Ledger())
    env_path = tmp_path / ".env"
    env_path.write_text(f"ASTER_API_KEY={SECRET_VALUE}\n",
                        encoding="utf-8")
    state = PanelState(engine=engine, venues=venues, ledger=Ledger(),
                       app_config=cfg, env_path=env_path,
                       market_scanner=_FakeScanner())
    server = serve(state, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, state, env_path
    server.shutdown()


def test_http_market_and_candles(panel):
    base, _, _ = panel
    status, data = _req(base, "/api/market")
    assert status == 200 and data["watchlist"] == ["BTC"]
    status, data = _req(base, "/api/market/candles?venue=aster&symbol=BTC")
    assert status == 200 and data["ok"] is False


def test_http_secrets_get_no_leak_and_post_saves(panel):
    base, state, env_path = panel
    status, data = _req(base, "/api/settings/secrets")
    assert status == 200
    assert SECRET_VALUE not in json.dumps(data, ensure_ascii=False)
    # POST 无 token → 403
    status, _ = _req(base, "/api/settings/secrets", method="POST",
                     body={"ASTER_SECRET_KEY": "v"})
    assert status == 403
    # 带 token 保存成功；回执与事件都不含值
    status, data = _req(base, "/api/settings/secrets", method="POST",
                        token=state.api_token, body={"ASTER_SECRET_KEY": "brand-new-value"})
    assert status == 200 and data["ok"] is True
    assert data["changed"] == ["ASTER_SECRET_KEY"]
    assert "brand-new-value" not in json.dumps(data, ensure_ascii=False)
    assert "ASTER_SECRET_KEY=brand-new-value" in \
        env_path.read_text(encoding="utf-8")
    assert all("brand-new-value" not in e for e in state.engine.events)
    # 未知字段 → 400
    status, data = _req(base, "/api/settings/secrets", method="POST",
                        token=state.api_token, body={"WHATEVER": "x"})
    assert status == 400
