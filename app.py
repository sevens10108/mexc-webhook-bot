from flask import Flask, request, jsonify
import json
import datetime

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "TradingView Webhook Server Running"

@app.route("/webhook", methods=["POST"])
def webhook():
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    print("=" * 50)
    print(f"NEW TRADINGVIEW SIGNAL: {now}")
    print("HEADERS:", dict(request.headers))

    raw_body = request.get_data(as_text=True)
    print("RAW BODY:", raw_body)

    try:
        data = request.get_json(force=True)
        print("JSON DATA:", json.dumps(data, indent=2, ensure_ascii=False))
    except Exception as e:
        print("JSON PARSE ERROR:", str(e))
        data = {"raw": raw_body}

    action = str(data.get("action", "")).lower()
    ticker = data.get("ticker", "")
    contracts = data.get("contracts", "")
    position = data.get("position", "")

    print("ACTION:", action)
    print("TICKER:", ticker)
    print("CONTRACTS:", contracts)
    print("POSITION:", position)

    if action in ["buy", "long"]:
        print("DEMO MODE: LONG SIGNAL RECEIVED — NO ORDER SENT")
    elif action in ["sell", "short"]:
        print("DEMO MODE: SHORT/SELL SIGNAL RECEIVED — NO ORDER SENT")
    elif action in ["close"]:
        print("DEMO MODE: CLOSE SIGNAL RECEIVED — NO ORDER SENT")
    else:
        print("DEMO MODE: UNKNOWN SIGNAL — NO ORDER SENT")

    print("=" * 50)

    return jsonify({
        "status": "ok",
        "mode": "demo_log_only",
        "received": data
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
