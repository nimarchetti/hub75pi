"""
hub75pi — switchrr-connected bridge: PULL frames → UART → Interstate 75W

Receives 2-part ZMQ PULL frames routed by switchrr:
  [JSON header, raw pixel bytes]
Compresses and sends over UART to the Interstate 75W (hub75rr firmware).

Mode switching input is accepted via a small HTTP server on HUB75PI_HTTP_PORT
(default 5580).  The Interstate 75W firmware (hub75rr) calls this endpoint when
its on-board buttons A/B are pressed, and hub75pi forwards INPUT events to
switchrr's hardware-event PULL socket so switchrr can perform the mode switch.

Environment variables:
  HUB75PI_FRAME_BIND   ZMQ PULL bind address for frames from switchrr
                        (default tcp://0.0.0.0:5565)
  HUB75PI_EVENTS_ADDR  ZMQ PUSH connect address for events to switchrr
                        (default tcp://127.0.0.1:5558)
  HUB75PI_DISPLAY_ID   display_id this bridge serves (default "hub75")
  HUB75PI_MODE_COUNT   number of modes registered for this display (default 4)
  HUB75PI_HTTP_PORT    HTTP server port for button events (default 5580)
  HUB75PI_SERIAL       UART device (default /dev/ttyAMA0)
  HUB75PI_BAUD         UART baud rate (default 2000000)
"""
import json
import os
import struct
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer

import serial
import zmq

FRAME_PULL_BIND  = os.environ.get("HUB75PI_FRAME_BIND",   "tcp://0.0.0.0:5565")
EVENTS_PUSH_ADDR = os.environ.get("HUB75PI_EVENTS_ADDR",  "tcp://127.0.0.1:5558")
DISPLAY_ID       = os.environ.get("HUB75PI_DISPLAY_ID",   "hub75")
MODE_COUNT       = int(os.environ.get("HUB75PI_MODE_COUNT", "4"))
HTTP_PORT        = int(os.environ.get("HUB75PI_HTTP_PORT",  "5580"))
SERIAL_DEV       = os.environ.get("HUB75PI_SERIAL",        "/dev/ttyAMA0")
BAUD             = int(os.environ.get("HUB75PI_BAUD",       "2000000"))

print(f"hub75pi: opening {SERIAL_DEV} at {BAUD} baud")
ser = serial.Serial(SERIAL_DEV, BAUD, timeout=5)

ctx = zmq.Context()

# Receive routed frames from switchrr
pull = ctx.socket(zmq.PULL)
pull.bind(FRAME_PULL_BIND)
print(f"hub75pi: PULL bound at {FRAME_PULL_BIND}")

# Send hardware events (mode-switch inputs) to switchrr
push = ctx.socket(zmq.PUSH)
push.connect(EVENTS_PUSH_ADDR)
print(f"hub75pi: PUSH connected to {EVENTS_PUSH_ADDR}")

# Current mode position, 1-indexed, cycles 1..MODE_COUNT
_mode_position = [1]
_mode_lock = threading.Lock()


def _send_mode_select(position: int) -> None:
    event = json.dumps({
        "event": "INPUT",
        "device": "hub75_btn",
        "type": "select",
        "value": position,
        "display_id": DISPLAY_ID,
    }).encode()
    try:
        push.send(event, zmq.NOBLOCK)
        print(f"hub75pi: sent MODE_SELECT position={position}")
    except zmq.Again:
        print("hub75pi: events push queue full, event dropped")


class _ButtonHandler(BaseHTTPRequestHandler):
    """
    Handles POST /event from hub75rr firmware (Interstate 75W buttons).

    Expected JSON body: {"action": "next"} or {"action": "prev"}

    next → increment mode position (wrapping)
    prev → decrement mode position (wrapping)
    """
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            data = json.loads(body) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {}
        action = data.get("action", "")
        with _mode_lock:
            pos = _mode_position[0]
            if action == "next":
                pos = (pos % MODE_COUNT) + 1
            elif action == "prev":
                pos = ((pos - 2) % MODE_COUNT) + 1
            _mode_position[0] = pos
        _send_mode_select(pos)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass  # suppress per-request HTTP log lines


def _http_server_thread():
    server = HTTPServer(("0.0.0.0", HTTP_PORT), _ButtonHandler)
    print(f"hub75pi: HTTP button server on :{HTTP_PORT}")
    server.serve_forever()


threading.Thread(target=_http_server_thread, daemon=True, name="http-button").start()

last_hash = None
sent = 0
t_start = time.time()

print("hub75pi: ready, waiting for frames from switchrr")
while True:
    parts = pull.recv_multipart()
    if len(parts) != 2:
        print(f"hub75pi: unexpected frame parts={len(parts)}, skipping")
        continue
    try:
        header = json.loads(parts[0])
    except (json.JSONDecodeError, UnicodeDecodeError):
        print("hub75pi: bad frame header, skipping")
        continue
    pixel_bytes = parts[1]

    # Deduplicate identical frames (static screens publish at 30fps)
    h = hash(pixel_bytes)
    if h == last_hash:
        continue
    last_hash = h

    compressed = zlib.compress(pixel_bytes, 1)
    ser.write(b'\xde\xad\xbe\xef' + struct.pack('>I', len(compressed)) + compressed)
    ser.flush()
    sent += 1
    if sent % 30 == 0:
        elapsed = time.time() - t_start
        print(
            f"hub75pi: {sent} frames ({sent/elapsed:.1f} fps), "
            f"last {len(compressed)}B compressed "
            f"({header.get('pixel_format','?')} "
            f"{header.get('width','?')}x{header.get('height','?')})"
        )
