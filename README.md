# axis.py — a modern local GUI for old AXIS network cameras

A single-file replacement web interface for AXIS network cameras running firmware 4.x
(VAPIX 2), plus a receiver for the images the camera uploads when it detects motion.

Cameras of this generation are still perfectly good hardware, but their built-in web
interface no longer works: motion detection needs a Java applet, and live video needs
ActiveX or a Java applet. No current browser runs either. This talks to the camera's CGI
endpoints directly instead, so everything works in a normal browser again.

## What it does

- **Live video.** The camera's MJPEG stream, proxied into a plain `<img>` tag. No plugin.
- **Motion detection windows.** Drag and resize the detection rectangles directly on the
  live image, with sliders for object size, history and sensitivity, and a live activity
  bar showing the measured level against the trigger threshold.
- **Motion upload.** Point the camera at this server and it posts JPEG images (or MP4
  clips) on every motion event. Received files are listed and viewable in the GUI.
- **Every camera setting.** All parameters the camera exposes, grouped into sections in a
  left-hand menu, editable and saved back in a single request. The form is built from the
  camera's own parameter schema, so enumerated settings are dropdowns with exactly the
  values that camera accepts, numbers carry their real minimum and maximum, and every field
  is labelled with the camera's own name for it. Nothing about the parameter set is
  hardcoded, so it adapts to whatever model it is pointed at.
- **Maintenance.** System log, server report, configuration backup, restart, factory reset.
- **Test trigger.** Fire a motion event from the GUI using the camera's virtual web-button
  input, so you can test the whole chain without walking to the camera. It holds the input
  high for six seconds, because a shorter pulse yields pre-trigger frames but no post-trigger
  ones, and an instantaneous one is missed entirely.

## Requirements

- Python 3.9 or newer
- `flask` and `requests` — that is the entire dependency list
- An AXIS camera with firmware 4.x and an account with administrator rights

## Running it

Put the camera credentials in a file, one line, `username:password`:

```sh
echo 'root:yourpassword' > .axis-creds
chmod 600 .axis-creds
```

Then start it:

```sh
AXIS_HOST=192.0.2.10 python3 axis.py
```

Open `http://localhost:6001/`.

### Configuration

All configuration is environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `AXIS_HOST` | required | Camera hostname or IP |
| `AXIS_CREDS` | `.axis-creds` | File containing `username:password` |
| `UPLOAD_DIR` | `./uploads` | Where uploaded images are written |
| `PORT` | `6001` | Port this server listens on |
| `ADVERTISE_HOST` | detected | Address the camera uploads to, `host` or `host:port` |

Do not use port 6000. Chrome, Edge and Firefox all refuse to open it, because it is on
their blocked-port list.

`ADVERTISE_HOST` is only needed when this server cannot be reached on the address it sees
itself as having, which happens behind NAT or a reverse proxy. On a normal LAN it is
detected from the route to the camera and you can leave it unset.

## Deploying it

Run it as a service rather than from a shell, so it survives reboots and crashes. There is
a systemd unit in `axis.service` and a matching environment file in `axis.env.example`.

```sh
adduser --system --group --home /opt/axis axis
install -d -o axis -g axis /opt/axis /srv/axis-uploads
install -o axis -g axis axis.py /opt/axis/
printf 'root:yourpassword\n' > /opt/axis/.axis-creds
chown axis:axis /opt/axis/.axis-creds && chmod 600 /opt/axis/.axis-creds
cp axis.env.example /etc/axis.env   # then edit AXIS_HOST and UPLOAD_DIR
cp axis.service /etc/systemd/system/
systemctl enable --now axis
```

The camera opens connections *to* this server, so it needs to be reachable from the camera's
network on `PORT`. Give the container or VM an address on the same LAN as the camera. If you
run it in a container with a bridged or NAT network instead, publish the port and set
`ADVERTISE_HOST` to the host's LAN address, otherwise the upload URL offered in the interface
will be an address the camera cannot reach.

Put `UPLOAD_DIR` on its own volume rather than the root filesystem. Images are small, around
19 KB each and six per event, so ordinary use is well under a megabyte a day. A camera that
is triggering on sensor noise instead writes about 150 MB a day, which fills a small root
filesystem quickly. There is no retention policy yet: nothing deletes old images.

### Setting up motion upload

Open the **Upload** section. The URL field is pre-filled with the address the camera can
reach this server on. Press **Test connection** to have the camera verify it can reach you,
then **Save**. That creates the HTTP event server and the upload action on the camera, and
enables the event. **Trigger event now** fires a test event.

Choose **JPEG images** unless you want video. With **MP4 clips** the camera uploads one
recording per event instead of a burst of stills, which is easy to select by accident and
then looks like nothing is arriving if you are watching for images.

## Things worth knowing about this hardware

**Connections to the camera can take 35 or 67 seconds.** Firmware 4.x runs Linux 2.6.23
with `tcp_tw_recycle` enabled, which silently drops SYN packets for up to 60 seconds after
a closed connection if the client's TCP timestamp is lower than the previous one. Modern
clients randomise that timestamp per connection, so roughly every second connection stalls
until a SYN retransmit gets through. This is a camera-side bug that cannot be fixed from
here, so the code minimises the number of connections, serialises them, and uses a long
connect timeout. You can avoid it entirely on the client side by disabling TCP timestamps
toward the camera:

```sh
# macOS (until reboot)
sudo sysctl -w net.inet.tcp.rfc1323=0
# Linux
iptables -t mangle -A OUTPUT -d <camera-ip> -p tcp --syn -j TCPOPTSTRIP --strip-options timestamp
```

**The image format parameter is `jpg`, not `jpeg`.** The camera accepts `jpeg` without
complaint and then never uploads anything.

**Uploads are not multipart.** The camera posts the raw image as the request body with
`Content-Type: image/jpeg` and the filename in `Content-Disposition`. A receiver that only
looks for multipart form files will silently store nothing.

**Noise triggers motion.** If the measured activity level sits above the threshold with an
empty room, the camera will upload continuously. Lowering sensitivity usually helps more
than raising object size, because it reduces the measured level rather than moving the
line above it.

## Security

- **The camera stores passwords in cleartext**, including SMTP credentials, and hands them
  out through the parameter list and the server report to anyone who can authenticate.
  Treat the server report as a secret and do not paste it anywhere.
- **Do not expose this server to the internet.** It has no authentication of its own and it
  holds your camera credentials.
- Parameters that can make the camera unreachable, such as the IP address, boot protocol and
  web server port, are marked in the interface and require an extra confirmation. They are
  flagged, not blocked, so you can still move the camera to a new address.

## Tested on

| Model | Firmware | Status |
|---|---|---|
| AXIS 207W | 4.44.2 | Working, all features |

Other firmware 4.x cameras use the same VAPIX 2 API and should work, but none have been
tested. Reports of what works and what does not are very welcome, especially the model,
firmware version, and any parameter groups that behave differently.

## Not implemented

Deliberately left out, and good places to contribute:

- Restoring a configuration backup (`restore.cgi`). Backup download works; restore is done
  in the camera's own page.
- Hard factory default (`hardfactorydefault.cgi`), which erases the network configuration
  and makes the camera unreachable.
- Adding and removing camera users (`pwdgrp.cgi`).
- Audio transmit and receive.
- Multiple cameras. One instance talks to one camera, set by `AXIS_HOST`. Running several
  instances on different ports works today; a single instance with a camera picker does not.
- Translations. The interface is English only, with no i18n layer. One would be welcome if
  anyone actually needs it.

## License

MIT. See `LICENSE`.
