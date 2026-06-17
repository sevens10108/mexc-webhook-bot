from flask import Flask, request

app = Flask(__name__)

@app.route("/")
def home():
    return "TradingView Webhook Server Running"

@app.route("/webhook", methods=["POST"])
def webhook():
    print("HEADERS:", dict(request.headers))
    print("BODY:", request.get_data(as_text=True))
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
