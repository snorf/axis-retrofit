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
import collections
import datetime
import json
import os
import re
import socket
import sys
import threading
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

import requests
from flask import Flask, Response, jsonify, request, send_from_directory
from requests.auth import HTTPBasicAuth

sys.stdout.reconfigure(line_buffering=True)  # so print() reaches a log file / journal promptly

HOST = os.environ.get("AXIS_HOST", "")
UPLOAD_DIR = os.path.abspath(os.environ.get("UPLOAD_DIR", "./uploads"))
PORT = int(os.environ.get("PORT", "6001"))  # not 6000: browsers refuse it as an unsafe port
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
    ("image", "Bild", ["Image", "ImageSource"]),
    ("network", "Nätverk", ["Network", "SOCKS", "HTTPS", "SNMP", "Bandwidth"]),
    ("time", "Tid", ["Time"]),
    ("audio", "Ljud", ["Audio", "AudioSource"]),
    ("notify", "Notifiering", ["Notify", "SMTP", "MailLogd"]),
    ("layout", "Live View", ["Layout"]),
    ("events", "Händelser (rå)", ["Event", "EventServers", "Motion"]),
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
            raise CameraError(f"{path} svarade HTTP {r.status_code}")
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
uploads = collections.deque(maxlen=50)


# --------------------------------------------------------------------- upload receiver

def save_upload(ts, filename, data):
    name = f"{ts}-{os.path.basename(filename or 'image.jpg')}"
    with open(os.path.join(UPLOAD_DIR, name), "wb") as f:
        f.write(data)
    uploads.appendleft({"ts": ts, "name": name, "size": len(data)})
    return name


@app.route("/upload", methods=["POST", "GET"])
def upload():
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    saved = [save_upload(ts, request.files[k].filename, request.files[k].read())
             for k in request.files]
    if not saved:  # the camera posts a raw image body with the name in Content-Disposition
        body = request.get_data()
        if body:
            m = re.search(r'filename="([^"]+)"', request.headers.get("Content-Disposition", ""))
            saved.append(save_upload(ts, m.group(1) if m else "image.jpg", body))
    print(request.method, "/upload from", request.remote_addr, "saved:", saved)
    return "OK", 200


@app.get("/api/uploads")
def api_uploads():
    return jsonify(list(uploads))


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
    return jsonify(error=f"Kameran svarar inte ({type(e).__name__})"), 504


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
        where = f"{where}:{PORT}"
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
        return jsonify(error=f"Ogiltig parameternyckel: {bad[0]}"), 400
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
        return jsonify(error="Ogiltigt fönster"), 400
    cam.update(window_fields(f"root.Motion.{idx}"))
    return jsonify(state())


@app.delete("/api/motion/<idx>")
def motion_delete(idx):
    if not re.fullmatch(r"M\d", idx):
        return jsonify(error="Ogiltigt fönster"), 400
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
        return jsonify(error="Okänd åtgärd"), 400
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
 details{border:1px solid #ddd;border-radius:4px;margin:6px 0;padding:4px 10px;background:#fff}
 summary{cursor:pointer;font-weight:600;padding:3px 0}
 label.risk span{color:#a30}
 label.risk::after{content:" ⚠";color:#a30}
 pre{background:#fff;border:1px solid #ddd;padding:10px;max-height:60vh;overflow:auto;white-space:pre-wrap}
</style>
<div id="status">Laddar…</div>
<div id="wrap">
<nav id="nav"></nav>
<main>

<section id="sec-live" hidden>
 <h2>Live</h2>
 <img id="livevid" alt="">
 <p><small>Strömmen hämtas från kamerans MJPEG-utgång och stängs när du byter sektion.</small></p>
</section>

<section id="sec-motion" hidden>
 <h2>Motion-fönster</h2>
 <div class="row">
  <div>
   <div id="stage"><img id="camimg" alt=""></div>
   <div id="bar"><div id="lvl"></div><div id="thr"></div></div>
   <small>Live-nivå för valt fönster. Röd = över tröskeln (Object size).</small>
  </div>
  <fieldset id="panel">
   <legend>Valt fönster: <span id="selname">–</span></legend>
   <label><span>Namn</span><input id="Name" pattern="[A-Za-z0-9 _-]+" size="18"></label>
   <label><span>Typ</span><select id="WindowType"><option value="include">Include</option><option value="exclude">Exclude</option></select></label>
   <label><span>Object size</span><input type="range" id="ObjectSize" min="0" max="100" oninput="this.nextElementSibling.textContent=this.value"><output></output></label>
   <label><span>History</span><input type="range" id="History" min="0" max="100" oninput="this.nextElementSibling.textContent=this.value"><output></output></label>
   <label><span>Sensitivity</span><input type="range" id="Sensitivity" min="0" max="100" oninput="this.nextElementSibling.textContent=this.value"><output></output></label>
   <small>Rekommenderat: Object size 5–15, History 60–90, Sensitivity 75–95. Ligger live-nivån över tröskeln med tomt rum triggar kameran på brus.</small><br>
   <button id="save">Spara</button><button id="neu">Nytt fönster</button><button id="del">Ta bort</button><button id="reload">Läs om</button>
  </fieldset>
 </div>
</section>

<section id="sec-upload" hidden>
 <h2>HTTP-upload vid motion</h2>
 <fieldset>
  <label><span>URL</span><input id="url" size="40" value="__UPLOAD_URL__"></label>
  <label><span>Format</span><select id="fileformat"><option value="jpg">JPEG-bilder</option><option value="mp4">MP4-klipp</option></select></label>
  <label><span>Före trigger</span><input id="pre" type="number" min="0" max="30"> <small>bilder (1/s), för MP4 sekunder</small></label>
  <label><span>Efter trigger</span><input id="post" type="number" min="0" max="30"> <small>bilder (1/s), för MP4 sekunder</small></label>
  <label><span>Min-intervall (s)</span><input id="min_interval" type="number" min="0"></label>
  <label><span>Aktiverad</span><input id="enabled" type="checkbox"></label>
  <div id="target"></div>
  <button id="test">Testa anslutning</button><button id="savet">Spara</button><button id="trig">Trigga event nu</button>
 </fieldset>
 <h2 style="margin-top:18px">Mottagna uploads</h2>
 <table id="uploads"></table>
</section>

<section id="sec-params" hidden>
 <h2 id="ptitle"></h2>
 <div id="params"></div>
 <button id="psave">Spara ändrade</button><button id="preload">Läs om</button>
 <span id="pcount"></span>
</section>

<section id="sec-maint" hidden>
 <h2>Underhåll</h2>
 <p>
  <button id="mlog">Systemlogg</button>
  <button id="mrep">Serverrapport</button>
  <a href="/api/backup">Hämta backup</a>
 </p>
 <pre id="mout">Serverrapporten innehåller kamerans lösenord i klartext. Dela den inte.</pre>
 <p>
  <button id="mrestart">Starta om kameran</button>
  <button id="mfact">Fabriksåterställ</button>
  <small>Fabriksåterställning behåller nätverksinställningarna. Återställning från backup görs i kamerans egen sida.</small>
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
  status('Pratar med kameran… (kan ta upp till 70 s)');
  document.querySelectorAll('button').forEach(b=>b.disabled=true);
  try{
    const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:body&&JSON.stringify(body)});
    const j=await r.json();
    if(!r.ok){if(st)render(st);status('Fel: '+j.error+' – visar senast bekräftade läge. "Läs om" hämtar kamerans.',true);return null;}
    status('Klart');return j;
  }catch(e){status('Fel: '+e.message,true);return null;}
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
  add('maint','Underhåll');
}
function videoOn(el){el.src='/video.mjpg?'+Date.now();}
function videoOff(el){el.removeAttribute('src');}
function go(id){
  if(cur===id)return;
  videoOff($('livevid'));videoOff($('camimg'));      // free the camera connection
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
  es.onerror=()=>{es.close();status('Live-nivå avbruten – klicka på fönstret för att återansluta.',true);};
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
  if(!$('Name').checkValidity()||!$('Name').value)return status('Namn: bara A–Z, 0–9, mellanslag, _ och -.',true);
  const s=await api('PUT','/api/motion/'+sel,{...rectOf(sel),...vals()});if(s)render(s);
};
$('neu').onclick=async()=>{
  const s=await api('POST','/api/motion',{Name:'Window',WindowType:'include',Sensitivity:80,History:90,ObjectSize:35,Left:2500,Top:2500,Right:7500,Bottom:7500});
  if(s){render(s);select(s.added);}
};
$('del').onclick=async()=>{
  if(!sel||!confirm('Ta bort '+sel+'?'))return;
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
    ?'Kameran: upload-action '+act[0]+' → server '+act[1].Server+(srv?' ('+st.servers[srv].Address+')':'')+', event '+(e.Enabled==='yes'?'aktiverat':'avstängt')+', trigger '+e.SWInput
    :'Kameran har ingen HTTP-upload-action ännu. Spara för att skapa server + action.';
}
const target=()=>({url:$('url').value,fileformat:$('fileformat').value,pre:+$('pre').value,post:+$('post').value,min_interval:+$('min_interval').value,enabled:$('enabled').checked});
$('savet').onclick=async()=>{const s=await api('PUT','/api/upload-target',target());if(s)render(s);};
$('test').onclick=async()=>{const j=await api('POST','/api/upload-target/test',{url:$('url').value});if(j)status((j.ok?'Test OK: ':'Test misslyckades: ')+j.text,!j.ok);};
$('trig').onclick=async()=>{const j=await api('POST','/api/trigger');if(j)status('Event triggat. Bilderna dyker upp i listan inom några sekunder.');};
async function loadUploads(){
  const u=await (await fetch('/api/uploads')).json();
  const t=$('uploads');t.innerHTML='';
  if(!u.length){t.innerHTML='<tr><td>Inga uploads mottagna sedan start.</td></tr>';return;}
  for(const x of u){const tr=t.insertRow();
    tr.insertCell().textContent=x.ts;
    const a=document.createElement('a');a.href='/uploads/'+encodeURIComponent(x.name);a.target='_blank';a.textContent=x.name;
    tr.insertCell().appendChild(a);
    tr.insertCell().textContent=x.size+' B';}
}

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
      const o=document.createElement('option');o.value=v;o.textContent=v+' (nuvarande)';inp.appendChild(o);}
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
  if(pw){const b=document.createElement('button');b.type='button';b.className='mini';b.textContent='visa';
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
  $('pcount').textContent=keys.length+' parametrar';
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
  if(!keys.length)return status('Inget ändrat.');
  const risky=keys.filter(k=>RISK.has(k));
  if(risky.length&&!confirm('Följande kan göra kameran oåtkomlig:\n\n'+risky.join('\n')+'\n\nSpara ändå?'))return;
  const s=await api('PUT','/api/params',ch);
  if(s){render(s);renderParams(cur);status(keys.length+' parametrar sparade.');}
};
$('preload').onclick=async()=>{const s=await api('GET','/api/state');if(s){render(s);renderParams(cur);}};

/* ---------------------------------------------------------------- maintenance */
async function showText(url,label){
  status('Hämtar '+label+'…');
  document.querySelectorAll('button').forEach(b=>b.disabled=true);
  try{$('mout').textContent=await (await fetch(url)).text();status('Klart');}
  catch(e){status('Fel: '+e.message,true);}
  finally{document.querySelectorAll('button').forEach(b=>b.disabled=false);}
}
$('mlog').onclick=()=>showText('/api/systemlog','systemlogg');
$('mrep').onclick=()=>showText('/api/serverreport','serverrapport');
$('mrestart').onclick=async()=>{
  if(!confirm('Starta om kameran? Den är otillgänglig i ungefär en minut.'))return;
  if(await api('POST','/api/action/restart'))status('Omstart beordrad. Vänta ungefär en minut och tryck Läs om.');
};
$('mfact').onclick=async()=>{
  if(!confirm('Fabriksåterställ kameran? All konfiguration utom nätverksinställningar raderas, inklusive motion-fönster och upload-inställningar.'))return;
  if(await api('POST','/api/action/factorydefault'))status('Fabriksåterställning beordrad.');
};

/* ---------------------------------------------------------------- startup */
async function load(){
  const s=await api('GET','/api/state');
  if(s){render(s);if(cur&&SECTIONS.some(x=>x[0]===cur))renderParams(cur);
        if(cur==='motion'&&sel&&(!es||es.readyState===2))live(sel);}
}
buildNav();
load().then(()=>go(location.hash.slice(1)||'live'));
setInterval(()=>{if(cur==='upload')loadUploads();},5000);
</script>
"""

app.run(host="0.0.0.0", port=PORT, threaded=True)
