"""Autoscale tester: generates load against a Service from inside the cluster
and shows live HPA / pod / CPU data read from the Kubernetes API."""
import collections
import os
import threading
import time

import requests
from flask import Flask, jsonify, request

TARGET = os.environ.get("TARGET_URL", "http://hpa-demo").rstrip("/")
NS = os.environ.get("NAMESPACE", "default")
HPA = os.environ.get("HPA_NAME", "hpa-demo")
LABEL = os.environ.get("POD_LABEL", "app=hpa-demo")
API = os.environ.get("K8S_API", "https://kubernetes.default.svc")
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
MAX_USERS = int(os.environ.get("MAX_USERS", "150"))

# (name, users, cpu ms per request, seconds)
STAGES = [
    ("Warm up", 5, 150, 60),
    ("Ramp up", 15, 300, 90),
    ("Peak", 40, 300, 120),
    ("Cool down", 0, 0, 180),
]

app = Flask(__name__, static_folder="static")


def k8s_get(path, params=None):
    headers, verify = {}, True
    try:
        with open(f"{SA}/token") as f:
            headers["Authorization"] = "Bearer " + f.read().strip()
        verify = f"{SA}/ca.crt"
    except FileNotFoundError:
        pass
    r = requests.get(API + path, headers=headers, params=params, verify=verify, timeout=5)
    r.raise_for_status()
    return r.json()


def cpu_m(q):
    q = str(q)
    if q.endswith("n"):
        return int(q[:-1]) / 1e6
    if q.endswith("u"):
        return int(q[:-1]) / 1e3
    if q.endswith("m"):
        return float(q[:-1])
    return float(q) * 1000


def cluster_status():
    hpa = k8s_get(f"/apis/autoscaling/v2/namespaces/{NS}/horizontalpodautoscalers/{HPA}")
    spec, st = hpa.get("spec", {}), hpa.get("status", {})
    target = None
    for m in spec.get("metrics") or []:
        if m.get("type") == "Resource" and m["resource"]["name"] == "cpu":
            target = m["resource"]["target"].get("averageUtilization")
    cpu = None
    for m in st.get("currentMetrics") or []:
        if m.get("type") == "Resource" and m["resource"]["name"] == "cpu":
            cpu = m["resource"]["current"].get("averageUtilization")

    usage = {}
    try:
        mt = k8s_get(f"/apis/metrics.k8s.io/v1beta1/namespaces/{NS}/pods", {"labelSelector": LABEL})
        for it in mt.get("items", []):
            usage[it["metadata"]["name"]] = sum(cpu_m(c["usage"]["cpu"]) for c in it.get("containers", []))
    except Exception:
        pass

    pods = []
    for p in k8s_get(f"/api/v1/namespaces/{NS}/pods", {"labelSelector": LABEL}).get("items", []):
        name = p["metadata"]["name"]
        cs = p.get("status", {}).get("containerStatuses") or []
        ready = bool(cs) and all(c.get("ready") for c in cs)
        status = "Terminating" if p["metadata"].get("deletionTimestamp") else p.get("status", {}).get("phase", "?")
        pods.append({"name": name, "status": status, "ready": ready, "cpu_m": usage.get(name)})
    pods.sort(key=lambda x: x["name"])
    return {
        "min": spec.get("minReplicas", 1), "max": spec.get("maxReplicas"),
        "current": st.get("currentReplicas", 0), "desired": st.get("desiredReplicas", 0),
        "cpu": cpu, "target": target, "pods": pods,
        "ready": sum(1 for p in pods if p["ready"] and p["status"] == "Running"),
    }


class LoadGen:
    def __init__(self):
        self.lock = threading.Lock()
        self.workers = []
        self.ms = 200
        self.deadline = None
        self.scenario = None
        self.cancel = threading.Event()
        self._reset()

    def _reset(self):
        self.sent = 0
        self.errors = 0
        self.lat = collections.deque(maxlen=500)
        self.recent_pods = collections.deque(maxlen=200)
        self.stamps = collections.deque()

    def _work(self, stop):
        s = requests.Session()
        while not stop.is_set():
            t0, pod = time.time(), None
            try:
                r = s.get(f"{TARGET}/burn", params={"ms": self.ms}, timeout=30)
                r.raise_for_status()
                pod = r.json().get("pod")
            except Exception:
                pass
            now = time.time()
            with self.lock:
                self.sent += 1
                self.stamps.append(now)
                if pod:
                    self.lat.append((now - t0) * 1000)
                    self.recent_pods.append(pod)
                else:
                    self.errors += 1
            if not pod:
                stop.wait(0.5)

    def set_load(self, users, ms):
        users = max(0, min(MAX_USERS, int(users)))
        self.ms = max(10, min(2000, int(ms)))
        with self.lock:
            if users > 0 and not self.workers:
                self._reset()
            while len(self.workers) < users:
                ev = threading.Event()
                threading.Thread(target=self._work, args=(ev,), daemon=True).start()
                self.workers.append(ev)
            while len(self.workers) > users:
                self.workers.pop().set()

    def start(self, users, ms, duration):
        self.cancel_scenario()
        self.set_load(users, ms)
        self.deadline = time.time() + duration if duration and users > 0 else None

    def stop(self):
        self.cancel_scenario()
        self.deadline = None
        self.set_load(0, self.ms)

    def check_deadline(self):
        if self.deadline and time.time() > self.deadline:
            self.deadline = None
            self.set_load(0, self.ms)

    def cancel_scenario(self):
        self.cancel.set()
        self.scenario = None

    def run_scenario(self):
        self.cancel_scenario()
        self.cancel = ev = threading.Event()
        self.deadline = None
        threading.Thread(target=self._scenario, args=(ev,), daemon=True).start()

    def _scenario(self, cancel):
        for i, (name, users, ms, secs) in enumerate(STAGES):
            self.scenario = {"stage": name, "index": i, "total": len(STAGES), "ends": time.time() + secs}
            self.set_load(users, ms or self.ms)
            if cancel.wait(secs):
                return
        self.scenario = None

    def snapshot(self):
        now = time.time()
        with self.lock:
            while self.stamps and self.stamps[0] < now - 5:
                self.stamps.popleft()
            rps = len(self.stamps) / 5
            lat = sorted(self.lat)
            pods = dict(collections.Counter(self.recent_pods))
            sent, errors = self.sent, self.errors
        sc = self.scenario
        return {
            "running": bool(self.workers), "users": len(self.workers), "ms": self.ms,
            "sent": sent, "errors": errors, "rps": round(rps, 1),
            "avg_ms": round(sum(lat) / len(lat)) if lat else None,
            "p95_ms": round(lat[min(len(lat) - 1, int(len(lat) * 0.95))]) if lat else None,
            "pods": pods,
            "remaining": max(0, round(self.deadline - now)) if self.deadline else None,
            "scenario": {"stage": sc["stage"], "index": sc["index"], "total": sc["total"],
                         "left": max(0, round(sc["ends"] - now))} if sc else None,
        }


gen = LoadGen()
state_lock = threading.Lock()
latest = {"ts": time.time(), "load": gen.snapshot(), "cluster": None, "error": "starting"}
history = collections.deque(maxlen=300)  # one sample / 2s = last 10 minutes


def sampler():
    global latest
    while True:
        t0 = time.time()
        gen.check_deadline()
        snap = gen.snapshot()
        cluster, err = None, None
        try:
            cluster = cluster_status()
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:300]
        point = {"t": t0, "users": snap["users"], "rps": snap["rps"],
                 "cur": cluster["current"] if cluster else None,
                 "des": cluster["desired"] if cluster else None,
                 "cpu": cluster["cpu"] if cluster else None,
                 "ready": cluster["ready"] if cluster else None}
        with state_lock:
            latest = {"ts": t0, "load": snap, "cluster": cluster, "error": err}
            history.append(point)
        time.sleep(max(0.0, 2 - (time.time() - t0)))


threading.Thread(target=sampler, daemon=True).start()


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/status")
def status():
    with state_lock:
        out = dict(latest)
        out["history"] = list(history)
    out["load"] = gen.snapshot()
    out["config"] = {"target": TARGET, "hpa": HPA, "stages": STAGES, "max_users": MAX_USERS}
    return jsonify(out)


@app.route("/api/start", methods=["POST"])
def start():
    d = request.get_json(force=True, silent=True) or {}
    gen.start(d.get("users", 10), d.get("ms", 200), int(d.get("duration", 0) or 0))
    return jsonify(gen.snapshot())


@app.route("/api/stop", methods=["POST"])
def stop():
    gen.stop()
    return jsonify(gen.snapshot())


@app.route("/api/scenario", methods=["POST"])
def scenario():
    gen.run_scenario()
    return jsonify(ok=True)


@app.route("/healthz")
def health():
    return "ok"
