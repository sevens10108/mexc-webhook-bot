from flask import Flask, request, jsonify
import json
import os
from datetime import datetime

app = Flask(__name__)

START_BALANCE = 1000.0
STATE_FILE = "paper_state.json"

COMMISSION_RATE = 0.0004   # 0.04%
POSITION_PERCENT = 1.0     # 100% баланса


def default_state():
    return {
        "balance": START_BALANCE,
        "position": None,
        "entry_price": None,
        "contracts": 0.0,
        "last_tv_position": 0.0,
        "last_signal_key": None,
        "trades": []
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()

    with open(STATE_FILE, "r") as f:
        state = json.load(f)

    base = default_state()
    for k, v in base.items():
        if k not in state:
            state[k] = v

    return state


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


def side_from_position(pos):
    if pos > 0:
        return "long"
    if pos < 0:
        return "short"
    return None


def calc_contracts(balance, price):
    return (balance * POSITION_PERCENT) / price


def calc_commission(price, contracts):
    return price * contracts * COMMISSION_RATE


def close_position(state, price, now):
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
        "mode": "PAPER_TV_POSITION_SYNC",
        "balance": state["balance"],
        "position": state["position"],
        "entry_price": state["entry_price"],
        "contracts": state["contracts"],
        "last_tv_position": state["last_tv_position"],
        "trades_count": len(state["trades"])
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    raw_body = request.get_data(as_text=True)

    try:
        data = request.get_json(force=True)
    except Exception as e:
        data = {}

    action = str(data.get("action", "")).lower()
    ticker = str(data.get("ticker", "BTCUSDT"))

    tv_order_contracts = to_float(data.get("contracts", 0), 0.0)
    tv_position = to_float(data.get("position", 0), 0.0)
    price = get_price(data)

    state = load_state()

    signal_key = f"{action}|{ticker}|{tv_order_contracts}|{tv_position}|{price}"

    print("=" * 70)
    print(f"NEW TRADINGVIEW SIGNAL: {now}")
    print("RAW BODY:", raw_body)
    print("ACTION:", action)
    print("TICKER:", ticker)
    print("TV ORDER CONTRACTS:", tv_order_contracts)
    print("TV POSITION:", tv_position)
    print("PRICE:", price)
    print("BOT BALANCE:", state["balance"])
    print("BOT POSITION:", state["position"])
    print("LAST TV POSITION:", state["last_tv_position"])

    if price is None:
        print("NO PRICE — IGNORED")
        return jsonify({"status": "ignored", "reason": "no_price"})

    if signal_key == state.get("last_signal_key"):
        print("DUPLICATE SIGNAL — IGNORED")
        return jsonify({"status": "ignored", "reason": "duplicate"})

    old_tv_position = to_float(state.get("last_tv_position", 0.0), 0.0)

    old_side = side_from_position(old_tv_position)
    new_side = side_from_position(tv_position)

    if old_tv_position == tv_position:
        print("TV POSITION DID NOT CHANGE — IGNORED")
        state["last_signal_key"] = signal_key
        save_state(state)
        return jsonify({"status": "ignored", "reason": "tv_position_not_changed"})

    if state["position"] is not None:
        gross_pnl, exit_commission, net_pnl = close_position(state, price, now)
        print(f"CLOSED {old_side}")
        print(f"GROSS PNL: {gross_pnl:.4f}")
        print(f"EXIT COMMISSION: {exit_commission:.4f}")
        print(f"NET PNL: {net_pnl:.4f}")

    if new_side is not None:
        contracts, entry_commission = open_position(state, new_side, price, now)
        print(f"OPENED {new_side}")
        print(f"PRICE: {price}")
        print(f"CONTRACTS: {contracts}")
        print(f"ENTRY COMMISSION: {entry_commission:.4f}")
    else:
        print("TV POSITION FLAT — POSITION CLOSED ONLY")

    state["last_tv_position"] = tv_position
    state["last_signal_key"] = signal_key

    save_state(state)

    print("NEW BALANCE:", state["balance"])
    print("NEW POSITION:", state["position"])
    print("NEW CONTRACTS:", state["contracts"])
    print("=" * 70)

    return jsonify({
        "status": "ok",
        "mode": "paper_tv_position_sync",
        "balance": state["balance"],
        "position": state["position"],
        "entry_price": state["entry_price"],
        "contracts": state["contracts"],
        "tv_position": tv_position,
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
        "contracts": 0.0,
        "last_tv_position": 0.0
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
