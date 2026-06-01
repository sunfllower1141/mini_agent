#!/usr/bin/env python3
"""
test_trading_ops.py — tests for Alpaca trading tools (mock-based, no live API needed).
"""
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from conftest import make_tool_call as _make_tool_call, make_gates as _gates
from tools import execute_tool, ToolResult


class TestAlpacaAccount(unittest.TestCase):
    """Tests for alpaca_account."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    @patch("tools.trading_ops._get_client")
    def test_returns_account_details(self, mock_client):
        mock_trading = MagicMock()
        mock_account = MagicMock()
        mock_account.id = "abc123-uuid"
        mock_account.status = "ACTIVE"
        mock_account.currency = "USD"
        mock_account.cash = "50000.00"
        mock_account.buying_power = "100000.00"
        mock_account.portfolio_value = "75000.00"
        mock_account.equity = "75000.00"
        mock_account.daytrade_count = 0
        mock_account.pattern_day_trader = False
        mock_trading.get_account.return_value = mock_account
        mock_client.return_value = {"trading": mock_trading, "data": MagicMock(), "live": False}

        tc = _make_tool_call("alpaca_account")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        data = json.loads(result.content)
        self.assertEqual(data["cash"], "50000.00")
        self.assertEqual(data["buying_power"], "100000.00")
        self.assertFalse(data["live"])

    @patch("tools.trading_ops._get_client")
    def test_handles_api_error(self, mock_client):
        mock_trading = MagicMock()
        mock_trading.get_account.side_effect = RuntimeError("API down")
        mock_client.return_value = {"trading": mock_trading, "data": MagicMock(), "live": False}

        tc = _make_tool_call("alpaca_account")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("API down", result.content)


class TestAlpacaPositions(unittest.TestCase):
    """Tests for alpaca_positions."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    @patch("tools.trading_ops._get_client")
    def test_lists_all_positions(self, mock_client):
        mock_trading = MagicMock()
        pos1 = MagicMock()
        pos1.symbol = "AAPL"; pos1.qty = "10"; pos1.market_value = "1800.00"
        pos1.cost_basis = "1700.00"; pos1.unrealized_pl = "100.00"
        pos1.unrealized_plpc = "0.0588"; pos1.side = "long"
        pos2 = MagicMock()
        pos2.symbol = "TSLA"; pos2.qty = "5"; pos2.market_value = "1250.00"
        pos2.cost_basis = "1300.00"; pos2.unrealized_pl = "-50.00"
        pos2.unrealized_plpc = "-0.0385"; pos2.side = "long"
        mock_trading.get_all_positions.return_value = [pos1, pos2]
        mock_client.return_value = {"trading": mock_trading, "data": MagicMock(), "live": False}

        tc = _make_tool_call("alpaca_positions")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        data = json.loads(result.content)
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["symbol"], "AAPL")
        self.assertEqual(data[1]["symbol"], "TSLA")

    @patch("tools.trading_ops._get_client")
    def test_filters_by_symbol(self, mock_client):
        mock_trading = MagicMock()
        pos = MagicMock()
        pos.symbol = "AAPL"; pos.qty = "10"; pos.market_value = "1800.00"
        pos.cost_basis = "1700.00"; pos.unrealized_pl = "100.00"
        pos.unrealized_plpc = "0.0588"; pos.side = "long"
        mock_trading.get_open_position.return_value = pos
        mock_client.return_value = {"trading": mock_trading, "data": MagicMock(), "live": False}

        tc = _make_tool_call("alpaca_positions", symbol="AAPL")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        data = json.loads(result.content)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["symbol"], "AAPL")

    @patch("tools.trading_ops._get_client")
    def test_empty_positions(self, mock_client):
        mock_trading = MagicMock()
        mock_trading.get_all_positions.return_value = []
        mock_client.return_value = {"trading": mock_trading, "data": MagicMock(), "live": False}

        tc = _make_tool_call("alpaca_positions")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        data = json.loads(result.content)
        self.assertEqual(data, [])


class TestAlpacaOrders(unittest.TestCase):
    """Tests for alpaca_orders."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    @patch("tools.trading_ops._get_client")
    def test_lists_orders_default(self, mock_client):
        mock_trading = MagicMock()
        order = MagicMock()
        order.id = "ord-123"; order.symbol = "AAPL"; order.side = "buy"
        order.type = "market"; order.qty = "10"; order.filled_qty = "10"
        order.limit_price = None; order.stop_price = None
        order.status = "filled"
        order.created_at = "2026-01-15T10:00:00Z"
        order.filled_at = "2026-01-15T10:00:01Z"
        mock_trading.get_orders.return_value = [order]
        mock_client.return_value = {"trading": mock_trading, "data": MagicMock(), "live": False}

        tc = _make_tool_call("alpaca_orders")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        data = json.loads(result.content)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["symbol"], "AAPL")
        self.assertEqual(data[0]["status"], "filled")

    @patch("tools.trading_ops._get_client")
    def test_filters_by_status(self, mock_client):
        mock_trading = MagicMock()
        mock_trading.get_orders.return_value = []
        mock_client.return_value = {"trading": mock_trading, "data": MagicMock(), "live": False}

        tc = _make_tool_call("alpaca_orders", status="open")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        mock_trading.get_orders.assert_called_once_with(status="open", limit=20)


class TestAlpacaPlaceOrder(unittest.TestCase):
    """Tests for alpaca_place_order."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    @patch("tools.trading_ops._get_client")
    def test_market_order_succeeds(self, mock_client):
        mock_trading = MagicMock()
        order = MagicMock()
        order.id = "ord-market"; order.symbol = "AAPL"; order.side = "buy"
        order.type = "market"; order.qty = "10"
        order.limit_price = None; order.stop_price = None
        order.status = "filled"; order.created_at = "2026-01-15T10:00:00Z"
        mock_trading.submit_order.return_value = order
        mock_client.return_value = {"trading": mock_trading, "data": MagicMock(), "live": False}

        tc = _make_tool_call("alpaca_place_order",
                             symbol="AAPL", side="buy", qty=10, type="market")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        data = json.loads(result.content)
        self.assertEqual(data["symbol"], "AAPL")
        self.assertEqual(data["side"], "buy")

    @patch("tools.trading_ops._get_client")
    def test_limit_order_requires_price(self, mock_client):
        mock_client.return_value = {"trading": MagicMock(), "data": MagicMock(), "live": False}

        tc = _make_tool_call("alpaca_place_order",
                             symbol="AAPL", side="buy", qty=10, type="limit")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("limit_price", result.content.lower())

    @patch("tools.trading_ops._get_client")
    def test_missing_symbol_fails(self, mock_client):
        mock_client.return_value = {"trading": MagicMock(), "data": MagicMock(), "live": False}

        tc = _make_tool_call("alpaca_place_order", side="buy", qty=10)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("symbol", result.content.lower())

    @patch("tools.trading_ops._get_client")
    def test_notional_order(self, mock_client):
        mock_trading = MagicMock()
        order = MagicMock()
        order.id = "ord-notional"; order.symbol = "TSLA"; order.side = "buy"
        order.type = "market"; order.qty = None
        order.limit_price = None; order.stop_price = None
        order.status = "filled"; order.created_at = "2026-01-15T10:00:00Z"
        mock_trading.submit_order.return_value = order
        mock_client.return_value = {"trading": mock_trading, "data": MagicMock(), "live": False}

        tc = _make_tool_call("alpaca_place_order",
                             symbol="TSLA", side="buy", notional=500.00)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)

    @patch("tools.trading_ops._get_client")
    def test_stop_order_requires_stop_price(self, mock_client):
        mock_client.return_value = {"trading": MagicMock(), "data": MagicMock(), "live": False}

        tc = _make_tool_call("alpaca_place_order",
                             symbol="AAPL", side="sell", qty=10, type="stop")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("stop_price", result.content.lower())


class TestAlpacaCancelOrder(unittest.TestCase):
    """Tests for alpaca_cancel_order."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    @patch("tools.trading_ops._get_client")
    def test_cancel_by_id(self, mock_client):
        mock_trading = MagicMock()
        mock_client.return_value = {"trading": mock_trading, "data": MagicMock(), "live": False}

        tc = _make_tool_call("alpaca_cancel_order", order_id="ord-123")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        mock_trading.cancel_order_by_id.assert_called_once_with("ord-123")

    @patch("tools.trading_ops._get_client")
    def test_cancel_all(self, mock_client):
        mock_trading = MagicMock()
        mock_client.return_value = {"trading": mock_trading, "data": MagicMock(), "live": False}

        tc = _make_tool_call("alpaca_cancel_order", cancel_all=True)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        mock_trading.cancel_orders.assert_called_once()

    @patch("tools.trading_ops._get_client")
    def test_neither_id_nor_all_fails(self, mock_client):
        mock_client.return_value = {"trading": MagicMock(), "data": MagicMock(), "live": False}

        tc = _make_tool_call("alpaca_cancel_order")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)


class TestAlpacaBars(unittest.TestCase):
    """Tests for alpaca_bars."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    @patch("tools.trading_ops._get_client")
    def test_returns_bars(self, mock_client):
        mock_data = MagicMock()
        bar = MagicMock()
        bar.timestamp = "2026-01-15T10:00:00Z"
        bar.open = 170.00; bar.high = 172.00; bar.low = 169.00
        bar.close = 171.50; bar.volume = 100000
        mock_data.get_stock_bars.return_value = {"AAPL": [bar]}
        mock_client.return_value = {"trading": MagicMock(), "data": mock_data, "live": False}

        tc = _make_tool_call("alpaca_bars", symbol="AAPL", timeframe="1day", limit=5)
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        data = json.loads(result.content)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["open"], 170.00)
        self.assertEqual(data[0]["close"], 171.50)

    @patch("tools.trading_ops._get_client")
    def test_missing_symbol_fails(self, mock_client):
        mock_client.return_value = {"trading": MagicMock(), "data": MagicMock(), "live": False}

        tc = _make_tool_call("alpaca_bars")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)
        self.assertIn("symbol", result.content.lower())

    @patch("tools.trading_ops._get_client")
    def test_empty_bars(self, mock_client):
        mock_data = MagicMock()
        mock_data.get_stock_bars.return_value = {"AAPL": []}
        mock_client.return_value = {"trading": MagicMock(), "data": mock_data, "live": False}

        tc = _make_tool_call("alpaca_bars", symbol="AAPL")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        data = json.loads(result.content)
        self.assertEqual(data, [])


class TestAlpacaAsset(unittest.TestCase):
    """Tests for alpaca_asset."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    @patch("tools.trading_ops._get_client")
    def test_lookup_by_symbol(self, mock_client):
        mock_trading = MagicMock()
        asset = MagicMock()
        asset.id = "asset-uuid"; asset.asset_class = "us_equity"
        asset.symbol = "AAPL"; asset.name = "Apple Inc."
        asset.status = "active"; asset.tradable = True
        asset.marginable = True; asset.shortable = True
        asset.easy_to_borrow = True; asset.fractionable = True
        mock_trading.get_asset.return_value = asset
        mock_client.return_value = {"trading": mock_trading, "data": MagicMock(), "live": False}

        tc = _make_tool_call("alpaca_asset", symbol="AAPL")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)
        data = json.loads(result.content)
        self.assertEqual(data["symbol"], "AAPL")
        self.assertTrue(data["tradable"])

    @patch("tools.trading_ops._get_client")
    def test_lookup_by_id(self, mock_client):
        mock_trading = MagicMock()
        asset = MagicMock()
        asset.id = "asset-uuid"; asset.asset_class = "us_equity"
        asset.symbol = "AAPL"; asset.name = "Apple Inc."
        asset.status = "active"; asset.tradable = True
        asset.marginable = True; asset.shortable = True
        asset.easy_to_borrow = True; asset.fractionable = True
        mock_trading.get_asset.return_value = asset
        mock_client.return_value = {"trading": mock_trading, "data": MagicMock(), "live": False}

        tc = _make_tool_call("alpaca_asset", asset_id="asset-uuid")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertTrue(result.success)

    @patch("tools.trading_ops._get_client")
    def test_neither_symbol_nor_id_fails(self, mock_client):
        mock_client.return_value = {"trading": MagicMock(), "data": MagicMock(), "live": False}

        tc = _make_tool_call("alpaca_asset")
        result = execute_tool(tc, self.write_gate, self.read_gate)
        self.assertFalse(result.success)


class TestAlpacaToolSummaries(unittest.TestCase):
    """Tests for @_summarize decorators on all trading tools."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.write_gate, self.read_gate = _gates(self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _summary(self, name: str, **kwargs) -> str:
        from tools import tool_summary
        tc = _make_tool_call(name, **kwargs)
        return tool_summary(tc)

    def test_account_summary(self):
        self.assertEqual(self._summary("alpaca_account"), "alpaca_account()")

    def test_positions_summary(self):
        self.assertEqual(self._summary("alpaca_positions"), "alpaca_positions()")
        self.assertIn("AAPL", self._summary("alpaca_positions", symbol="AAPL"))

    def test_orders_summary(self):
        self.assertIn("all", self._summary("alpaca_orders"))
        self.assertIn("open", self._summary("alpaca_orders", status="open"))

    def test_place_order_summary(self):
        s = self._summary("alpaca_place_order", symbol="AAPL", side="buy")
        self.assertIn("AAPL", s)
        self.assertIn("buy", s)

    def test_cancel_order_summary(self):
        self.assertIn("?", self._summary("alpaca_cancel_order"))
        self.assertIn("cancel_all=True", self._summary("alpaca_cancel_order", cancel_all=True))
        self.assertIn("ord-1", self._summary("alpaca_cancel_order", order_id="ord-1"))

    def test_bars_summary(self):
        s = self._summary("alpaca_bars", symbol="SPY", timeframe="1hour")
        self.assertIn("SPY", s)
        self.assertIn("1hour", s)

    def test_asset_summary(self):
        self.assertIn("AAPL", self._summary("alpaca_asset", symbol="AAPL"))


if __name__ == "__main__":
    unittest.main()
