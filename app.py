from flask import Flask, request, jsonify, Response
import json
import os
from datetime import datetime

app = Flask(__name__)

START_BALANCE = 1000.0
STATE_FILE = "paper_state.json"
EVENTS_FILE = "bot_events.jsonl"

COMMISSION_RATE = 0.0004
POSITION_PERCENT = 1.0


def now_utc():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


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


def save_event(event):
    with open(EVENTS_FILE, "a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


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

    trade = {
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
    }

    state["trades"].append(trade)

    state["position"] = None
    state["entry_price"] = None
    state["contracts"] = 0.0

    return trade


def open_position(state, side, price, now):
    contracts = calc_contracts(state["balance"], price)
    entry_commission = calc_commission(price, contracts)

    state["balance"] -= entry_commission
    state["position"] = side
    state["entry_price"] = price
    state["contracts"] = contracts

    trade = {
        "time": now,
        "type": "open",
        "side": side,
        "entry_price": price,
        "contracts": contracts,
        "entry_commission": entry_commission,
        "balance": state["balance"]
    }

    state["trades"].append(trade)

    return trade


@app.route("/", methods=["GET"])
def home():
    state = load_state()
    return jsonify({
        "status": "TradingView Webhook Server Running",
        "mode": "PAPER_TV_SIDE_SYNC_SAFE",
        "balance": state["balance"],
        "position": state["position"],
        "entry_price": state["entry_price"],
        "contracts": state["contracts"],
        "last_tv_position": state["last_tv_position"],
        "trades_count": len(state["trades"])
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    now = now_utc()
    raw_body = request.get_data(as_text=True)

    try:
        data = request.get_json(force=True)
    except:
        data = {}

    action = str(data.get("action", "")).lower()
    ticker = str(data.get("ticker", "BTCUSDT"))

    tv_order_contracts = to_float(data.get("contracts", 0), 0.0)
    tv_position = to_float(data.get("position", 0), 0.0)
    price = get_price(data)

    state = load_state()

    old_tv_position = to_float(state.get("last_tv_position", 0.0), 0.0)
    old_tv_side = side_from_position(old_tv_position)
    new_tv_side = side_from_position(tv_position)
    bot_side = state["position"]

    signal_key = f"{action}|{ticker}|{tv_order_contracts}|{tv_position}|{price}"

    event = {
        "time": now,
        "raw": data,
        "action": action,
        "ticker": ticker,
        "tv_order_contracts": tv_order_contracts,
        "old_tv_position": old_tv_position,
        "new_tv_position": tv_position,
        "old_tv_side": old_tv_side,
        "new_tv_side": new_tv_side,
        "bot_side_before": bot_side,
        "price": price,
        "result": None,
        "trades": []
    }

    print("=" * 70)
    print(f"NEW TRADINGVIEW SIGNAL: {now}")
    print("RAW BODY:", raw_body)
    print("ACTION:", action)
    print("TICKER:", ticker)
    print("TV ORDER CONTRACTS:", tv_order_contracts)
    print("OLD TV POSITION:", old_tv_position)
    print("NEW TV POSITION:", tv_position)
    print("OLD TV SIDE:", old_tv_side)
    print("NEW TV SIDE:", new_tv_side)
    print("BOT POSITION:", bot_side)
    print("PRICE:", price)
    print("BOT BALANCE:", state["balance"])

    if price is None:
        event["result"] = "ignored_no_price"
        save_event(event)
        return jsonify({"status": "ignored", "reason": "no_price"})

    if signal_key == state.get("last_signal_key"):
        event["result"] = "ignored_duplicate"
        save_event(event)
        return jsonify({"status": "ignored", "reason": "duplicate"})

    # Главное исправление:
    # если сторона TV не изменилась, позицию НЕ закрываем и НЕ открываем заново
    if old_tv_side == new_tv_side:
        state["last_tv_position"] = tv_position
        state["last_signal_key"] = signal_key
        save_state(state)

        event["result"] = "ignored_same_side"
        event["bot_side_after"] = state["position"]
        event["balance_after"] = state["balance"]
        save_event(event)

        print("SAME TV SIDE — IGNORED, NO CLOSE/OPEN")
        print("=" * 70)

        return jsonify({
            "status": "ignored",
            "reason": "same_side_no_reopen",
            "balance": state["balance"],
            "position": state["position"],
            "tv_position": tv_position
        })

    # Если сторона изменилась — закрываем старую позицию
    if state["position"] is not None:
        close_trade = close_position(state, price, now)
        event["trades"].append(close_trade)
        print(f"CLOSED {close_trade['side']}")
        print(f"GROSS PNL: {close_trade['gross_pnl']:.4f}")
        print(f"EXIT COMMISSION: {close_trade['exit_commission']:.4f}")
        print(f"NET PNL: {close_trade['net_pnl']:.4f}")

    # Если новая TV позиция не flat — открываем новую
    if new_tv_side is not None:
        open_trade = open_position(state, new_tv_side, price, now)
        event["trades"].append(open_trade)
        print(f"OPENED {new_tv_side}")
        print(f"PRICE: {price}")
        print(f"CONTRACTS: {open_trade['contracts']}")
        print(f"ENTRY COMMISSION: {open_trade['entry_commission']:.4f}")
    else:
        print("TV POSITION FLAT — POSITION CLOSED ONLY")

    state["last_tv_position"] = tv_position
    state["last_signal_key"] = signal_key

    save_state(state)

    event["result"] = "processed"
    event["bot_side_after"] = state["position"]
    event["balance_after"] = state["balance"]
    save_event(event)

    print("NEW BALANCE:", state["balance"])
    print("NEW POSITION:", state["position"])
    print("NEW CONTRACTS:", state["contracts"])
    print("=" * 70)

    return jsonify({
        "status": "ok",
        "mode": "paper_tv_side_sync_safe",
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


@app.route("/trades", methods=["GET"])
def trades():
    return jsonify(load_state().get("trades", []))


@app.route("/events", methods=["GET"])
def events():
    if not os.path.exists(EVENTS_FILE):
        return jsonify([])

    rows = []
    with open(EVENTS_FILE, "r") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except:
                pass

    return jsonify(rows[-500:])


@app.route("/download_events", methods=["GET"])
def download_events():
    if not os.path.exists(EVENTS_FILE):
        return Response("", mimetype="text/plain")

    with open(EVENTS_FILE, "r") as f:
        content = f.read()

    return Response(
        content,
        mimetype="text/plain",
        headers={"Content-Disposition": "attachment; filename=bot_events.jsonl"}
    )


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
