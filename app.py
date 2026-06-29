from flask import Flask, request, jsonify
import json
import os
from datetime import datetime

app = Flask(__name__)

START_BALANCE = 1000.0
STATE_FILE = "paper_state.json"

POSITION_PCT = 1.0   # 1.0 = 100% баланса
LEVERAGE = 1.0       # пока без плеча для безопасного paper-теста
COMMISSION_PCT = 0.0004  # 0.04% как в TradingView


def empty_state():
    return {
        "balance": START_BALANCE,
        "position": None,
        "entry_price": None,
        "contracts": 0.0,
        "trades": []
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        return empty_state()

    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def to_float(value, default=0.0):
    try:
        return float(str(value).replace(",", "."))
    except Exception:
        return default


def get_price(data):
    for key in ["price", "close", "strategy.order.price"]:
        if key in data:
            return to_float(data[key], None)
    return None


def target_side_from_position(position_value):
    if position_value > 0:
        return "long"
    if position_value < 0:
        return "short"
    return None


def calc_contracts(balance, price):
    if price is None or price <= 0:
        return 0.0

    position_usdt = balance * POSITION_PCT * LEVERAGE
    return position_usdt / price


def calc_commission(price, contracts):
    return abs(price * contracts) * COMMISSION_PCT


def close_position(state, price, now, reason="close"):
    if state["position"] is None or state["entry_price"] is None:
        return 0.0

    side = state["position"]
    entry = float(state["entry_price"])
    contracts = abs(float(state["contracts"]))

    if side == "long":
        gross_pnl = (price - entry) * contracts
    else:
        gross_pnl = (entry - price) * contracts

    close_commission = calc_commission(price, contracts)
    net_pnl = gross_pnl - close_commission

    state["balance"] += net_pnl

    state["trades"].append({
        "time": now,
        "type": reason,
        "side": side,
        "entry_price": entry,
        "exit_price": price,
        "contracts": contracts,
        "gross_pnl": gross_pnl,
        "commission": close_commission,
        "net_pnl": net_pnl,
        "balance": state["balance"]
    })

    state["position"] = None
    state["entry_price"] = None
    state["contracts"] = 0.0

    return net_pnl


def open_position(state, side, price, now):
    contracts = calc_contracts(state["balance"], price)
    open_commission = calc_commission(price, contracts)

    state["balance"] -= open_commission
    state["position"] = side
    state["entry_price"] = price
    state["contracts"] = contracts

    state["trades"].append({
        "time": now,
        "type": "open",
        "side": side,
        "entry_price": price,
        "contracts": contracts,
        "position_usdt": price * contracts,
        "commission": open_commission,
        "balance": state["balance"]
    })

    return contracts, open_commission


@app.route("/", methods=["GET"])
def home():
    state = load_state()
    return jsonify({
        "status": "TradingView Webhook Server Running",
        "mode": "PAPER_TRADING_BALANCE_BASED",
        "start_balance": START_BALANCE,
        "position_pct": POSITION_PCT,
        "leverage": LEVERAGE,
        "commission_pct": COMMISSION_PCT,
        "balance": state["balance"],
        "position": state["position"],
        "entry_price": state["entry_price"],
        "contracts": state["contracts"],
        "trades_count": len(state["trades"])
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    print("=" * 70)
    print(f"NEW TRADINGVIEW SIGNAL: {now}")

    raw_body = request.get_data(as_text=True)
    print("RAW BODY:", raw_body)

    try:
        data = request.get_json(force=True)
    except Exception as e:
        print("JSON PARSE ERROR:", str(e))
        data = {}

    print("JSON DATA:", json.dumps(data, indent=2, ensure_ascii=False))

    action = str(data.get("action", "")).lower()
    ticker = str(data.get("ticker", "BTCUSDT"))

    tv_order_contracts = to_float(data.get("contracts", 0), 0.0)
    tv_target_position = to_float(data.get("position", 0), 0.0)

    target_side = target_side_from_position(tv_target_position)
    price = get_price(data)

    state = load_state()
    current_side = state["position"]

    print("ACTION:", action)
    print("TICKER:", ticker)
    print("TV ORDER CONTRACTS:", tv_order_contracts)
    print("TV TARGET POSITION:", tv_target_position)
    print("TARGET SIDE:", target_side)
    print("PRICE:", price)
    print("CURRENT BALANCE:", state["balance"])
    print("CURRENT POSITION:", current_side)
    print("CURRENT CONTRACTS:", state["contracts"])

    if price is None:
        print("NO PRICE IN SIGNAL — SIGNAL LOGGED ONLY")
        return jsonify({"status": "ok", "message": "no price", "received": data})

    if target_side == current_side:
        print("SAME TARGET SIDE — NO OPEN/CLOSE")
        print("TradingView position changed, but paper bot keeps its own balance-based size.")

    else:
        if current_side is not None:
            net_pnl = close_position(state, price, now, reason="reverse_or_close")
            print(f"CLOSED {current_side.upper()} | NET PNL: {net_pnl:.4f} USDT")

        if target_side is not None:
            contracts, commission = open_position(state, target_side, price, now)
            print(f"OPENED {target_side.upper()} | PRICE: {price} | CONTRACTS: {contracts:.8f} BTC | COMMISSION: {commission:.4f}")
        else:
            print("TARGET POSITION IS FLAT — POSITION CLOSED ONLY")

    save_state(state)

    print("NEW BALANCE:", state["balance"])
    print("NEW POSITION:", state["position"])
    print("NEW CONTRACTS:", state["contracts"])
    print("=" * 70)

    return jsonify({
        "status": "ok",
        "mode": "paper_trading_balance_based",
        "balance": state["balance"],
        "position": state["position"],
        "entry_price": state["entry_price"],
        "contracts": state["contracts"],
        "tv_target_position": tv_target_position,
        "received": data
    })


@app.route("/status", methods=["GET"])
def status():
    return jsonify(load_state())


@app.route("/reset", methods=["GET"])
def reset():
    state = empty_state()
    save_state(state)
    return jsonify({
        "status": "reset",
        "balance": START_BALANCE,
        "position": None,
        "contracts": 0.0,
        "trades": []
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
