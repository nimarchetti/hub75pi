# HUB75 Display — hub75pi + hub75rr

A stutter-free 256×64 HUB75 LED matrix display for the displayrr departure board, driven by a
Pimoroni Interstate 75W (RP2350). A Pi Zero W acts as a WiFi-to-UART bridge, keeping the
RP2350's memory bus free of WiFi DMA so the HUB75 PIO can run without contention.

## Architecture

```
boardrr (Pi 500)
  ZMQ PUB tcp://:5600
       │
       │  WiFi (ZMQ SUB)
       ▼
Pi Zero W  [hub75pi.service]
  /dev/ttyAMA0  GPIO 14 (TXD)
       │
       │  UART 2 Mbaud, 1 wire
       ▼
Interstate 75W  [hub75rr MicroPython]
  GP17 (RX)  →  deflate decompress  →  viper render  →  HUB75 PIO/DMA
       │
       ▼
256×64 HUB75 panels (two 128×64 panels chained)
```

### Why the Pi Zero?

The RP2350 on the Interstate 75W runs HUB75 output via PIO and DMA. When the CYW43 WiFi chip
is also active, its SPI bus shares the memory interconnect with the HUB75 DMA, causing stalls
of 70–380 ms every ~1.3 seconds — the classic RP2350 WiFi+HUB75 contention problem.

Removing WiFi from the RP2350 completely eliminates the contention. The Pi Zero W handles
networking; the Interstate only receives frames over a single UART wire and renders them.

---

## Hardware

### Components

| Part | Role |
|---|---|
| Pimoroni Interstate 75W | RP2350 HUB75 controller, MicroPython |
| Pi Zero W | WiFi bridge, ZMQ → UART |
| 2× 128×64 HUB75 panels | Display (chained = 256×64) |
| 5 V PSU (≥4 A) | Powers panels + Pi Zero + Interstate |

### Wiring

**Power — Pi Zero from the panel PSU 5 V rail:**

| Pi Zero pin | PSU rail |
|---|---|
| Pin 2 (5 V) | PSU 5 V |
| Pin 6 (GND) | PSU GND |

This bypasses the Pi Zero's USB fuse and shares GND with the Interstate, which matters for
UART signal integrity.

**Data — UART (one signal wire):**

| Pi Zero | Interstate 75W |
|---|---|
| GPIO 14 / pin 8 (TXD) | GP17 (RX pad, labelled on board) |

Data flows one way only: Pi Zero sends, Interstate receives. No RX wire needed.
GND is shared via the common PSU rail above.

**HUB75 panels — standard Interstate 75W wiring** (unchanged from default; two 128×64 panels
chained to make 256×64).

---

## Software

### hub75pi — Pi Zero W (`hub75pi/main.py`)

Receives frames from boardrr over ZMQ, deduplicates by pixel hash, zlib-compresses, and
sends over UART with a magic-byte framing header.

**Wire format sent to Interstate:**

```
4 bytes  magic header   0xDE 0xAD 0xBE 0xEF
4 bytes  length N       big-endian uint32
4 bytes  payload CRC32  big-endian uint32, of the raw (uncompressed) RGB888 frame
4 bytes  header CRC32   big-endian uint32, CRC32 of the preceding length + payload CRC32 fields
N bytes  zlib-compressed RGB888 frame (256×64×3 = 49,152 bytes raw)
```

The magic header lets the Interstate resync cleanly after any framing error or reboot.

Two layers of integrity checking on the Interstate:

- **Header CRC32** is checked first. If it fails, `length` can't be trusted (reading that many
  bytes would misread the stream and desync framing), so the frame is silently discarded and
  the stream is resynced on the next magic header — no payload is read.
- **Payload CRC32** is checked after decompression. A mismatch (or a decompression error, e.g.
  from a UART bit-flip that still parses as a shorter/garbled DEFLATE stream) causes the frame
  to be dropped — the previous good frame stays on screen and `main: CRC mismatch, dropping
  frame` is printed — instead of rendering corrupted "noise" pixels.

Both checks exist because a bit-flip in the *header* fields (length/payload-CRC) used to cause
a misread of the byte stream, which made `deflate.DeflateIO` raise `EINVAL` — an unhandled
exception in `main.py` that triggered a 1-second `time.sleep()`, visible as a periodic
display freeze. The header CRC catches that case before any misread happens.

**Dependencies** (installed on Pi Zero):

```
sudo apt-get install python3-zmq python3-serial
```

**Systemd service** (`hub75pi/hub75pi.service`) — installed at `/etc/systemd/system/hub75pi.service`:

```
sudo cp hub75pi/hub75pi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hub75pi
```

### hub75rr — Interstate 75W firmware (`hub75rr/`)

MicroPython firmware running on the Interstate 75W. Receives the wire format above, decompresses
with `deflate.DeflateIO(ZLIB)`, converts RGB888→BGRA with a `@micropython.viper` kernel, and
flips the HUB75 framebuffer with `i75.update()`.

**Key design decisions in `serial_client.py`:**

- All intermediate buffers pre-allocated in `__init__` (`_cmp`, `_lbuf`, `_wbuf`, `_obuf`).
  Zero per-frame heap allocation (except an unavoidable ~200 B `DeflateIO` object).
  Without pre-allocation, MicroPython GC fires every ~0.8 s and drops UART bytes.
- `rxbuf=8192` on the UART object gives ~41 ms of byte buffering at 2 Mbaud. This absorbs
  any GC pause, render time, or `i75.update()` latency without losing data.
- `_sync()` scans for the magic header byte-by-byte, so the Interstate always resyncs cleanly
  after a reboot or a stray framing error rather than rendering garbage.

---

## Setup

### Pi Zero W — first time

1. Flash Raspberry Pi OS Lite (32-bit). Enable SSH and WiFi in the imager.
2. Edit `/boot/firmware/config.txt` — add `dtoverlay=disable-bt` to get the reliable PL011
   UART on GPIO 14/15 (`/dev/ttyAMA0`) instead of the mini-UART.
3. Edit `/boot/firmware/cmdline.txt` — remove `console=serial0,115200` so the serial console
   does not conflict with UART data.
4. Install dependencies:
   ```bash
   sudo apt-get install -y python3-zmq python3-serial
   ```
5. Copy `hub75pi/main.py` to `~/hub75pi/main.py` on the Pi Zero.
6. Install and start the systemd service (see above).

### Interstate 75W — firmware

Flash Pimoroni MicroPython v0.0.5 for Interstate 75W (`i75w_rp2350-v0.0.5-micropython.uf2`)
via BOOTSEL if not already done.

Deploy firmware from this repo:

```bash
cd hub75rr
make deploy          # requires mpremote; PORT defaults to /dev/ttyACM0
```

This copies `main.py`, `serial_client.py`, and `config.py` to the Interstate and resets it.

### Verify

```bash
# On Pi 500 — watch hub75pi logs
ssh pi@hub75pi.local "journalctl -f -u hub75pi"

# Expected output:
# hub75pi: opening /dev/ttyAMA0 at 2000000 baud
# hub75pi: subscribed to tcp://10.0.3.55:5600 mode=uk_tdd
# hub75pi: 30 frames sent, last 2400B compressed
```

The departure board should appear on the HUB75 panels within a few seconds of both services
being active, with smooth scrolling and no stutter.

---

## Troubleshooting

**Display frozen / static image**

The Interstate is blocked in `_sync()` waiting for the magic header. Check hub75pi is running
(`systemctl status hub75pi`) and that the UART wire is connected (Pi Zero GPIO 14 → GP17 on
Interstate).

**`make deploy` fails with "ModemManager"**

```bash
sudo systemctl stop ModemManager
```

ModemManager grabs `/dev/ttyACM0` on connect. Disabling it (or adding a udev rule) fixes this
permanently.

**Pi Zero UART unreliable / garbled**

Confirm `/boot/firmware/config.txt` has `dtoverlay=disable-bt` and that `console=serial0,115200`
is absent from `cmdline.txt`. Without these, the Pi Zero W uses the mini-UART (clock-dependent)
rather than the PL011.

**Stutter returns after future firmware changes**

Run the local animation test to confirm whether the stutter is in the Interstate pipeline or
upstream:

```bash
mpremote connect /dev/ttyACM0 run hub75rr/test_smooth.py
```

If this runs at ~400+ fps without visible stutter, the Interstate hardware is fine and the issue
is in `serial_client.py` allocations or upstream frame timing.

---

## Performance

At stock RP2350 clock (150 MHz), 2 Mbaud UART:

| Phase | Time |
|---|---|
| UART transit (2,400 B compressed) | ~12 ms |
| zlib decompress (49,152 B output) | ~30 ms |
| viper RGB888→BGRA render | ~3 ms |
| `i75.update()` flip | ~2 ms |
| **Total per frame** | **~47 ms** |

Effective display rate: ~21 fps. boardrr publishes at 30 fps; hub75pi deduplicates, so the
Interstate renders every unique frame. For a departure board with scrolling text, this is
perceptibly smooth.

The `rxbuf=8192` buffer absorbs up to 41 ms of processing time at 2 Mbaud before any UART
bytes are dropped, which covers the decompress + render + flip cycle comfortably.
