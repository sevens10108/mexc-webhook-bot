from __future__ import annotations

from flask import Flask, jsonify, request, send_file
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Optional
import hashlib
import json
import os
import tempfile

app = Flask(__name__)

START_BALANCE = float(os.getenv("START_BALANCE", "1000"))
POSITION_PERCENT = float(os.getenv("POSITION_PERCENT", "1.0"))
LEVERAGE = float(os.getenv("LEVERAGE", "1.0"))

# Defaults: MEXC upper fee rates with 20% MX discount:
# maker 0.040% * 0.80 = 0.032%; taker 0.100% * 0.80 = 0.080%.
MAKER_FEE_RATE = float(os.getenv("MAKER_FEE_RATE", "0.00032"))
TAKER_FEE_RATE = float(os.getenv("TAKER_FEE_RATE", "0.00080"))
ENTRY_FEE_TYPE = os.getenv("ENTRY_FEE_TYPE", "taker").strip().lower()
EXIT_FEE_TYPE = os.getenv("EXIT_FEE_TYPE", "taker").strip().lower()
SIGNAL_CACHE_LIMIT = int(os.getenv("SIGNAL_CACHE_LIMIT", "500"))

# Mount a Railway Volume at /data and set DATA_DIR=/data for persistence.
DATA_DIR = Path(os.getenv("DATA_DIR", ".")).resolve()
STATE_FILE = DATA_DIR / "paper_state.json"
EVENTS_FILE = DATA_DIR / "events.jsonl"
LOCK = RLock()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def to_float(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        if value is None:
            return default
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def side_from_position(position: float) -> Optional[str]:
    if abs(position) < 1e-12:
        return None
    return "long" if position > 0 else "short"


def get_price(data: dict[str, Any]) -> Optional[float]:
    for key in ("price", "close", "strategy.order.price"):
        if key in data:
            price = to_float(data.get(key), None)
            if price is not None and price > 0:
                return price
    return None


def fee_rate(fee_type: str) -> float:
    return MAKER_FEE_RATE if fee_type == "maker" else TAKER_FEE_RATE


def calculate_fee(price: float, contracts: float, fee_type: str) -> float:
    return abs(price * contracts) * fee_rate(fee_type)


def calculate_contracts(balance: float, price: float) -> float:
    if price <= 0:
        raise ValueError("Price must be positive")
    notional = max(balance, 0.0) * POSITION_PERCENT * LEVERAGE
    return notional / price


def default_state() -> dict[str, Any]:
    now = utc_now()
    return {
        "version": 3,
        "balance": START_BALANCE,
        "position": None,
        "entry_price": None,
        "contracts": 0.0,
        "entry_fee": 0.0,
        "entry_fee_type": None,
        "last_tv_position": 0.0,
        "last_signal_key": None,
        "recent_signal_keys": [],
        "trades": [],
        "created_at": now,
        "updated_at": now,
    }


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def migrate_state(state: dict[str, Any]) -> dict[str, Any]:
    base = default_state()
    for key, value in base.items():
        state.setdefault(key, value)
    state["balance"] = float(to_float(state.get("balance"), START_BALANCE) or START_BALANCE)
    state["contracts"] = abs(float(to_float(state.get("contracts"), 0.0) or 0.0))
    state["entry_fee"] = float(to_float(state.get("entry_fee"), 0.0) or 0.0)
    state["last_tv_position"] = float(to_float(state.get("last_tv_position"), 0.0) or 0.0)
    if state.get("position") not in {None, "long", "short"}:
        state["position"] = None
    if not isinstance(state.get("trades"), list):
        state["trades"] = []
    if not isinstance(state.get("recent_signal_keys"), list):
        state["recent_signal_keys"] = []
    return state


def load_state() -> dict[str, Any]:
    ensure_data_dir()
    if not STATE_FILE.exists():
        return default_state()
    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            state = json.load(file)
        if not isinstance(state, dict):
            raise ValueError("State root must be an object")
        return migrate_state(state)
    except Exception as exc:
        backup = STATE_FILE.with_name(
            f"paper_state.corrupt.{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        )
        try:
            STATE_FILE.replace(backup)
        except OSError:
            pass
        print(f"STATE LOAD ERROR: {exc}", flush=True)
        return default_state()


def save_state(state: dict[str, Any]) -> None:
    ensure_data_dir()
    state["updated_at"] = utc_now()
    fd, temp_name = tempfile.mkstemp(dir=str(DATA_DIR), prefix="paper_state_", suffix=".tmp", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, STATE_FILE)
    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)


def append_event(event_type: str, payload: dict[str, Any]) -> None:
    ensure_data_dir()
    event = {"time": utc_now(), "event": event_type, **payload}
    with EVENTS_FILE.open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False) + "\n")


def make_signal_key(data: dict[str, Any]) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def remember_signal(state: dict[str, Any], key: str) -> None:
    keys = state.setdefault("recent_signal_keys", [])
    keys.append(key)
    if len(keys) > SIGNAL_CACHE_LIMIT:
        del keys[:-SIGNAL_CACHE_LIMIT]
    state["last_signal_key"] = key


def open_position(state: dict[str, Any], side: str, price: float, now: str, source: dict[str, Any]) -> dict[str, float]:
    contracts = calculate_contracts(state["balance"], price)
    entry_fee = calculate_fee(price, contracts, ENTRY_FEE_TYPE)
    state["balance"] -= entry_fee
    state["position"] = side
    state["entry_price"] = price
    state["contracts"] = contracts
    state["entry_fee"] = entry_fee
    state["entry_fee_type"] = ENTRY_FEE_TYPE
    trade = {
        "time": now,
        "type": "open",
        "side": side,
        "entry_price": price,
        "contracts": contracts,
        "notional": price * contracts,
        "leverage": LEVERAGE,
        "entry_fee_type": ENTRY_FEE_TYPE,
        "entry_fee_rate": fee_rate(ENTRY_FEE_TYPE),
        "entry_commission": entry_fee,
        "balance": state["balance"],
        "tv_position": to_float(source.get("position"), 0.0),
        "tv_order_contracts": to_float(source.get("contracts"), 0.0),
    }
    state["trades"].append(trade)
    append_event("position_opened", trade)
    return {"contracts": contracts, "entry_fee": entry_fee}


def close_position(state: dict[str, Any], price: float, now: str, source: dict[str, Any]) -> dict[str, float]:
    side = state.get("position")
    entry_price = to_float(state.get("entry_price"), None)
    contracts = abs(float(to_float(state.get("contracts"), 0.0) or 0.0))
    if side not in {"long", "short"} or entry_price is None or contracts <= 0:
        return {"gross_pnl": 0.0, "exit_fee": 0.0, "net_close_pnl": 0.0, "total_trade_pnl": 0.0}
    gross_pnl = (price - entry_price) * contracts if side == "long" else (entry_price - price) * contracts
    exit_fee = calculate_fee(price, contracts, EXIT_FEE_TYPE)
    net_close_pnl = gross_pnl - exit_fee
    entry_fee = float(to_float(state.get("entry_fee"), 0.0) or 0.0)
    total_trade_pnl = gross_pnl - entry_fee - exit_fee
    state["balance"] += net_close_pnl
    trade = {
        "time": now,
        "type": "close",
        "side": side,
        "entry_price": entry_price,
        "exit_price": price,
        "contracts": contracts,
        "gross_pnl": gross_pnl,
        "entry_fee": entry_fee,
        "entry_fee_type": state.get("entry_fee_type"),
        "exit_fee_type": EXIT_FEE_TYPE,
        "exit_fee_rate": fee_rate(EXIT_FEE_TYPE),
        "exit_commission": exit_fee,
        "net_close_pnl": net_close_pnl,
        "total_trade_pnl": total_trade_pnl,
        "balance": state["balance"],
        "tv_position": to_float(source.get("position"), 0.0),
        "tv_order_contracts": to_float(source.get("contracts"), 0.0),
    }
    state["trades"].append(trade)
    append_event("position_closed", trade)
    state["position"] = None
    state["entry_price"] = None
    state["contracts"] = 0.0
    state["entry_fee"] = 0.0
    state["entry_fee_type"] = None
    return {
        "gross_pnl": gross_pnl,
        "exit_fee": exit_fee,
        "net_close_pnl": net_close_pnl,
        "total_trade_pnl": total_trade_pnl,
    }


@app.route("/", methods=["GET"])
def home():
    with LOCK:
        state = load_state()
    return jsonify({
        "status": "TradingView Webhook Server Running",
        "mode": "PAPER_TV_SIDE_SYNC_MEXC_FEES",
        "balance": state["balance"],
        "position": state["position"],
        "entry_price": state["entry_price"],
        "contracts": state["contracts"],
        "last_tv_position": state["last_tv_position"],
        "trades_count": len(state["trades"]),
        "maker_fee_rate": MAKER_FEE_RATE,
        "taker_fee_rate": TAKER_FEE_RATE,
        "entry_fee_type": ENTRY_FEE_TYPE,
        "exit_fee_type": EXIT_FEE_TYPE,
        "leverage": LEVERAGE,
        "position_percent": POSITION_PERCENT,
        "data_dir": str(DATA_DIR),
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    now = utc_now()
    raw_body = request.get_data(as_text=True)
    try:
        data = request.get_json(force=True, silent=False)
        if not isinstance(data, dict):
            raise ValueError("JSON root must be an object")
    except Exception as exc:
        append_event("invalid_json", {"error": str(exc), "raw_body": raw_body[:5000]})
        return jsonify({"status": "error", "reason": "invalid_json"}), 400

    action = str(data.get("action", "")).strip().lower()
    ticker = str(data.get("ticker", "BTCUSDT")).strip().upper()
    tv_order_contracts = abs(float(to_float(data.get("contracts"), 0.0) or 0.0))
    tv_position = float(to_float(data.get("position"), 0.0) or 0.0)
    price = get_price(data)
    key = make_signal_key(data)

    with LOCK:
        state = load_state()
        print("=" * 78, flush=True)
        print(f"NEW TRADINGVIEW SIGNAL: {now}", flush=True)
        print(f"RAW BODY: {raw_body}", flush=True)
        print(f"ACTION: {action}", flush=True)
        print(f"TICKER: {ticker}", flush=True)
        print(f"TV ORDER CONTRACTS: {tv_order_contracts}", flush=True)
        print(f"TV POSITION: {tv_position}", flush=True)
        print(f"PRICE: {price}", flush=True)
        print(f"BOT BALANCE: {state['balance']}", flush=True)
        print(f"BOT POSITION: {state['position']}", flush=True)
        print(f"LAST TV POSITION: {state['last_tv_position']}", flush=True)

        append_event("webhook_received", {
            "signal_key": key,
            "action": action,
            "ticker": ticker,
            "tv_order_contracts": tv_order_contracts,
            "tv_position": tv_position,
            "price": price,
            "raw": data,
        })

        if price is None:
            remember_signal(state, key)
            save_state(state)
            return jsonify({"status": "ignored", "reason": "no_valid_price"}), 200

        if key in state.get("recent_signal_keys", []):
            append_event("webhook_ignored", {"reason": "duplicate", "signal_key": key})
            return jsonify({"status": "ignored", "reason": "duplicate"}), 200

        old_tv_position = float(to_float(state.get("last_tv_position"), 0.0) or 0.0)
        old_tv_side = side_from_position(old_tv_position)
        new_tv_side = side_from_position(tv_position)
        bot_side = state.get("position")

        # Critical fix: quantity changes inside the same side are not new trades.
        if old_tv_side == new_tv_side:
            state["last_tv_position"] = tv_position
            remember_signal(state, key)
            save_state(state)
            reason = "tv_position_unchanged" if old_tv_position == tv_position else "same_side_size_update"
            append_event("webhook_ignored", {
                "reason": reason,
                "old_tv_position": old_tv_position,
                "new_tv_position": tv_position,
                "side": new_tv_side,
            })
            return jsonify({
                "status": "ignored",
                "reason": reason,
                "balance": state["balance"],
                "position": state["position"],
                "entry_price": state["entry_price"],
                "contracts": state["contracts"],
                "tv_position": tv_position,
            }), 200

        result: dict[str, Any] = {"status": "ok", "mode": "paper_tv_side_sync_mexc_fees"}

        if bot_side is not None:
            close_result = close_position(state, price, now, data)
            result["closed"] = close_result
            print(f"CLOSED {bot_side.upper()}", flush=True)
            print(f"GROSS PNL: {close_result['gross_pnl']:.8f}", flush=True)
            print(f"EXIT COMMISSION: {close_result['exit_fee']:.8f}", flush=True)
            print(f"TOTAL TRADE PNL: {close_result['total_trade_pnl']:.8f}", flush=True)

        if new_tv_side is not None:
            open_result = open_position(state, new_tv_side, price, now, data)
            result["opened"] = open_result
            result["action_taken"] = f"opened_{new_tv_side}"
            print(f"OPENED {new_tv_side.upper()}", flush=True)
            print(f"ENTRY COMMISSION: {open_result['entry_fee']:.8f}", flush=True)
        else:
            result["action_taken"] = "closed_to_flat"
            print("TV POSITION FLAT — POSITION CLOSED ONLY", flush=True)

        state["last_tv_position"] = tv_position
        remember_signal(state, key)
        save_state(state)

        result.update({
            "balance": state["balance"],
            "position": state["position"],
            "entry_price": state["entry_price"],
            "contracts": state["contracts"],
            "last_tv_position": state["last_tv_position"],
            "tv_position": tv_position,
            "received": data,
        })
        print(f"NEW BALANCE: {state['balance']}", flush=True)
        print(f"NEW POSITION: {state['position']}", flush=True)
        print("=" * 78, flush=True)
        return jsonify(result), 200


@app.route("/status", methods=["GET"])
def status():
    with LOCK:
        state = load_state()
    return jsonify({
        **state,
        "configuration": {
            "start_balance": START_BALANCE,
            "position_percent": POSITION_PERCENT,
            "leverage": LEVERAGE,
            "maker_fee_rate": MAKER_FEE_RATE,
            "taker_fee_rate": TAKER_FEE_RATE,
            "entry_fee_type": ENTRY_FEE_TYPE,
            "exit_fee_type": EXIT_FEE_TYPE,
            "data_dir": str(DATA_DIR),
        },
    })


@app.route("/trades", methods=["GET"])
def trades():
    with LOCK:
        return jsonify(load_state().get("trades", []))


@app.route("/events", methods=["GET"])
def events():
    ensure_data_dir()
    limit = int(to_float(request.args.get("limit"), 500) or 500)
    limit = min(max(limit, 1), 5000)
    if not EVENTS_FILE.exists():
        return jsonify([])
    with EVENTS_FILE.open("r", encoding="utf-8") as file:
        lines = file.readlines()[-limit:]
    result = []
    for line in lines:
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            result.append({"event": "unparsed_line", "raw": line.rstrip("\n")})
    return jsonify(result)


@app.route("/download_events", methods=["GET"])
def download_events():
    ensure_data_dir()
    if not EVENTS_FILE.exists():
        EVENTS_FILE.touch()
    return send_file(EVENTS_FILE, as_attachment=True, download_name="mexc_webhook_events.jsonl", mimetype="application/x-ndjson")


@app.route("/download_state", methods=["GET"])
def download_state():
    with LOCK:
        state = load_state()
        save_state(state)
    return send_file(STATE_FILE, as_attachment=True, download_name="paper_state.json", mimetype="application/json")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "time": utc_now()})


@app.route("/reset", methods=["GET", "POST"])
def reset():
    configured_token = os.getenv("RESET_TOKEN", "")
    supplied_token = request.args.get("token") or request.headers.get("X-Reset-Token") or ""
    if configured_token and supplied_token != configured_token:
        return jsonify({"status": "error", "reason": "invalid_reset_token"}), 403
    with LOCK:
        old_state = load_state()
        append_event("state_reset", {
            "old_balance": old_state.get("balance"),
            "old_position": old_state.get("position"),
            "old_trades_count": len(old_state.get("trades", [])),
        })
        state = default_state()
        save_state(state)
    return jsonify({
        "status": "reset",
        "balance": state["balance"],
        "position": state["position"],
        "contracts": state["contracts"],
        "last_tv_position": state["last_tv_position"],
    })


@app.errorhandler(Exception)
def handle_unexpected_error(exc: Exception):
    print(f"UNEXPECTED ERROR: {exc}", flush=True)
    try:
        append_event("server_error", {"error": str(exc), "path": request.path, "method": request.method})
    except Exception:
        pass
    return jsonify({"status": "error", "reason": "internal_server_error"}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
