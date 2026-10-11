import os, socket, time
from flask import Flask, jsonify, request

app = Flask(__name__)
POD = os.environ.get("HOSTNAME", socket.gethostname())

def burn(ms):
    end = time.perf_counter() + ms / 1000
    x = 0
    while time.perf_counter() < end:
        x += 1

@app.route("/")
def index():
    return jsonify(app="hpa-workload", pod=POD, hint="call /burn?ms=200 to use CPU")

@app.route("/burn")
def do_burn():
    ms = max(1, min(int(request.args.get("ms", 200)), 5000))
    burn(ms)
    return jsonify(pod=POD, burned_ms=ms)

@app.route("/healthz")
def health():
    return "ok"
