# A container image for axis.py. Not how this is run in anger — that is systemd in an LXC —
# but the shortest path from finding this repo to having it running.
#
# Run it with host networking:
#
#   docker run --network host -e AXIS_HOST=192.0.2.10 ... axis-retrofit
#
# The address the camera is told to upload to is derived from the route to the camera, so in a
# bridged container it comes out as the container's internal address, which the camera cannot
# reach, and uploads silently never arrive. ADVERTISE_HOST exists for the bridged case; see
# the README.
FROM python:3.13-slim

# Loose upper bounds: a future major release cannot quietly break the image, and no patch
# versions are frozen for someone else to maintain.
#
# Pillow is optional in axis.py, imported inside the function that builds a notification. It
# is included here because without it a notification carries a single JPEG rather than an
# animated GIF, and in an image it costs a few megabytes. The promise of two dependencies is
# about the source file, not about what a convenience image ships.
RUN pip install --no-cache-dir "flask>=3,<4" "requests>=2,<3" "pillow>=10,<13"

# Only this one file, and never COPY . . — a working copy of this repo has .axis-creds sitting
# in it, and baking real camera credentials into an image someone might push to a registry
# would be a gift to whoever pulls it. .dockerignore is the second line of defence.
COPY axis.py /opt/axis/axis.py

ENV UPLOAD_DIR=/srv/axis-uploads \
    PORT=6001 \
    AXIS_CREDS=/run/secrets/axis-creds

# Nothing here needs root. The uploads directory is the only thing written to.
RUN useradd --system --create-home --home-dir /home/axis axis \
 && mkdir -p /srv/axis-uploads \
 && chown axis:axis /srv/axis-uploads
VOLUME /srv/axis-uploads

WORKDIR /opt/axis
USER axis

# 6001 is the web interface, 6002 the listener the camera posts images to. The camera opens
# connections *to* this process, so both have to be reachable from the camera's network.
EXPOSE 6001 6002

# Checks /api/camera-address deliberately: it is the one endpoint that does not talk to the
# camera, so this reports on whether this process is alive. Checking /api/state instead would
# mark the container unhealthy whenever the camera is merely slow — 67 s is normal for this
# hardware — or has changed address.
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '6001') + '/api/camera-address', timeout=5)"]

CMD ["python", "/opt/axis/axis.py"]
