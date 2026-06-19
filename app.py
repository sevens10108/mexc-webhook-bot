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


def get_price(data):
    for key in ["price", "close", "strategy.order.price"]:
        if key in data:
            try:
                return float(str(data[key]).replace(",", "."))
            except:
                pass
    return None


def close_position(state, price, now):
    if state["position"] is None or state["entry_price"] is None:
        return 0.0

    entry = float(state["entry_price"])
    contracts = float(state["contracts"])

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
    state["contracts"] = contracts

    state["trades"].append({
        "time": now,
        "type": "open",
        "side": side,
        "entry_price": price,
        "contracts": contracts,
        "balance": state["balance"]
    })


@app.route("/", methods=["GET"])
def home():
    state = load_state()
    return jsonify({
        "status": "TradingView Webhook Server Running",
        "mode": "PAPER_TRADING",
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

    try:
        contracts = float(str(data.get("contracts", 1)).replace(",", "."))
    except:
        contracts = 1.0

    price = get_price(data)

    state = load_state()

    print("ACTION:", action)
    print("TICKER:", ticker)
    print("CONTRACTS:", contracts)
    print("PRICE:", price)
    print("CURRENT BALANCE:", state["balance"])
    print("CURRENT POSITION:", state["position"])

    if price is None:
        print("NO PRICE IN SIGNAL — SIGNAL LOGGED ONLY")
        save_state(state)
        return jsonify({
            "status": "ok",
            "mode": "paper_trading",
            "message": "signal received, but no price provided",
            "received": data
        })

    if action in ["buy", "long"]:
        if state["position"] == "short":
            pnl = close_position(state, price, now)
            print(f"CLOSED SHORT | PNL: {pnl:.4f} USDT")

        if state["position"] is None:
            open_position(state, "long", price, contracts, now)
            print(f"OPENED LONG | PRICE: {price} | CONTRACTS: {contracts}")
        else:
            print("LONG SIGNAL RECEIVED, BUT LONG ALREADY OPEN")

    elif action in ["sell", "short"]:
        if state["position"] == "long":
            pnl = close_position(state, price, now)
            print(f"CLOSED LONG | PNL: {pnl:.4f} USDT")

        if state["position"] is None:
            open_position(state, "short", price, contracts, now)
            print(f"OPENED SHORT | PRICE: {price} | CONTRACTS: {contracts}")
        else:
            print("SHORT SIGNAL RECEIVED, BUT SHORT ALREADY OPEN")

    elif action in ["close"]:
        if state["position"] is not None:
            pnl = close_position(state, price, now)
            print(f"CLOSED POSITION | PNL: {pnl:.4f} USDT")
        else:
            print("CLOSE SIGNAL RECEIVED, BUT NO POSITION OPEN")

    else:
        print("UNKNOWN SIGNAL — NO ACTION")

    save_state(state)

    print("NEW BALANCE:", state["balance"])
    print("NEW POSITION:", state["position"])
    print("=" * 60)

    return jsonify({
        "status": "ok",
        "mode": "paper_trading",
        "balance": state["balance"],
        "position": state["position"],
        "entry_price": state["entry_price"],
        "contracts": state["contracts"],
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
