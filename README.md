# Ollie Remote

Drive a Sphero Ollie (model 1B01, advertises as `2B-xxxx`) without the retired Sphero app.

- `index.html` — phone/laptop controller using Web Bluetooth (joystick, WASD/arrows, spin, aim, colour, battery, sleep).
- `ollie.py` — laptop fallback in Python using `bleak` (terminal keyboard driving, also a small library).

## Running the web controller

Web Bluetooth only works on a page served over **https** or from **localhost**, and only in some browsers:

| Device | Browser |
|---|---|
| Android | Chrome |
| Windows / macOS / ChromeOS | Chrome or Edge |
| iPhone / iPad | Bluefy (free; Safari has no Web Bluetooth) |
| Linux | Chrome with `chrome://flags/#enable-experimental-web-platform-features` |

Ways to host it:

- **Laptop:** `python3 -m http.server 8000` in this folder, then open `http://localhost:8000`.
- **Phone:** put `index.html` in a GitHub repo and turn on Pages (Settings → Pages), or drag the folder onto Netlify Drop. Open the https URL on the phone.

Don't pair Ollie in the OS Bluetooth settings. The browser's own device picker handles the connection.

## Python

```
pip install bleak
python3 ollie.py --scan
python3 ollie.py --top 120
```

## First drive

1. Wake Ollie (plug it into USB for a few seconds if it doesn't show up in the scan).
2. Connect, then tap **Aim**. Rotate until the tail light points at you and tap **Done**. Joystick "up" now means "away from you".
3. Keep the top-speed slider low indoors, because Ollie is fast.

## Protocol notes (Sphero API v1 over BLE)

| | UUID |
|---|---|
| Radio service | `22bb746f-2bb0-7554-2d6f-726568705327` |
| Anti-DoS (write `"011i3"`) | `22bb746f-2bbd-…` |
| TX power (write `0x07`) | `22bb746f-2bb2-…` |
| Wake CPU (write `0x01`) | `22bb746f-2bbf-…` |
| Robot control service | `22bb746f-2ba0-…` |
| Commands (write) | `22bb746f-2ba1-…` |
| Responses (notify) | `22bb746f-2ba6-…` |

The unlock sequence after connecting is anti-DoS, then TX power, then wake. Ollie ignores commands until all three are written.

Packet: `FF, FF|FE, DID, CID, SEQ, DLEN, DATA…, CHK`, where `DLEN = len(DATA)+1` and `CHK = ~sum(DID…DATA) & 0xFF`. `SOP2 = FF` asks for a reply and `FE` doesn't.

| Command | DID CID | Data |
|---|---|---|
| Roll | 02 30 | speed, heading hi, heading lo, state (1 go / 0 stop) |
| Set RGB | 02 20 | R, G, B, persist (0) |
| Back (tail) LED | 02 21 | brightness |
| Set heading (current = 0°) | 02 01 | heading hi, lo |
| Stabilisation | 02 02 | 0/1 |
| Raw motors | 02 33 | L mode, L power, R mode, R power (mode 0 off, 1 fwd, 2 rev, 3 brake) |
| Power state | 00 20 | → reply: ver, state, volts×100 (2 bytes), charges, secs since charge |
| Sleep | 00 22 | 00 00 00 00 00 |

Example: set the LED to red is `FF FE 02 20 01 05 FF 00 00 00 D8`.
