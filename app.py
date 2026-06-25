from flask import Flask, request, jsonify
import json
import os
from datetime import datetime

app = Flask(__name__)

START_BALANCE = 1000.0
STATE_FILE = "paper_state.json"


def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "balance": START_BALANCE,
            "position": None,
            "entry_price": None,
            "contracts": 0.0,
            "trades": []
        }

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


def close_position(state, price, now):
    if state["position"] is None or state["entry_price"] is None:
        return 0.0

    entry = float(state["entry_price"])
    contracts = abs(float(state["contracts"]))

    if state["position"] == "long":
        pnl = (price - entry) * contracts
    else:
        pnl = (entry - price) * contracts

    state["balance"] += pnl

    state["trades"].append({
        "time": now,
        "type": "close",
        "side": state["position"],
        "entry_price": entry,
        "exit_price": price,
        "contracts": contracts,
        "pnl": pnl,
        "balance": state["balance"]
    })

    state["position"] = None
    state["entry_price"] = None
    state["contracts"] = 0.0

    return pnl


def open_position(state, side, price, contracts, now):
    state["position"] = side
    state["entry_price"] = price
    state["contracts"] = abs(contracts)

    state["trades"].append({
        "time": now,
        "type": "open",
        "side": side,
        "entry_price": price,
        "contracts": abs(contracts),
        "balance": state["balance"]
    })


def target_side_from_position(position_value):
    if position_value > 0:
        return "long"
    if position_value < 0:
        return "short"
    return None


@app.route("/", methods=["GET"])
def home():
    state = load_state()
    return jsonify({
        "status": "TradingView Webhook Server Running",
        "mode": "PAPER_TRADING_POSITION_BASED",
        "balance": state["balance"],
        "position": state["position"],
        "entry_price": state["entry_price"],
        "contracts": state["contracts"],
        "trades_count": len(state["trades"])
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    print("=" * 60)
    print(f"NEW TRADINGVIEW SIGNAL: {now}")
    print("HEADERS:", dict(request.headers))

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
    order_contracts = to_float(data.get("contracts", 0), 0.0)
    target_position = to_float(data.get("position", 0), 0.0)
    target_side = target_side_from_position(target_position)
    target_contracts = abs(target_position)

    price = get_price(data)
    state = load_state()

    print("ACTION:", action)
    print("TICKER:", ticker)
    print("ORDER CONTRACTS:", order_contracts)
    print("TARGET POSITION:", target_position)
    print("TARGET SIDE:", target_side)
    print("TARGET CONTRACTS:", target_contracts)
    print("PRICE:", price)
    print("CURRENT BALANCE:", state["balance"])
    print("CURRENT POSITION:", state["position"])
    print("CURRENT CONTRACTS:", state["contracts"])

    if price is None:
        print("NO PRICE IN SIGNAL — SIGNAL LOGGED ONLY")
        save_state(state)
        return jsonify({"status": "ok", "message": "no price", "received": data})

    current_side = state["position"]

    if target_side == current_side:
        state["contracts"] = target_contracts
        print("SAME TARGET SIDE — NO OPEN/CLOSE, CONTRACTS UPDATED ONLY")

    else:
        if current_side is not None:
            pnl = close_position(state, price, now)
            print(f"CLOSED {current_side.upper()} | PNL: {pnl:.4f} USDT")

        if target_side is not None and target_contracts > 0:
            open_position(state, target_side, price, target_contracts, now)
            print(f"OPENED {target_side.upper()} | PRICE: {price} | CONTRACTS: {target_contracts}")
        else:
            print("TARGET POSITION IS FLAT — NO NEW POSITION OPENED")

    save_state(state)

    print("NEW BALANCE:", state["balance"])
    print("NEW POSITION:", state["position"])
    print("NEW CONTRACTS:", state["contracts"])
    print("=" * 60)

    return jsonify({
        "status": "ok",
        "mode": "paper_trading_position_based",
        "balance": state["balance"],
        "position": state["position"],
        "entry_price": state["entry_price"],
        "contracts": state["contracts"],
        "target_position": target_position,
        "received": data
    })


@app.route("/status", methods=["GET"])
def status():
    state = load_state()
    return jsonify(state)


@app.route("/reset", methods=["GET"])
def reset():
    state = {
        "balance": START_BALANCE,
        "position": None,
        "entry_price": None,
        "contracts": 0.0,
        "trades": []
    }
    save_state(state)
    return jsonify({
        "status": "reset",
        "balance": START_BALANCE
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
