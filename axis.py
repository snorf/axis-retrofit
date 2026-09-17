# A local replacement web GUI for old AXIS network cameras (VAPIX 2 / firmware 4.x),
# plus a receiver for the images the camera uploads on motion.
#
# The camera's own GUI needs a Java applet for motion detection and ActiveX or a Java applet
# for live video, so it is unusable in a current browser. Everything it does is a thin layer
# over a handful of CGIs, which is what this file talks to instead.
#
# Note on speed: firmware 4.x runs Linux 2.6.23 with tcp_tw_recycle, which silently drops
# SYNs for up to 60 s after a closed connection when the client's TCP timestamp is lower than
# the previous one. Any single request can therefore take 35 or 67 s. Optional fix on the
# host running this file:
#   macOS:  sudo sysctl -w net.inet.tcp.rfc1323=0
#   Linux:  iptables -t mangle -A OUTPUT -d <camera-ip> -p tcp --syn -j TCPOPTSTRIP --strip-options timestamp
import datetime
import json
import os
import re
import socket
import socketserver
import sys
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

import requests
from flask import Flask, Response, jsonify, request, send_from_directory
from requests.auth import HTTPBasicAuth
from werkzeug.serving import WSGIRequestHandler

# The camera speaks HTTP/1.0 and waits for the connection to close instead of honouring
# Content-Length. Werkzeug answers HTTP/1.1 and keeps the socket open, so the camera stalls
# for 60 s, gives up and retries the same image; only one picture per event ever arrives.
# Answering HTTP/1.0 makes the server close after each response.
WSGIRequestHandler.protocol_version = "HTTP/1.0"

sys.stdout.reconfigure(line_buffering=True)  # so print() reaches a log file / journal promptly

HOST = os.environ.get("AXIS_HOST", "")
UPLOAD_DIR = os.path.abspath(os.environ.get("UPLOAD_DIR", "./uploads"))
PORT = int(os.environ.get("PORT", "6001"))  # not 6000: browsers refuse it as an unsafe port
# The camera uploads to its own listener, not to Flask; see UploadHandler.
UPLOAD_PORT = int(os.environ.get("UPLOAD_PORT", PORT + 1))
CREDS = os.environ.get("AXIS_CREDS", ".axis-creds")
# Address the camera should upload to. Only needed when this cannot be detected from the
# route to the camera, i.e. behind NAT (a bridged container) or a reverse proxy.
# Accepts "host" or "host:port".
ADVERTISE = os.environ.get("ADVERTISE_HOST", "")
TIMEOUT = (130, 30)  # connect: Linux retries SYN at 63 and 127 s, see the note above

if not HOST:
    sys.exit("AXIS_HOST is not set. Example: AXIS_HOST=192.0.2.10 python3 axis.py")
try:
    with open(CREDS) as f:
        USER, PASS = f.read().strip().split(":", 1)
except (OSError, ValueError):
    sys.exit(f"Cannot read camera credentials from {CREDS!r}. "
             "Create it with a single line: username:password")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Left-hand menu. Every parameter group the camera exposes belongs to exactly one section;
# adding a section is one line here and nothing else.
SECTIONS = [
    ("image", "Image", ["Image", "ImageSource"]),
    ("network", "Network", ["Network", "SOCKS", "HTTPS", "SNMP", "Bandwidth"]),
    ("time", "Time", ["Time"]),
    ("audio", "Audio", ["Audio", "AudioSource"]),
    ("notify", "Notifications", ["Notify", "SMTP", "MailLogd"]),
    ("layout", "Live View", ["Layout"]),
    ("events", "Events (raw)", ["Event", "EventServers", "Motion"]),
    ("system", "System", ["System", "Log", "StatusLED", "Input", "Output",
                          "Brand", "Properties"]),
]

# Parameters that can make the camera unreachable. Flagged for the user, never blocked.
RISKY = re.compile(r"^root\.(Network\.(BootProto|IPAddress|SubnetMask|DefaultRouter|"
                   r"Interface\.|Resolver\.|Routing\.|Filter\.|HTTP\.Authentication)|"
                   r"System\.(BoaPort|AlternateBoaPort|BoaProtViewer))")
KEY = re.compile(r"^root\.[A-Za-z0-9_.]+$")


class CameraError(Exception):
    pass


class Camera:
    def __init__(self, host, user, pw):
        self.base = f"http://{host}/axis-cgi/"
        self.auth = HTTPBasicAuth(user, pw)
        self.lock = threading.Lock()  # one connection at a time, see the note at the top

    def _request(self, method, path, **kw):
        with self.lock:  # released once the headers are in, also for streams
            try:
                return requests.request(method, self.base + path, auth=self.auth,
                                        timeout=TIMEOUT, **kw)
            except requests.ConnectionError as e:  # connect refused outright: one fresh try
                print("camera connect failed, retrying:", e)
                return requests.request(method, self.base + path, auth=self.auth,
                                        timeout=TIMEOUT, **kw)

    def get(self, path, stream=False, **params):
        r = self._request("GET", path, params=params, stream=stream)
        if r.status_code != 200:
            if stream:
                r.close()
            raise CameraError(f"{path} returned HTTP {r.status_code}")
        return r

    def param(self, kv):
        body = urlencode(kv, encoding="iso-8859-1")  # the camera speaks Latin-1
        r = self._request("POST", "admin/param.cgi", data=body,
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
        r.encoding = "iso-8859-1"
        if r.status_code != 200 or r.text.lstrip().startswith("#"):  # errors: "# Error: ..."
            raise CameraError(r.text.strip() or f"HTTP {r.status_code}")
        return r.text

    def list(self):
        return dict(line.split("=", 1) for line in self.param({"action": "list"}).splitlines()
                    if "=" in line)

    def update(self, params):
        self.param({"action": "update", **params})

    def add(self, group, template, params):
        text = self.param({"action": "add", "group": group, "template": template, **params})
        m = re.search(r"\b([A-Z]\d+)\b", text)  # the camera answers "M1 OK"
        if not m:
            raise CameraError(text)
        return m.group(1)

    def remove(self, group):
        self.param({"action": "remove", "group": group})


app = Flask(__name__)
cam = Camera(HOST, USER, PASS)


# --------------------------------------------------------------------- upload receiver

def save_upload(ts, filename, data):
    name = f"{ts}-{os.path.basename(filename or 'image.jpg')}"
    with open(os.path.join(UPLOAD_DIR, name), "wb") as f:
        f.write(data)
    return name


class UploadHandler(socketserver.StreamRequestHandler):
    """Receives the images the camera posts.

    This is deliberately not a Flask route. The camera's HTTP client waits for the server
    to close the connection and ignores Content-Length, and Werkzeug keeps the socket open
    after responding. The camera then stalls for 60 s, abandons the upload and retries the
    same picture, so only one image per event ever arrives. Forty lines of socket code that
    shut the connection down explicitly are worth more here than a WSGI server."""

    timeout = 30

    def handle(self):
        try:
            line = self.rfile.readline(8192)
            if not line:
                return
            method = line.split(b" ", 1)[0].upper()
            headers = {}
            while True:
                h = self.rfile.readline(8192)
                if h in (b"\r\n", b"\n", b""):
                    break
                k, _, v = h.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()
            body = b""
            n = int(headers.get("content-length", "0") or 0)
            while n > 0:  # read exactly Content-Length; the camera appends a stray CRLF
                chunk = self.rfile.read(min(n, 65536))
                if not chunk:
                    break
                body += chunk
                n -= len(chunk)
            if method == b"POST" and body:
                ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                m = re.search(r'filename="([^"]+)"', headers.get("content-disposition", ""))
                name = save_upload(ts, m.group(1) if m else "image.jpg", body)
                print("upload from", self.client_address[0], len(body), "bytes:", name)
            self.wfile.write(b"HTTP/1.0 200 OK\r\nContent-Length: 0\r\n"
                             b"Connection: close\r\n\r\n")
            self.wfile.flush()
        except (OSError, ValueError) as e:
            print("upload from", self.client_address[0], "failed:", e)
        finally:
            try:  # the camera is waiting for this, and only this
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class UploadServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


EVENT_GAP = 8  # seconds between two captures that starts a new event


def capture_time(name):
    """When the camera took the picture, from its own stamp in the filename, falling back
    to when we received it."""
    m = re.search(r"(\d\d)-(\d\d)-(\d\d)_(\d\d)-(\d\d)-(\d\d)", name)
    if m:
        y, mo, d, H, M, S = (int(g) for g in m.groups())
        try:
            return datetime.datetime(2000 + y, mo, d, H, M, S)
        except ValueError:
            pass
    m = re.match(r"(\d{8})-(\d{6})", name)
    if m:
        try:
            return datetime.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            pass
    return datetime.datetime.min


def uploaded_events():
    """Files grouped into events, newest event first, images within an event in order."""
    files = []
    for e in os.scandir(UPLOAD_DIR):
        if e.is_file():
            try:
                files.append((capture_time(e.name), e.name, e.stat().st_size))
            except OSError:
                pass  # removed while listing
    files.sort()
    events = []
    for f in files:
        if events and (f[0] - events[-1][-1][0]).total_seconds() <= EVENT_GAP:
            events[-1].append(f)
        else:
            events.append([f])
    events.reverse()
    return events


@app.get("/api/events")
def api_events():
    """The upload directory is the source of truth, so the list survives a restart."""
    try:
        limit = max(1, min(100, int(request.args.get("limit", 15))))
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        return jsonify(error="Invalid offset or limit"), 400
    events = uploaded_events()
    items = [{"start": g[0][0].isoformat(sep=" ", timespec="seconds"),
              "seconds": int((g[-1][0] - g[0][0]).total_seconds()),
              "bytes": sum(f[2] for f in g),
              "files": [f[1] for f in g]}
             for g in events[offset:offset + limit]]
    return jsonify(items=items, total=len(events), offset=offset, limit=limit)


@app.get("/uploads/<name>")
def uploaded_file(name):
    return send_from_directory(UPLOAD_DIR, os.path.basename(name))


# ------------------------------------------------------------------------------- state

def groups(p, prefix):
    """{"M0": {"Name": ..}, ...} for all keys under prefix."""
    out = {}
    for k, v in p.items():
        if k.startswith(prefix):
            idx, _, key = k[len(prefix):].partition(".")
            if key:
                out.setdefault(idx, {})[key] = v
    return out


def state(p=None):
    """Everything the page needs, from a single camera connection."""
    p = p or cam.list()
    return {
        "motion": groups(p, "root.Motion."),
        "servers": groups(p, "root.EventServers.HTTP."),
        "actions": groups(p, "root.Event.E0.Actions."),
        "event": {k[len("root.Event.E0."):]: v for k, v in p.items()
                  if k.startswith("root.Event.E0.")
                  and not k.startswith("root.Event.E0.Actions.")},
        "params": p,
        "risky": [k for k in p if RISKY.match(k)],
    }


@app.errorhandler(requests.RequestException)
def cam_unreachable(e):
    print("camera unreachable:", type(e).__name__)
    return jsonify(error=f"Camera is not responding ({type(e).__name__})"), 504


@app.errorhandler(CameraError)
def cam_error(e):
    return jsonify(error=str(e)), 502


@app.get("/")
def index():
    where = ADVERTISE
    if not where:  # the address the camera can reach us on; a UDP connect sends nothing
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((HOST, 80))
            where = s.getsockname()[0]
            s.close()
        except OSError:
            where = "127.0.0.1"
    if ":" not in where:
        where = f"{where}:{UPLOAD_PORT}"
    return (HTML.replace("__UPLOAD_URL__", f"http://{where}/upload")
                .replace("__SECTIONS__", json.dumps(SECTIONS))
                .replace("__CAMERA__", HOST))


@app.get("/api/state")
def api_state():
    return jsonify(state())


# ------------------------------------------------------------------- generic parameters

def parse_defs(xml_text):
    """The camera describes every parameter's type, allowed values and label in
    listdefinitions. Turn that into {key: {kind, nice, values|min|max|true|false}}."""
    # Re-encode: the document declares iso-8859-1, and ElementTree rejects a str that does.
    root = ET.fromstring(xml_text.encode("iso-8859-1"))
    out = {}

    def walk(el, path):
        for child in el:
            tag = child.tag.rpartition("}")[2]
            name = child.get("name")
            if tag == "group":
                walk(child, f"{path}.{name}" if path else name)
            elif tag == "parameter":
                d = {"nice": child.get("niceName") or name}
                ty = child.find("{*}type")
                if ty is not None and len(ty):
                    k = ty[0]
                    d["kind"] = kind = k.tag.rpartition("}")[2]
                    if kind == "bool":
                        d["true"], d["false"] = k.get("true"), k.get("false")
                    elif kind == "enum":
                        d["values"] = [[e.get("value"), e.get("niceValue") or e.get("value")]
                                       for e in k]
                    else:
                        for a in ("min", "max", "maxlen"):
                            if k.get(a) is not None:
                                d[a] = k.get(a)
                out[f"{path}.{name}"] = d

    walk(root, "")
    return out


_defs = None
_defs_lock = threading.Lock()


@app.get("/api/defs")
def api_defs():
    """Parameter definitions. Fixed for a given firmware, so fetched once and cached."""
    global _defs
    with _defs_lock:
        if _defs is None:
            _defs = parse_defs(cam.param({"action": "listdefinitions",
                                          "listformat": "xmlschema"}))
    return jsonify(_defs)


@app.put("/api/params")
def put_params():
    changed = request.json or {}
    bad = [k for k in changed if not KEY.match(k)]
    if bad:
        return jsonify(error=f"Invalid parameter key: {bad[0]}"), 400
    if changed:
        cam.update(changed)  # the whole batch in one connection
        print("params updated:", " ".join(sorted(changed)))  # keys only, never values
    return jsonify(state())


# ------------------------------------------------------------------------ motion windows

FIELDS = ("Name", "WindowType", "Left", "Top", "Right", "Bottom",
          "Sensitivity", "History", "ObjectSize")


def window_fields(prefix):
    return {f"{prefix}.{k}": request.json[k] for k in FIELDS}


@app.post("/api/motion")
def motion_add():
    new = cam.add("Motion", "motion",
                  {"Motion.M.ImageSource": "0", **window_fields("Motion.M")})
    return jsonify(added=new, **state())


@app.put("/api/motion/<idx>")
def motion_update(idx):
    if not re.fullmatch(r"M\d", idx):
        return jsonify(error="Invalid window"), 400
    cam.update(window_fields(f"root.Motion.{idx}"))
    return jsonify(state())


@app.delete("/api/motion/<idx>")
def motion_delete(idx):
    if not re.fullmatch(r"M\d", idx):
        return jsonify(error="Invalid window"), 400
    cam.remove(f"root.Motion.{idx}")
    return jsonify(state())


# -------------------------------------------------------------------- upload target (event)

@app.put("/api/upload-target")
def upload_target():
    b = request.json
    p = cam.list()
    servers = groups(p, "root.EventServers.HTTP.")
    actions = groups(p, "root.Event.E0.Actions.")
    upd = {}
    srv = next(iter(servers), None)
    if srv:
        upd[f"root.EventServers.HTTP.{srv}.Address"] = b["url"]
    else:
        h = "root.EventServers.HTTP.H."
        srv = cam.add("root.EventServers.HTTP", "http_config", {
            h + "Name": "axis.py", h + "Address": b["url"], h + "Login": "", h + "Password": "",
            h + "Proxy": "", h + "ProxyPort": "0", h + "ProxyLogin": "", h + "ProxyPassword": ""})
    http = next((a for a, v in actions.items()
                 if v.get("Type") == "U" and v.get("Protocol") == "HTTP"), None)
    if http:
        upd[f"root.Event.E0.Actions.{http}.Server"] = srv
    else:
        a = "root.Event.E0.Actions.A."
        cam.add("root.Event.E0.Actions", "httpaction",
                {a + "Type": "U", a + "Protocol": "HTTP", a + "Server": srv, a + "Order": "0"})
    for a, v in actions.items():  # drop other upload actions (the stock SMTP mail action)
        if a != http and v.get("Type") == "U":
            cam.remove(f"root.Event.E0.Actions.{a}")
    pre, post, mi = int(b["pre"]), int(b["post"]), int(b["min_interval"])
    jpg = b["fileformat"] != "mp4"
    window = next(iter(groups(p, "root.Motion.")), "M0")
    e = "root.Event.E0."
    upd.update({  # Enabled flips last, so a timeout never leaves a half-built event enabled
        e + "Enabled": "yes" if b["enabled"] else "no",
        e + "SWInput": f"IO5:/|{window}:/",  # web button, or motion starting in the window
        e + "FileFormat": "jpg" if jpg else "mp4",  # "jpg" as in the camera's own page
        e + "FileName": "image.jpg" if jpg else "image.mp4",
        e + "IncludePreTrigger": "yes" if pre else "no",
        e + "PreTriggerSize": pre, e + "MPEGPreTriggerDuration": pre,
        e + "IncludePostTrigger": "yes" if post else "no",
        e + "PostTriggerSize": post, e + "MPEGPostTriggerDuration": post,
        e + "MinimumTriggerInterval": "%02d:%02d:%02d" % (mi // 3600, mi // 60 % 60, mi % 60),
    })
    cam.update(upd)
    return jsonify(state())


@app.post("/api/upload-target/test")
def upload_target_test():
    r = cam.get("operator/httptest.cgi", address=request.json["url"], username="", password="",
                proxyaddress="", proxyport="", proxylogin="", proxypassword="")
    t = r.text.strip()
    return jsonify(ok="Error" not in t and "Fail" not in t, text=t)


@app.post("/api/trigger")
def trigger():
    """Fire virtual input 6 (the web button, IO5) so the event runs without real motion."""
    cam.get("io/virtualinput.cgi", action="6:/")
    # The input must stay high long enough for the event to run: a back-to-back pulse
    # is missed entirely, and a short one yields pre-trigger frames but no post-trigger.
    time.sleep(6)
    cam.get("io/virtualinput.cgi", action="6:\\")
    return jsonify(ok=True)


# ------------------------------------------------------------------------ live video/level

@app.get("/video.mjpg")
def video():
    r = cam.get("mjpg/video.cgi", stream=True, resolution="640x480", camera="1")

    def gen():
        try:
            for chunk in r.iter_content(8192):
                yield chunk
        finally:
            r.close()  # GeneratorExit when the browser drops the connection

    return Response(gen(), content_type=r.headers.get("Content-Type", "multipart/x-mixed-replace"),
                    headers={"Cache-Control": "no-store"})


@app.get("/snapshot.jpg")
def snapshot():
    r = cam.get("jpg/image.cgi", resolution="640x480")
    return Response(r.content, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/motionlevel")
def motionlevel():
    r = cam.get("motion/motiondata.cgi", stream=True, group=request.args.get("group", "0"))

    def gen():
        try:
            # 1-byte reads: the stream is neither chunked nor length-delimited, so a larger
            # read would block until that many bytes have trickled in.
            for line in r.iter_lines(chunk_size=1):
                m = re.search(rb"level=(\d+);threshold=(\d+)", line)
                if m:
                    yield f"data: {m[1].decode()} {m[2].decode()}\n\n"
        finally:
            r.close()

    return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache"})


# ------------------------------------------------------------------------------ maintenance

@app.get("/api/systemlog")
def systemlog():
    return Response(cam.get("admin/systemlog.cgi").text, mimetype="text/plain")


@app.get("/api/serverreport")
def serverreport():
    return Response(cam.get("admin/serverreport.cgi").text, mimetype="text/plain")


@app.get("/api/backup")
def backup():
    r = cam.get("admin/backup.cgi")
    return Response(r.content, mimetype="application/octet-stream",
                    headers={"Content-Disposition": 'attachment; filename="axis-backup.bin"'})


@app.post("/api/action/<name>")
def action(name):
    paths = {"restart": "admin/restart.cgi", "factorydefault": "admin/factorydefault.cgi"}
    if name not in paths:
        return jsonify(error="Unknown action"), 400
    cam.get(paths[name])
    return jsonify(ok=True)


HTML = r"""<!doctype html>
<meta charset="utf-8">
<title>AXIS __CAMERA__</title>
<style>
 body{font:14px system-ui,sans-serif;margin:0;color:#222;background:#fafafa}
 #status{padding:8px 14px;background:#eef;min-height:1.2em;border-bottom:1px solid #dde}
 #status.err{background:#fdd}
 #wrap{display:flex;align-items:flex-start}
 nav{width:170px;flex:none;padding:12px 0;border-right:1px solid #ddd;min-height:90vh;background:#fff}
 nav a{display:block;padding:6px 14px;color:#225;text-decoration:none;cursor:pointer}
 nav a:hover{background:#eef}
 nav a.on{background:#225;color:#fff}
 nav hr{border:0;border-top:1px solid #ddd;margin:8px 12px}
 main{padding:14px 20px;flex:1;min-width:0}
 h2{font-size:16px;margin:0 0 10px}
 .row{display:flex;gap:24px;align-items:flex-start;flex-wrap:wrap}
 #stage{position:relative;width:640px;height:480px;background:#000;touch-action:none;user-select:none}
 #stage img,#livevid{display:block;width:640px;height:480px;background:#000}
 .win{position:absolute;box-sizing:border-box;border:2px solid #0c0;background:rgba(0,200,0,.08);cursor:move}
 .win.exclude{border-color:#c00;background:rgba(200,0,0,.1)}
 .win.sel{border-width:3px}
 .win .name{position:absolute;left:0;top:0;font-size:11px;background:rgba(0,0,0,.6);color:#fff;padding:0 3px;pointer-events:none;white-space:nowrap}
 .grip{position:absolute;right:0;bottom:0;width:12px;height:12px;background:#fff;opacity:.7;cursor:nwse-resize}
 fieldset{border:1px solid #ccc;border-radius:4px;min-width:300px;background:#fff}
 label{display:block;margin:5px 0}
 label span{display:inline-block;width:190px;vertical-align:top}
 #sec-upload label span,#panel label span{width:130px}
 input[type=range]{width:130px;vertical-align:middle}
 output{display:inline-block;width:2em;text-align:right}
 #bar{position:relative;height:14px;background:#ddd;margin:6px 0;width:640px}
 #lvl{height:100%;width:0;background:#0a0}
 #thr{position:absolute;top:0;width:2px;height:100%;background:#000}
 button{margin:6px 4px 0 0}
 button.mini{margin:0 0 0 6px;font-size:11px}
 table{border-collapse:collapse}td{padding:2px 10px 2px 0;border-bottom:1px solid #eee}
 small{color:#666}
 table.pick tr{cursor:pointer}
 table.pick tr:hover td{background:#eef}
 table.pick tr.sel td{background:#225;color:#fff}
 #shot{width:640px;height:480px;background:#000;display:flex;align-items:center;justify-content:center}
 #shot img{max-width:100%;max-height:100%;display:block}
 details{border:1px solid #ddd;border-radius:4px;margin:6px 0;padding:4px 10px;background:#fff}
 summary{cursor:pointer;font-weight:600;padding:3px 0}
 label.risk span{color:#a30}
 label.risk::after{content:" ⚠";color:#a30}
 pre{background:#fff;border:1px solid #ddd;padding:10px;max-height:60vh;overflow:auto;white-space:pre-wrap}
</style>
<div id="status">Loading…</div>
<div id="wrap">
<nav id="nav"></nav>
<main>

<section id="sec-live" hidden>
 <h2>Live</h2>
 <img id="livevid" alt="">
 <p><small>The stream comes from the camera's MJPEG output and is closed when you leave this section.</small></p>
</section>

<section id="sec-motion" hidden>
 <h2>Motion windows</h2>
 <div class="row">
  <div>
   <div id="stage"><img id="camimg" alt=""></div>
   <div id="bar"><div id="lvl"></div><div id="thr"></div></div>
   <small>Live activity level for the selected window. Red means above the trigger threshold (object size).</small>
  </div>
  <fieldset id="panel">
   <legend>Selected window: <span id="selname">–</span></legend>
   <label><span>Name</span><input autocomplete="off" id="Name" pattern="[A-Za-z0-9 _-]+" size="18"></label>
   <label><span>Type</span><select autocomplete="off" id="WindowType"><option value="include">Include</option><option value="exclude">Exclude</option></select></label>
   <label><span>Object size</span><input autocomplete="off" type="range" id="ObjectSize" min="0" max="100" oninput="this.nextElementSibling.textContent=this.value"><output></output></label>
   <label><span>History</span><input autocomplete="off" type="range" id="History" min="0" max="100" oninput="this.nextElementSibling.textContent=this.value"><output></output></label>
   <label><span>Sensitivity</span><input autocomplete="off" type="range" id="Sensitivity" min="0" max="100" oninput="this.nextElementSibling.textContent=this.value"><output></output></label>
   <small>Recommended: object size 5–15, history 60–90, sensitivity 75–95. If the level stays above the threshold with an empty room, the camera is triggering on noise.</small><br>
   <button id="save">Save</button><button id="neu">New window</button><button id="del">Delete</button><button id="reload">Reload</button>
  </fieldset>
 </div>
</section>

<section id="sec-upload" hidden>
 <h2>HTTP upload on motion</h2>
 <fieldset>
  <label><span>URL</span><input autocomplete="off" id="url" size="40" value="__UPLOAD_URL__"></label>
  <label><span>Format</span><select autocomplete="off" id="fileformat"><option value="jpg">JPEG images</option><option value="mp4">MP4 clips</option></select></label>
  <label><span>Pre-trigger</span><input autocomplete="off" id="pre" type="number" min="0" max="30"> <small>seconds, one image per second</small></label>
  <label><span>Post-trigger</span><input autocomplete="off" id="post" type="number" min="0" max="30"> <small>seconds, one image per second</small></label>
  <label><span>Min interval (s)</span><input autocomplete="off" id="min_interval" type="number" min="0"></label>
  <label><span>Enabled</span><input autocomplete="off" id="enabled" type="checkbox"></label>
  <div id="target"></div>
  <button id="test">Test connection</button><button id="savet">Save</button><button id="trig">Trigger event now</button>
 </fieldset>
 <h2 style="margin-top:18px">Events</h2>
 <div class="row">
  <div>
   <table id="events" class="pick"></table>
   <p><button id="evprev">Previous</button><button id="evnext">Next</button>
      <small id="evpage"></small></p>
  </div>
  <div id="viewer" hidden>
   <div id="shot"><img id="shotimg" alt=""></div>
   <p><button id="imgprev">&#9664;</button><button id="imgplay">Play</button><button id="imgnext">&#9654;</button>
      <small id="imgpos"></small> &nbsp;<a id="imglink" target="_blank">open full size</a></p>
  </div>
 </div>
</section>

<section id="sec-params" hidden>
 <h2 id="ptitle"></h2>
 <div id="params"></div>
 <button id="psave">Save changes</button><button id="preload">Reload</button>
 <span id="pcount"></span>
</section>

<section id="sec-maint" hidden>
 <h2>Maintenance</h2>
 <p>
  <button id="mlog">System log</button>
  <button id="mrep">Server report</button>
  <a href="/api/backup">Download backup</a>
 </p>
 <pre id="mout">The server report contains the camera's passwords in cleartext. Do not share it.</pre>
 <p>
  <button id="mrestart">Restart camera</button>
  <button id="mfact">Factory reset</button>
  <small>Factory reset keeps the network settings. Restoring from a backup is done in the camera's own interface.</small>
 </p>
</section>

</main>
</div>
<script>
const SECTIONS=__SECTIONS__;
const W=640,H=480,S=9999;  // camera coords 0..9999, origin top-left, y down
const px=(v,d)=>Math.round(v/S*d), un=(p,d)=>Math.max(0,Math.min(S,Math.round(p/d*S)));
const $=id=>document.getElementById(id);
const statusEl=$('status'), stage=$('stage');
let st=null, P=null, RISK=new Set(), sel=null, es=null, cur=null, snapTried=false;

function status(m,err){statusEl.textContent=m;statusEl.className=err?'err':'';}
async function api(method,url,body){
  status('Talking to the camera… (can take up to 70 s)');
  document.querySelectorAll('button').forEach(b=>b.disabled=true);
  try{
    const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:body&&JSON.stringify(body)});
    const j=await r.json();
    if(!r.ok){if(st)render(st);status('Error: '+j.error+' - showing the last confirmed state. Reload fetches the camera\'s.',true);return null;}
    status('Done');return j;
  }catch(e){status('Error: '+e.message,true);return null;}
  finally{document.querySelectorAll('button').forEach(b=>b.disabled=false);}
}

/* ---------------------------------------------------------------- navigation */
function buildNav(){
  const n=$('nav');
  const add=(id,label)=>{const a=document.createElement('a');a.dataset.id=id;a.textContent=label;
    a.onclick=()=>go(id);n.appendChild(a);return a;};
  add('live','Live');add('motion','Motion');add('upload','Upload');
  n.appendChild(document.createElement('hr'));
  for(const [id,label] of SECTIONS)add(id,label);
  n.appendChild(document.createElement('hr'));
  add('maint','Maintenance');
}
function videoOn(el){el.src='/video.mjpg?'+Date.now();}
function videoOff(el){el.removeAttribute('src');}
function go(id){
  if(cur===id)return;
  videoOff($('livevid'));videoOff($('camimg'));      // free the camera connection
  stopPlay();
  if(es){es.close();es=null;}
  cur=id;location.hash=id;
  document.querySelectorAll('#nav a').forEach(a=>a.classList.toggle('on',a.dataset.id===id));
  document.querySelectorAll('main > section').forEach(s=>s.hidden=true);
  if(id==='live'){$('sec-live').hidden=false;videoOn($('livevid'));}
  else if(id==='motion'){$('sec-motion').hidden=false;snapTried=false;videoOn($('camimg'));if(sel)live(sel);}
  else if(id==='upload'){$('sec-upload').hidden=false;loadUploads();}
  else if(id==='maint'){$('sec-maint').hidden=false;}
  else{$('sec-params').hidden=false;renderParams(id);}
}
$('camimg').onerror=()=>{if(!snapTried){snapTried=true;$('camimg').src='/snapshot.jpg?'+Date.now();}};

/* ---------------------------------------------------------------- motion windows */
function render(s){
  st=s;
  if(s.params){P=s.params;RISK=new Set(s.risky||[]);}
  stage.querySelectorAll('.win').forEach(e=>e.remove());
  if(!(sel in s.motion))sel=Object.keys(s.motion)[0]||null;
  for(const [id,w] of Object.entries(s.motion)){
    const d=document.createElement('div');
    d.className='win '+w.WindowType+(id===sel?' sel':'');d.dataset.id=id;
    d.style.left=px(+w.Left,W)+'px';d.style.top=px(+w.Top,H)+'px';
    d.style.width=px(w.Right-w.Left,W)+'px';d.style.height=px(w.Bottom-w.Top,H)+'px';
    d.innerHTML='<span class="name"></span><div class="grip"></div>';
    d.firstChild.textContent=id+' '+w.Name;
    stage.appendChild(d);
  }
  fillPanel();fillTarget();
}
function fillPanel(){
  const w=sel&&st.motion[sel];
  $('selname').textContent=sel||'–';
  if(!w)return;
  $('Name').value=w.Name;$('WindowType').value=w.WindowType;
  for(const k of ['ObjectSize','History','Sensitivity']){$(k).value=w[k];$(k).nextElementSibling.textContent=w[k];}
}
function select(id){
  sel=id;
  stage.querySelectorAll('.win').forEach(e=>e.classList.toggle('sel',e.dataset.id===id));
  fillPanel();live(id);
}
function live(id){
  if(es)es.close();
  es=new EventSource('/api/motionlevel?group='+id.slice(1));
  es.onmessage=e=>{const [l,t]=e.data.split(' ').map(Number);
    $('lvl').style.width=Math.min(l,100)+'%';$('thr').style.left=Math.min(t,100)+'%';
    $('lvl').style.background=l>=t?'#c00':'#0a0';};
  // no auto-reconnect: every retry would be a new camera connection
  es.onerror=()=>{es.close();status('Live level interrupted - click the window to reconnect.',true);};
}
stage.onpointerdown=e=>{
  const el=e.target.closest('.win');if(!el)return;
  if(el.dataset.id!==sel)select(el.dataset.id);else if(!es||es.readyState===2)live(sel);
  const grip=e.target.classList.contains('grip');
  const x0=e.clientX,y0=e.clientY,L=el.offsetLeft,T=el.offsetTop,Wd=el.offsetWidth,Ht=el.offsetHeight;
  const cl=(v,a,b)=>Math.max(a,Math.min(b,v));
  stage.setPointerCapture(e.pointerId);
  stage.onpointermove=m=>{
    const dx=m.clientX-x0,dy=m.clientY-y0;
    if(grip){el.style.width=cl(Wd+dx,16,W-L)+'px';el.style.height=cl(Ht+dy,16,H-T)+'px';}
    else{el.style.left=cl(L+dx,0,W-Wd)+'px';el.style.top=cl(T+dy,0,H-Ht)+'px';}
  };
  stage.onpointerup=()=>{stage.onpointermove=stage.onpointerup=null;};
  e.preventDefault();
};
function rectOf(id){
  const el=stage.querySelector('.win[data-id="'+id+'"]');
  return {Left:un(el.offsetLeft,W),Top:un(el.offsetTop,H),Right:un(el.offsetLeft+el.offsetWidth,W),Bottom:un(el.offsetTop+el.offsetHeight,H)};
}
const vals=()=>({Name:$('Name').value,WindowType:$('WindowType').value,ObjectSize:+$('ObjectSize').value,History:+$('History').value,Sensitivity:+$('Sensitivity').value});
$('save').onclick=async()=>{
  if(!sel)return;
  if(!$('Name').checkValidity()||!$('Name').value)return status('Name: only A-Z, 0-9, space, _ and -.',true);
  const s=await api('PUT','/api/motion/'+sel,{...rectOf(sel),...vals()});if(s)render(s);
};
$('neu').onclick=async()=>{
  const s=await api('POST','/api/motion',{Name:'Window',WindowType:'include',Sensitivity:80,History:90,ObjectSize:35,Left:2500,Top:2500,Right:7500,Bottom:7500});
  if(s){render(s);select(s.added);}
};
$('del').onclick=async()=>{
  if(!sel||!confirm('Delete '+sel+'?'))return;
  const s=await api('DELETE','/api/motion/'+sel);if(s){sel=null;render(s);if(sel)select(sel);}
};
$('reload').onclick=load;

/* ---------------------------------------------------------------- upload target */
function fillTarget(){
  const e=st.event,srv=Object.keys(st.servers)[0];
  const act=Object.entries(st.actions).find(([a,v])=>v.Type==='U'&&v.Protocol==='HTTP');
  if(srv)$('url').value=st.servers[srv].Address;
  $('fileformat').value=e.FileFormat==='mp4'?'mp4':'jpg';
  $('pre').value=e.IncludePreTrigger==='yes'?e.PreTriggerSize:0;
  $('post').value=e.IncludePostTrigger==='yes'?e.PostTriggerSize:0;
  $('min_interval').value=(e.MinimumTriggerInterval||'0').split(':').reduce((a,b)=>a*60+ +b,0);
  $('enabled').checked=e.Enabled==='yes';
  $('target').textContent=act
    ?'Camera: upload action '+act[0]+' -> server '+act[1].Server+(srv?' ('+st.servers[srv].Address+')':'')+', format '+e.FileFormat+', event '+(e.Enabled==='yes'?'enabled':'disabled')+', trigger '+e.SWInput
    :'The camera has no HTTP upload action yet. Save to create the server and action.';
}
const target=()=>({url:$('url').value,fileformat:$('fileformat').value,pre:+$('pre').value,post:+$('post').value,min_interval:+$('min_interval').value,enabled:$('enabled').checked});
$('savet').onclick=async()=>{
  // Changing the format is easy to do by accident and quietly changes what arrives.
  const now=st.event.FileFormat==='mp4'?'mp4':'jpg', want=$('fileformat').value;
  if(now!==want&&!confirm('Change the upload format from '+now+' to '+want+'?\n\n'
      +'jpg uploads a burst of still images per event, mp4 uploads one video clip.'))return;
  const s=await api('PUT','/api/upload-target',target());if(s)render(s);};
$('test').onclick=async()=>{const j=await api('POST','/api/upload-target/test',{url:$('url').value});if(j)status((j.ok?'Test OK: ':'Test failed: ')+j.text,!j.ok);};
$('trig').onclick=async()=>{const j=await api('POST','/api/trigger');if(j)status('Event triggered. Images appear in the list within a few seconds.');};
const PAGE=15;
let evOffset=0, evTotal=0, evShown=0, EV=[], evSel=-1, imgIdx=0, play=null;
async function loadUploads(){
  const j=await (await fetch('/api/events?offset='+evOffset+'&limit='+PAGE)).json();
  evTotal=j.total; evShown=j.items.length; EV=j.items;
  const t=$('events');t.innerHTML='';
  if(!EV.length){t.innerHTML='<tr><td>No events in the upload directory yet.</td></tr>';}
  EV.forEach((e,i)=>{const tr=t.insertRow();
    tr.onclick=()=>selectEvent(i);
    if(i===evSel)tr.className='sel';
    tr.insertCell().textContent=e.start;
    tr.insertCell().textContent=e.files.length+(e.files.length===1?' image':' images');
    tr.insertCell().textContent=e.seconds+' s';
    tr.insertCell().textContent=(e.bytes/1024).toFixed(0)+' kB';});
  $('evpage').textContent=j.total?(j.offset+1)+'-'+(j.offset+evShown)+' of '+j.total:'';
  $('evprev').disabled=evOffset===0;
  $('evnext').disabled=evOffset+evShown>=evTotal;
  if(evSel>=EV.length){evSel=-1;$('viewer').hidden=true;stopPlay();}
}
$('evprev').onclick=()=>{if(evOffset===0)return;evOffset=Math.max(0,evOffset-PAGE);evSel=-1;$('viewer').hidden=true;stopPlay();loadUploads();};
$('evnext').onclick=()=>{if(evOffset+evShown>=evTotal)return;evOffset+=PAGE;evSel=-1;$('viewer').hidden=true;stopPlay();loadUploads();};

function selectEvent(i){
  stopPlay();
  evSel=i;imgIdx=0;
  document.querySelectorAll('#events tr').forEach((tr,n)=>tr.className=n===i?'sel':'');
  $('viewer').hidden=false;
  showImage();
}
function showImage(){
  const e=EV[evSel];if(!e)return;
  const name=e.files[imgIdx];
  const url='/uploads/'+encodeURIComponent(name);
  $('shotimg').src=url;
  $('imglink').href=url;
  $('imgpos').textContent=(imgIdx+1)+' / '+e.files.length+'  '+name;
  $('imgprev').disabled=imgIdx===0;
  $('imgnext').disabled=imgIdx>=e.files.length-1;
}
function step(d){
  const e=EV[evSel];if(!e)return;
  imgIdx=(imgIdx+d+e.files.length)%e.files.length;
  showImage();
}
$('imgprev').onclick=()=>{stopPlay();step(-1);};
$('imgnext').onclick=()=>{stopPlay();step(1);};
function stopPlay(){if(play){clearInterval(play);play=null;$('imgplay').textContent='Play';}}
$('imgplay').onclick=()=>{
  if(play){stopPlay();return;}
  if(evSel<0)return;
  $('imgplay').textContent='Stop';
  play=setInterval(()=>step(1),700);   // loops; prev/next or leaving the section stops it
};

/* ---------------------------------------------------------------- generic parameters */
const READONLY=/^root\.(Properties|Brand)\./;
let DEFS=null;
async function ensureDefs(){
  if(DEFS)return true;
  const j=await api('GET','/api/defs');   // fixed per firmware, fetched once
  if(!j)return false;
  DEFS=j;return true;
}
function field(k){
  const v=P[k], d=(DEFS&&DEFS[k])||{}, leaf=k.slice(k.lastIndexOf('.')+1);
  const pw=d.kind==='password'||/Password$/.test(k);
  const lab=document.createElement('label');
  const sp=document.createElement('span');sp.textContent=d.nice||leaf;sp.title=k;
  let inp;
  if(d.kind==='enum'&&d.values){
    inp=document.createElement('select');
    for(const [val,nice] of d.values){
      const o=document.createElement('option');o.value=val;o.textContent=nice;inp.appendChild(o);}
    if(!d.values.some(x=>x[0]===v)){   // keep an out-of-range current value visible
      const o=document.createElement('option');o.value=v;o.textContent=v+' (current)';inp.appendChild(o);}
    inp.value=v;
  }else if(d.kind==='bool'){
    inp=document.createElement('input');inp.type='checkbox';inp.checked=v===d.true;
    inp.dataset.true=d.true;inp.dataset.false=d.false;   // not always yes/no
  }else{
    inp=document.createElement('input');
    if(pw){inp.type='password';inp.value=v;inp.size=24;}
    else if(d.kind==='int'||(!d.kind&&/^\d+$/.test(v))){
      inp.type='number';inp.value=v;
      if(d.min!==undefined)inp.min=d.min;
      if(d.max!==undefined)inp.max=d.max;
    }
    else if(v==='yes'||v==='no'){inp.type='checkbox';inp.checked=v==='yes';}
    else{inp.type='text';inp.value=v;inp.size=30;if(d.maxlen)inp.maxLength=d.maxlen;}
  }
  inp.dataset.key=k;
  if(READONLY.test(k))inp.disabled=true;
  lab.append(sp,inp);
  if(d.kind==='int'&&d.min!==undefined&&d.max!==undefined){
    const s=document.createElement('small');s.textContent=' '+d.min+'–'+d.max;lab.appendChild(s);}
  if(pw){const b=document.createElement('button');b.type='button';b.className='mini';b.textContent='show';
    b.onclick=()=>{inp.type=inp.type==='password'?'text':'password';};lab.appendChild(b);}
  if(RISK.has(k))lab.classList.add('risk');
  return lab;
}
async function renderParams(id){
  const sec=SECTIONS.find(s=>s[0]===id);if(!sec||!P)return;
  if(!await ensureDefs())return;
  $('ptitle').textContent=sec[1];
  const keys=Object.keys(P).filter(k=>sec[2].includes(k.split('.')[1])).sort();
  const by={};
  for(const k of keys){const path=k.slice(5,k.lastIndexOf('.'));(by[path]=by[path]||[]).push(k);}
  const paths=Object.keys(by).sort();
  const box=$('params');box.innerHTML='';
  for(const path of paths){
    const d=document.createElement('details');d.open=paths.length<=4;
    const s=document.createElement('summary');
    s.textContent=path.split('.').join(' · ')+' ('+by[path].length+')';
    d.appendChild(s);
    for(const k of by[path])d.appendChild(field(k));
    box.appendChild(d);
  }
  $('pcount').textContent=keys.length+' parameters';
}
function changedParams(){
  const out={};
  document.querySelectorAll('#params [data-key]').forEach(i=>{
    if(i.disabled)return;
    const k=i.dataset.key;
    const v=i.type==='checkbox'?(i.checked?(i.dataset.true||'yes'):(i.dataset.false||'no')):i.value;
    if(v!==P[k])out[k]=v;
  });
  return out;
}
$('psave').onclick=async()=>{
  const ch=changedParams(), keys=Object.keys(ch);
  if(!keys.length)return status('Nothing changed.');
  const risky=keys.filter(k=>RISK.has(k));
  if(risky.length&&!confirm('These settings can make the camera unreachable:\n\n'+risky.join('\n')+'\n\nSave anyway?'))return;
  const s=await api('PUT','/api/params',ch);
  if(s){render(s);renderParams(cur);status(keys.length+' parameters saved.');}
};
$('preload').onclick=async()=>{const s=await api('GET','/api/state');if(s){render(s);renderParams(cur);}};

/* ---------------------------------------------------------------- maintenance */
async function showText(url,label){
  status('Fetching '+label+'…');
  document.querySelectorAll('button').forEach(b=>b.disabled=true);
  try{$('mout').textContent=await (await fetch(url)).text();status('Done');}
  catch(e){status('Error: '+e.message,true);}
  finally{document.querySelectorAll('button').forEach(b=>b.disabled=false);}
}
$('mlog').onclick=()=>showText('/api/systemlog','system log');
$('mrep').onclick=()=>showText('/api/serverreport','server report');
$('mrestart').onclick=async()=>{
  if(!confirm('Restart the camera? It will be unavailable for about a minute.'))return;
  if(await api('POST','/api/action/restart'))status('Restart requested. Wait about a minute, then press Reload.');
};
$('mfact').onclick=async()=>{
  if(!confirm('Factory reset the camera? All configuration except network settings is erased, including motion windows and upload settings.'))return;
  if(await api('POST','/api/action/factorydefault'))status('Factory reset requested.');
};

/* ---------------------------------------------------------------- startup */
async function load(){
  const s=await api('GET','/api/state');
  if(s){render(s);if(cur&&SECTIONS.some(x=>x[0]===cur))renderParams(cur);
        if(cur==='motion'&&sel&&(!es||es.readyState===2))live(sel);}
}
buildNav();
load().then(()=>go(location.hash.slice(1)||'live'));
// only refresh the newest page, and never while paging or playing back
setInterval(()=>{if(cur==='upload'&&evOffset===0&&!play&&evSel<0)loadUploads();},5000);
</script>
"""

upload_server = UploadServer(("0.0.0.0", UPLOAD_PORT), UploadHandler)
threading.Thread(target=upload_server.serve_forever, daemon=True).start()
print(f"upload receiver on :{UPLOAD_PORT}, web interface on :{PORT}")
app.run(host="0.0.0.0", port=PORT, threaded=True)
