#!/usr/bin/env python3
"""Request tracking dashboard for the local nginx.

Reads the custom nginx access log (json lines, one per request) written by
local_cloud.sh (log_format `track`), aggregates per-request metadata, and
serves a minimal dashboard.

What it answers (from nginx's own request metadata):
  - Did the request come from GCP?          -> X-Via-GCP header ($http_x_via_gcp)
  - What request was made?                  -> method + URI + status
  - Outbound egress per request             -> $bytes_sent (bytes sent to client)
  - Inbound  egress per request             -> $request_length (bytes from client)
  - Which app initiated it?                 -> user-agent classification (Android
                                              app / iOS app / desktop app / browser / CLI)

Run:            python3 server/track_dashboard.py [--port 8093] [--log PATH]
Serve via nginx: location /track/ { proxy_pass http://127.0.0.1:8093/; }
"""

import argparse
import json
import os
import threading
import time
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

DEFAULT_LOG = "/var/log/nginx/track.log"
DEFAULT_PORT = 8093

APP_PATTERNS = [
    ("Nextcloud Android", "nextcloud-android"),
    ("Nextcloud iOS", "nextcloud-ios"),
    ("Nextcloud Desktop", "nextcloud-desktop"),
    ("Nextcloud DAVx5", "davx5"),
    ("curl", "curl"),
    ("wget", "wget"),
    ("Python requests", "python-requests"),
    ("Rclone/Nextcloud", "rclone"),
    ("Browser Chrome", "chrome"),
    ("Browser Firefox", "firefox"),
    ("Browser Safari", "safari"),
    ("Browser Edge", "edg"),
    ("Browser (other Mozilla)", "mozilla"),
]


def classify_app(ua):
    ua = (ua or "").lower()
    if "nextcloud-android" in ua:
        return "Nextcloud Android app"
    if "nextcloud-ios" in ua or "nextcloudmobile" in ua:
        return "Nextcloud iOS app"
    if "nextcloud-desktop" in ua:
        return "Nextcloud Desktop app"
    for label, needle in APP_PATTERNS:
        if needle in ua:
            return label
    if ua:
        return "Unknown client"
    return "(no user-agent)"


def status_class(status):
    try:
        s = int(status)
    except (TypeError, ValueError):
        return "?"
    return f"{s // 100}xx"


class Tracker:
    def __init__(self, log_path, keep_entries=5000):
        self.path = log_path
        self.keep = keep_entries
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.total = 0
        self.gcp_total = 0
        self.by_app = Counter()
        self.by_uri_top = Counter()
        self.by_method = Counter()
        self.by_status = Counter()
        self.out_bytes = {"all": 0, "gcp": 0, "non_gcp": 0}
        self.in_bytes = {"all": 0, "gcp": 0, "non_gcp": 0}
        self.recent = []
        self.start_ts = time.time()
        self.last_ts = None

    def _ingest(self, raw):
        try:
            r = json.loads(raw)
        except (ValueError, TypeError):
            return
        self.total += 1
        gcp = str(r.get("gcp") or "").strip().lower()
        is_gcp = gcp in ("1", "true", "yes", "on")
        if is_gcp:
            self.gcp_total += 1

        ua = r.get("ua") or ""
        app = classify_app(ua)
        self.by_app[app] += 1

        uri = r.get("uri") or "/"
        self.by_uri_top[uri] += 1

        method = r.get("method") or "-"
        self.by_method[method] += 1

        status = r.get("status") or 0
        self.by_status[status_class(status)] += 1

        out = int(r.get("out") or 0)
        inc = int(r.get("in") or 0)
        bucket = "gcp" if is_gcp else "non_gcp"
        self.out_bytes["all"] += out
        self.out_bytes[bucket] += out
        self.in_bytes["all"] += inc
        self.in_bytes[bucket] += inc

        ts = r.get("time") or time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.last_ts = ts
        entry = {
            "time": ts,
            "gcp": is_gcp,
            "app": app,
            "method": method,
            "uri": uri,
            "status": status,
            "in": inc,
            "out": out,
        }
        self.recent.append(entry)
        if len(self.recent) > self.keep:
            self.recent = self.recent[-self.keep:]

    def poll(self):
        """Read newly appended lines from the log and ingest them."""
        if not os.path.exists(self.path):
            return
        pos = getattr(self, "_offset", 0)
        size = os.path.getsize(self.path)
        if size < pos:
            # rotated/truncated — restart from the beginning
            pos = 0
        with open(self.path, "rb") as f:
            f.seek(pos)
            lines = f.read()
            self._offset = f.tell()
        for raw in lines.decode("utf-8", "replace").splitlines():
            self._ingest(raw)

    def stats(self):
        with self.lock:
            self.poll()
            top_uri = self.by_uri_top.most_common(25)
            return {
                "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "uptime_secs": int(time.time() - self.start_ts),
                "last_seen": self.last_ts,
                "total": self.total,
                "gcp_total": self.gcp_total,
                "non_gcp_total": self.total - self.gcp_total,
                "in_bytes": dict(self.in_bytes),
                "out_bytes": dict(self.out_bytes),
                "by_app": self.by_app.most_common(),
                "by_method": self.by_method.most_common(),
                "by_status": self.by_status.most_common(),
                "top_uri": top_uri,
                "recent": list(reversed(self.recent[-200:])),
            }


TRACKER = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/stats":
            data = json.dumps(TRACKER.stats()).encode()
            self._send(200, "application/json", data)
        elif path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE.encode())
        else:
            self._send(404, "text/plain", b"not found")


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Request Tracker</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0d1117; color: #c9d1d9; padding: 24px; }
h1 { color: #58a6ff; font-size: 22px; margin-bottom: 4px; }
.sub { color: #8b949e; font-size: 13px; margin-bottom: 20px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin-bottom: 20px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 16px; }
.card .label { font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: #8b949e; }
.card .val { font-size: 26px; font-weight: 700; color: #58a6ff; }
.card .subval { font-size: 12px; color: #8b949e; margin-top: 2px; }
.cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }
.panel { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 16px; margin-bottom: 14px; }
.panel h3 { color: #58a6ff; font-size: 14px; margin-bottom: 10px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #21262d; }
th { color: #8b949e; font-weight: 600; }
.bar { display: inline-block; height: 10px; border-radius: 3px; background: #1f6feb; vertical-align: middle; }
.bar-gcp { background: #f85149; }
.gcp-yes { color: #f85149; font-weight: 600; }
.gcp-no  { color: #3fb950; font-weight: 600; }
tr.recent:hover td { background: #1c2128; }
@media (prefers-reduced-motion: reduce) { * { transition: none; } }
</style>
</head>
<body>
<h1>Request Tracker</h1>
<div class="sub" id="meta">loading…</div>
<div class="grid" id="cards"></div>
<div class="cols">
  <div class="panel"><h3>By app / initiator</h3><table id="appTable"></table></div>
  <div class="panel"><h3>Top URIs</h3><table id="uriTable"></table></div>
  <div class="panel"><h3>Methods</h3><table id="methodTable"></table></div>
  <div class="panel"><h3>Status</h3><table id="statusTable"></table></div>
</div>
<div class="panel"><h3>Recent requests</h3><table id="recentTable"></table></div>
<script>
function fmtBytes(n) {
  if (n >= 1<<30) return (n/(1<<30)).toFixed(2)+' GiB';
  if (n >= 1<<20) return (n/(1<<20)).toFixed(2)+' MiB';
  if (n >= 1<<10) return (n/(1<<10)).toFixed(1)+' KiB';
  return n+' B';
}
function barRow(label, count, total, color) {
  const w = total ? (count/total*100) : 0;
  return '<tr><td>'+label+'</td><td>'+count+'</td><td><span class="bar '+(color||'')+'" style="width:'+w.toFixed(1)+'%"></span></td></tr>';
}
function render(s) {
  const gcpPct = s.total ? (s.gcp_total/s.total*100).toFixed(1) : '0';
  document.getElementById('meta').textContent =
    'Generated '+s.generated+' · tracking for '+Math.floor(s.uptime_secs/60)+' min · last request '+s.last_seen;
  const cards = [
    {label:'Total requests', val:s.total},
    {label:'From GCP', val:s.gcp_total, sub:gcpPct+'% of all', cls:'gcp-yes'},
    {label:'Not GCP', val:s.non_gcp_total, cls:'gcp-no', sub:(100-parseFloat(gcpPct)).toFixed(1)+'% of all'},
    {label:'Inbound (from clients)', val:fmtBytes(s.in_bytes.all), sub:'GCP '+fmtBytes(s.in_bytes.gcp)},
    {label:'Outbound (to clients)', val:fmtBytes(s.out_bytes.all), sub:'GCP '+fmtBytes(s.out_bytes.gcp)},
  ];
  document.getElementById('cards').innerHTML = cards.map(c =>
    '<div class="card"><div class="label">'+c.label+'</div><div class="val '+(c.cls||'')+'">'+c.val+'</div><div class="subval">'+(c.sub||'')+'</div></div>'
  ).join('');
  const total = s.total || 1;
  document.getElementById('appTable').innerHTML =
    '<tr><th>App</th><th>Requests</th><th style="width:40%"></th></tr>' +
    s.by_app.map(r=>barRow(r[0],r[1],total)).join('');
  document.getElementById('uriTable').innerHTML =
    '<tr><th>URI</th><th>Requests</th><th style="width:40%"></th></tr>' +
    s.top_uri.map(r=>barRow(r[0],r[1],total)).join('');
  document.getElementById('methodTable').innerHTML =
    '<tr><th>Method</th><th>Requests</th><th style="width:40%"></th></tr>' +
    s.by_method.map(r=>barRow(r[0],r[1],total)).join('');
  document.getElementById('statusTable').innerHTML =
    '<tr><th>Class</th><th>Requests</th><th style="width:40%"></th></tr>' +
    s.by_status.map(r=>barRow(r[0],r[1],total)).join('');
  document.getElementById('recentTable').innerHTML =
    '<tr><th>Time</th><th>GCP</th><th>App</th><th>Method</th><th>URI</th><th>Status</th><th>Inb</th><th>Out</th></tr>' +
    s.recent.map(r =>
      '<tr class="recent"><td>'+r.time+'</td><td class="'+(r.gcp?'gcp-yes':'gcp-no')+'">'+(r.gcp?'yes':'no')+'</td><td>'+(r.app||'')+'</td><td>'+r.method+'</td><td>'+r.uri+'</td><td>'+r.status+'</td><td>'+fmtBytes(r.in)+'</td><td>'+fmtBytes(r.out)+'</td></tr>'
    ).join('');
}
function refresh() {
  fetch('/api/stats').then(r=>r.json()).then(render).catch(()=>{});
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--log", default=DEFAULT_LOG)
    parser.add_argument("--bind", default="127.0.0.1")
    args = parser.parse_args()

    global TRACKER
    TRACKER = Tracker(args.log)
    # Prime with existing log content on startup.
    TRACKER.poll()

    os.makedirs(os.path.dirname(args.log), exist_ok=True)
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"[track] dashboard on http://{args.bind}:{args.port} reading {args.log}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()