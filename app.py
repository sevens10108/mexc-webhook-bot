from flask import Flask, request, jsonify
import json
import os
from datetime import datetime

app = Flask(__name__)

START_BALANCE = 1000.0
STATE_FILE = "paper_state.json"

COMMISSION_RATE = 0.0004  # 0.04%
POSITION_PERCENT = 1.0    # 100% баланса в сделку


def default_state():
    return {
        "balance": START_BALANCE,
        "position": None,
        "entry_price": None,
        "contracts": 0.0,   # BTC amount
        "trades": []
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()

    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def to_float(value, default=0.0):
    try:
        return float(str(value).replace(",", "."))
    except:
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
    position_usdt = balance * POSITION_PERCENT
    return position_usdt / price


def calc_commission(price, contracts):
    return price * contracts * COMMISSION_RATE


def close_position(state, price, now):
    if state["position"] is None or state["entry_price"] is None:
        return 0.0, 0.0, 0.0

    side = state["position"]
    entry = float(state["entry_price"])
    contracts = abs(float(state["contracts"]))

    if side == "long":
        gross_pnl = (price - entry) * contracts
    else:
        gross_pnl = (entry - price) * contracts

    exit_commission = calc_commission(price, contracts)
    net_pnl = gross_pnl - exit_commission

    state["balance"] += net_pnl

    state["trades"].append({
        "time": now,
        "type": "close",
        "side": side,
        "entry_price": entry,
        "exit_price": price,
        "contracts": contracts,
        "gross_pnl": gross_pnl,
        "exit_commission": exit_commission,
        "net_pnl": net_pnl,
        "balance": state["balance"]
    })

    state["position"] = None
    state["entry_price"] = None
    state["contracts"] = 0.0

    return gross_pnl, exit_commission, net_pnl


def open_position(state, side, price, now):
    contracts = calc_contracts(state["balance"], price)
    entry_commission = calc_commission(price, contracts)

    state["balance"] -= entry_commission

    state["position"] = side
    state["entry_price"] = price
    state["contracts"] = contracts

    state["trades"].append({
        "time": now,
        "type": "open",
        "side": side,
        "entry_price": price,
        "contracts": contracts,
        "entry_commission": entry_commission,
        "balance": state["balance"]
    })

    return contracts, entry_commission


@app.route("/", methods=["GET"])
def home():
    state = load_state()
    return jsonify({
        "status": "TradingView Webhook Server Running",
        "mode": "PAPER_TRADING_REALISTIC_PNL",
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

    print("ACTION:", action)
    print("TICKER:", ticker)
    print("TV ORDER CONTRACTS:", tv_order_contracts)
    print("TV TARGET POSITION:", tv_target_position)
    print("TARGET SIDE:", target_side)
    print("PRICE:", price)
    print("CURRENT BALANCE:", state["balance"])
    print("CURRENT POSITION:", state["position"])
    print("CURRENT CONTRACTS:", state["contracts"])

    if price is None:
        print("NO PRICE — SIGNAL LOGGED ONLY")
        return jsonify({"status": "ok", "message": "no price", "received": data})

    current_side = state["position"]

    if target_side == current_side:
        print("SAME TARGET SIDE — NO OPEN/CLOSE")
        print("Paper bot keeps own balance-based position size.")

    else:
        if current_side is not None:
            gross_pnl, exit_commission, net_pnl = close_position(state, price, now)
            print(f"CLOSED {current_side.upper()}")
            print(f"GROSS PNL: {gross_pnl:.4f} USDT")
            print(f"EXIT COMMISSION: {exit_commission:.4f} USDT")
            print(f"NET PNL: {net_pnl:.4f} USDT")

        if target_side is not None:
            contracts, entry_commission = open_position(state, target_side, price, now)
            print(f"OPENED {target_side.upper()}")
            print(f"PRICE: {price}")
            print(f"CONTRACTS: {contracts} BTC")
            print(f"ENTRY COMMISSION: {entry_commission:.4f} USDT")
        else:
            print("TARGET POSITION IS FLAT — POSITION CLOSED ONLY")

    save_state(state)

    print("NEW BALANCE:", state["balance"])
    print("NEW POSITION:", state["position"])
    print("NEW CONTRACTS:", state["contracts"])
    print("=" * 70)

    return jsonify({
        "status": "ok",
        "mode": "paper_trading_realistic_pnl",
        "balance": state["balance"],
        "position": state["position"],
        "entry_price": state["entry_price"],
        "contracts": state["contracts"],
        "received": data
    })


@app.route("/status", methods=["GET"])
def status():
    return jsonify(load_state())


@app.route("/reset", methods=["GET"])
def reset():
    state = default_state()
    save_state(state)
    return jsonify({
        "status": "reset",
        "balance": START_BALANCE,
        "position": None,
        "contracts": 0.0
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
