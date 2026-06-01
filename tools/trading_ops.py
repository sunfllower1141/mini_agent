#!/usr/bin/env python3
"""
trading_ops.py — Alpaca Trade API tools for mini_agent.

Tools: alpaca_account, alpaca_positions, alpaca_orders, alpaca_place_order,
       alpaca_cancel_order, alpaca_bars, alpaca_asset

Uses ALPACA_API_KEY and ALPACA_SECRET_KEY from the workspace .env file.
Defaults to paper trading (paper-api.alpaca.markets); set ALPACA_LIVE=true
in .env for live trading.
"""
from __future__ import annotations

import json
import os

from safety import ReadSafetyGate, WriteSafetyGate
from tools import _register, _summarize, ToolResult


# ---------------------------------------------------------------------------
# Alpaca client (lazy init — only connects on first use)
# ---------------------------------------------------------------------------

_client = None


def _get_client():
    """Return a lazily-initialised Alpaca TradingClient (paper by default)."""
    global _client
    if _client is not None:
        return _client

    from alpaca.trading.client import TradingClient
    from alpaca.data.historical import StockHistoricalDataClient

    # Read creds from .env in workspace
    workspace = os.environ.get("WORKSPACE_DIR", os.getcwd())
    env_path = os.path.join(workspace, ".env")
    _load_dotenv(env_path)

    api_key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    use_live = os.environ.get("ALPACA_LIVE", "").strip().lower() == "true"

    if not api_key or not secret_key:
        raise ValueError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY not set. "
            "Add them to .env in the workspace."
        )

    base_url = "https://api.alpaca.markets" if use_live else "https://paper-api.alpaca.markets"
    data_url = "https://data.alpaca.markets" if use_live else "https://data.alpaca.markets"

    _client = {
        "trading": TradingClient(api_key=api_key, secret_key=secret_key, paper=not use_live),
        "data": StockHistoricalDataClient(api_key=api_key, secret_key=secret_key, url_override=data_url),
        "live": use_live,
    }
    return _client


def _load_dotenv(path: str) -> None:
    """Parse KEY=VALUE lines from a .env file into os.environ (no override)."""
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# alpaca_account — account details
# ---------------------------------------------------------------------------

@_register("alpaca_account")
def _alpaca_account(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Return the current Alpaca account details (cash, buying power, etc.)."""
    try:
        client = _get_client()
        account = client["trading"].get_account()
        return ToolResult(
            success=True,
            content=json.dumps({
                "id": account.id,
                "status": account.status,
                "currency": account.currency,
                "cash": account.cash,
                "buying_power": account.buying_power,
                "portfolio_value": account.portfolio_value,
                "equity": account.equity,
                "daytrade_count": account.daytrade_count,
                "pattern_day_trader": account.pattern_day_trader,
                "live": client["live"],
            }, indent=2),
        )
    except Exception as e:
        return ToolResult(success=False, content=str(e))


@_summarize("alpaca_account")
def _alpaca_account_summary(_args: dict) -> str:
    return "alpaca_account()"


# ---------------------------------------------------------------------------
# alpaca_positions — list current positions
# ---------------------------------------------------------------------------

@_register("alpaca_positions")
def _alpaca_positions(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """List current open positions. Optionally filter by symbol."""
    try:
        client = _get_client()
        symbol = args.get("symbol", "").strip().upper() or None

        if symbol:
            try:
                pos = client["trading"].get_open_position(symbol)
                positions = [pos]
            except Exception:
                positions = []
        else:
            positions = client["trading"].get_all_positions()

        result = []
        for p in positions:
            result.append({
                "symbol": p.symbol,
                "qty": p.qty,
                "market_value": p.market_value,
                "cost_basis": p.cost_basis,
                "unrealized_pl": p.unrealized_pl,
                "unrealized_plpc": p.unrealized_plpc,
                "side": p.side,
            })
        return ToolResult(success=True, content=json.dumps(result, indent=2))
    except Exception as e:
        return ToolResult(success=False, content=str(e))


@_summarize("alpaca_positions")
def _alpaca_positions_summary(args: dict) -> str:
    sym = args.get("symbol", "")
    return f"alpaca_positions(symbol={sym!r})" if sym else "alpaca_positions()"


# ---------------------------------------------------------------------------
# alpaca_orders — list orders
# ---------------------------------------------------------------------------

@_register("alpaca_orders")
def _alpaca_orders(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """List recent orders. Optionally filter by status and limit count."""
    try:
        client = _get_client()
        status = args.get("status", "all").lower()
        limit = min(int(args.get("limit", 20)), 100)

        filters = {}
        if status != "all":
            filters["status"] = status
        if limit:
            filters["limit"] = limit

        orders = client["trading"].get_orders(**filters)

        result = []
        for o in orders:
            result.append({
                "id": o.id,
                "symbol": o.symbol,
                "side": o.side,
                "type": o.type,
                "qty": o.qty,
                "filled_qty": o.filled_qty,
                "limit_price": o.limit_price,
                "stop_price": o.stop_price,
                "status": o.status,
                "created_at": str(o.created_at),
                "filled_at": str(o.filled_at) if hasattr(o, "filled_at") and o.filled_at else None,
            })
        return ToolResult(success=True, content=json.dumps(result, indent=2))
    except Exception as e:
        return ToolResult(success=False, content=str(e))


@_summarize("alpaca_orders")
def _alpaca_orders_summary(args: dict) -> str:
    status = args.get("status", "all")
    return f"alpaca_orders(status={status!r})"


# ---------------------------------------------------------------------------
# alpaca_place_order — submit a buy/sell order
# ---------------------------------------------------------------------------

@_register("alpaca_place_order")
def _alpaca_place_order(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Place a stock order via Alpaca. Supports market, limit, stop, stop_limit.

    Required: symbol, side (buy/sell), qty (number of shares or fractional),
              type (market/limit/stop/stop_limit), time_in_force (day/gtc/opg/cls/ioc/fok).

    Optional: limit_price (for limit/stop_limit), stop_price (for stop/stop_limit),
              notional (dollar amount instead of qty, for fractional orders).
    """
    from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
    from alpaca.trading.requests import StopOrderRequest, StopLimitOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    try:
        client = _get_client()
        symbol = args.get("symbol", "").strip().upper()
        side = args.get("side", "buy").lower()
        qty = args.get("qty")
        notional = args.get("notional")
        order_type = args.get("type", "market").lower()
        time_in_force = args.get("time_in_force", "day").lower()
        limit_price = args.get("limit_price")
        stop_price = args.get("stop_price")

        if not symbol:
            return ToolResult(success=False, content="Missing required parameter: 'symbol'")
        if qty is None and notional is None:
            return ToolResult(success=False, content="Provide 'qty' (shares) or 'notional' (dollar amount)")

        # Map enums
        side_enum = OrderSide.BUY if side == "buy" else OrderSide.SELL
        tif_map = {
            "day": TimeInForce.DAY, "gtc": TimeInForce.GTC,
            "opg": TimeInForce.OPG, "cls": TimeInForce.CLS,
            "ioc": TimeInForce.IOC, "fok": TimeInForce.FOK,
        }
        tif_enum = tif_map.get(time_in_force, TimeInForce.DAY)

        common = {"symbol": symbol, "side": side_enum, "time_in_force": tif_enum}
        if qty is not None:
            common["qty"] = float(qty)
        if notional is not None:
            common["notional"] = float(notional)

        if order_type == "market":
            order_data = MarketOrderRequest(**common)
        elif order_type == "limit":
            if limit_price is None:
                return ToolResult(success=False, content="'limit_price' required for limit orders")
            order_data = LimitOrderRequest(limit_price=float(limit_price), **common)
        elif order_type == "stop":
            if stop_price is None:
                return ToolResult(success=False, content="'stop_price' required for stop orders")
            order_data = StopOrderRequest(stop_price=float(stop_price), **common)
        elif order_type == "stop_limit":
            if limit_price is None or stop_price is None:
                return ToolResult(success=False, content="'limit_price' and 'stop_price' required for stop_limit orders")
            order_data = StopLimitOrderRequest(
                limit_price=float(limit_price), stop_price=float(stop_price), **common
            )
        else:
            return ToolResult(success=False, content=f"Unknown order type: {order_type}")

        order = client["trading"].submit_order(order_data)
        return ToolResult(
            success=True,
            content=json.dumps({
                "id": order.id,
                "symbol": order.symbol,
                "side": order.side,
                "type": order.type,
                "qty": order.qty,
                "limit_price": order.limit_price,
                "stop_price": order.stop_price,
                "status": order.status,
                "created_at": str(order.created_at),
            }, indent=2),
        )
    except Exception as e:
        return ToolResult(success=False, content=str(e))


@_summarize("alpaca_place_order")
def _alpaca_place_order_summary(args: dict) -> str:
    sym = args.get("symbol", "?")
    side = args.get("side", "?")
    return f"alpaca_place_order({sym} {side})"


# ---------------------------------------------------------------------------
# alpaca_cancel_order — cancel an existing order
# ---------------------------------------------------------------------------

@_register("alpaca_cancel_order")
def _alpaca_cancel_order(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Cancel an order by ID. Use 'cancel_all=true' to cancel all open orders."""
    try:
        client = _get_client()
        cancel_all = str(args.get("cancel_all", "")).lower() == "true"
        order_id = args.get("order_id", "").strip()

        if cancel_all:
            client["trading"].cancel_orders()
            return ToolResult(success=True, content="All open orders cancelled.")
        elif order_id:
            client["trading"].cancel_order_by_id(order_id)
            return ToolResult(success=True, content=f"Order {order_id} cancelled.")
        else:
            return ToolResult(success=False, content="Provide 'order_id' or 'cancel_all=true'")
    except Exception as e:
        return ToolResult(success=False, content=str(e))


@_summarize("alpaca_cancel_order")
def _alpaca_cancel_order_summary(args: dict) -> str:
    if args.get("cancel_all"):
        return "alpaca_cancel_order(cancel_all=True)"
    return f"alpaca_cancel_order(order_id={args.get('order_id', '?')!r})"


# ---------------------------------------------------------------------------
# alpaca_bars — historical price data
# ---------------------------------------------------------------------------

@_register("alpaca_bars")
def _alpaca_bars(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Get historical price bars for a symbol. Returns O/H/L/C/V data.

    timeframe: 1min, 5min, 15min, 30min, 1hour, 1day (default: 1day)
    limit: max bars to return (default 20, max 200)
    start/end: optional ISO date strings (e.g. '2026-01-01')
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    try:
        client = _get_client()
        symbol = args.get("symbol", "").strip().upper()
        if not symbol:
            return ToolResult(success=False, content="Missing required parameter: 'symbol'")

        tf_map = {
            "1min": TimeFrame.Minute, "5min": TimeFrame(5, TimeFrame.Minute.unit),
            "15min": TimeFrame(15, TimeFrame.Minute.unit), "30min": TimeFrame(30, TimeFrame.Minute.unit),
            "1hour": TimeFrame.Hour, "1day": TimeFrame.Day,
        }
        timeframe = tf_map.get(args.get("timeframe", "1day").lower(), TimeFrame.Day)
        limit = min(int(args.get("limit", 20)), 200)

        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=timeframe,
            limit=limit,
            start=args.get("start"),
            end=args.get("end"),
        )
        bars_data = client["data"].get_stock_bars(req)

        result = []
        bars = bars_data.get(symbol, [])
        for b in bars:
            result.append({
                "time": str(b.timestamp),
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            })
        return ToolResult(success=True, content=json.dumps(result, indent=2))
    except Exception as e:
        return ToolResult(success=False, content=str(e))


@_summarize("alpaca_bars")
def _alpaca_bars_summary(args: dict) -> str:
    sym = args.get("symbol", "?")
    tf = args.get("timeframe", "1day")
    return f"alpaca_bars({sym}, {tf})"


# ---------------------------------------------------------------------------
# alpaca_asset — lookup asset info
# ---------------------------------------------------------------------------

@_register("alpaca_asset")
def _alpaca_asset(args: dict, _wg: WriteSafetyGate, _rg: ReadSafetyGate) -> ToolResult:
    """Look up an asset by symbol or asset_id. Returns asset details (name, class, status, etc.)."""
    try:
        client = _get_client()
        symbol = args.get("symbol", "").strip().upper()
        asset_id = args.get("asset_id", "").strip()

        if symbol:
            asset = client["trading"].get_asset(symbol)
        elif asset_id:
            asset = client["trading"].get_asset(asset_id)
        else:
            return ToolResult(success=False, content="Provide 'symbol' or 'asset_id'")

        return ToolResult(
            success=True,
            content=json.dumps({
                "id": asset.id,
                "class": asset.asset_class,
                "symbol": asset.symbol,
                "name": asset.name,
                "status": asset.status,
                "tradable": asset.tradable,
                "marginable": asset.marginable,
                "shortable": asset.shortable,
                "easy_to_borrow": asset.easy_to_borrow,
                "fractionable": asset.fractionable,
            }, indent=2),
        )
    except Exception as e:
        return ToolResult(success=False, content=str(e))


@_summarize("alpaca_asset")
def _alpaca_asset_summary(args: dict) -> str:
    return f"alpaca_asset({args.get('symbol') or args.get('asset_id') or '?'})"
