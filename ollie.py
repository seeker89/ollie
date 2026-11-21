#!/usr/bin/env python3
"""
Drive a Sphero Ollie (model 1B01) from a laptop over Bluetooth LE — no Sphero app needed.

    pip install bleak
    python3 ollie.py                 # finds the first "2B-xxxx" nearby
    python3 ollie.py --name 2B-1A2B  # pick a specific one
    python3 ollie.py --scan          # list nearby Sphero BLE robots

Keys (terminals have no key-up events, so driving is "throttle" style):
    w / ↑   faster forward (or less backward)      a / ←   turn left 15°
    s / ↓   slower / backward                      d / →   turn right 15°
    space   stop                                   c       next colour
    h       aim mode: a/d rotate in place, h again = "this way is forward"
    z       put Ollie to sleep and quit            q       stop and quit

Also usable as a library:
    async with Ollie.find() as o:
        await o.rgb(255, 0, 0); await o.roll(80, 0); await asyncio.sleep(1); await o.stop()
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import threading

ROBOT_SVC = "22bb746f-2ba0-7554-2d6f-726568705327"
COMMANDS = "22bb746f-2ba1-7554-2d6f-726568705327"
RESPONSE = "22bb746f-2ba6-7554-2d6f-726568705327"
RADIO_SVC = "22bb746f-2bb0-7554-2d6f-726568705327"
ANTI_DOS = "22bb746f-2bbd-7554-2d6f-726568705327"
TX_POWER = "22bb746f-2bb2-7554-2d6f-726568705327"
WAKE = "22bb746f-2bbf-7554-2d6f-726568705327"

NAME_PREFIX = "2B-"  # Ollie advertises as 2B-XXXX (it was codenamed "2B")


# ----------------------------------------------------------------------------- protocol
def _b(v: float) -> int:
    return max(0, min(255, round(v)))


def _deg(h: float) -> int:
    return round(h) % 360


class Proto:
    """Sphero API v1 packets: FF, FF|FE, DID, CID, SEQ, DLEN, DATA..., CHK."""

    def __init__(self) -> None:
        self.seq = 0

    def packet(self, did: int, cid: int, data: list[int] = (), answer: bool = False) -> tuple[int, bytes]:
        self.seq = (self.seq + 1) & 0xFF
        body = [did, cid, self.seq, len(data) + 1, *data]
        chk = ~sum(body) & 0xFF
        return self.seq, bytes([0xFF, 0xFF if answer else 0xFE, *body, chk])

    def ping(self):                 return self.packet(0x00, 0x01, [], True)
    def power_state(self):          return self.packet(0x00, 0x20, [], True)
    def sleep(self):                return self.packet(0x00, 0x22, [0, 0, 0, 0, 0])
    def set_heading(self, h):       h = _deg(h); return self.packet(0x02, 0x01, [h >> 8, h & 0xFF])
    def stabilization(self, on):    return self.packet(0x02, 0x02, [1 if on else 0])
    def rgb(self, r, g, b):         return self.packet(0x02, 0x20, [_b(r), _b(g), _b(b), 0])
    def back_led(self, v):          return self.packet(0x02, 0x21, [_b(v)])

    def roll(self, speed, heading, go=True):
        h = _deg(heading)
        return self.packet(0x02, 0x30, [_b(speed), h >> 8, h & 0xFF, 1 if go else 0])

    def raw_motors(self, lmode, lpow, rmode, rpow):  # modes: 0 off, 1 fwd, 2 rev, 3 brake, 4 ignore
        return self.packet(0x02, 0x33, [lmode, _b(lpow), rmode, _b(rpow)])


def parse_responses(buf: bytearray):
    """Pull complete packets off the front of buf. Yields ('response', code, seq, data) or ('async', id, data)."""
    while True:
        i = 0
        while i < len(buf) - 1 and not (buf[i] == 0xFF and buf[i + 1] in (0xFF, 0xFE)):
            i += 1
        del buf[:i]
        if len(buf) < 5:
            return
        sync = buf[1] == 0xFF
        dlen = buf[4] if sync else (buf[3] << 8) | buf[4]
        if not 1 <= dlen <= 2048:
            del buf[0]
            continue
        total = 5 + dlen
        if len(buf) < total:
            return
        p = bytes(buf[:total])
        del buf[:total]
        if (~sum(p[2:-1]) & 0xFF) != p[-1]:
            continue
        yield ("response", p[2], p[3], p[5:-1]) if sync else ("async", p[2], p[5:-1])


# ----------------------------------------------------------------------------- device
class Ollie:
    def __init__(self, device):
        from bleak import BleakClient

        self.device = device
        self.client = BleakClient(device)
        self.proto = Proto()
        self._buf = bytearray()
        self._pending: dict[int, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    async def scan(timeout: float = 6.0):
        from bleak import BleakScanner

        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        return [d for d, adv in found.values() if (d.name or adv.local_name or "").startswith(NAME_PREFIX)]

    @classmethod
    def find(cls, name: str | None = None, timeout: float = 10.0) -> "_Finder":
        return _Finder(cls, name, timeout)

    async def connect(self):
        await self.client.connect()
        await self.client.write_gatt_char(ANTI_DOS, b"011i3", response=True)
        await self.client.write_gatt_char(TX_POWER, bytes([7]), response=True)
        await self.client.write_gatt_char(WAKE, bytes([1]), response=True)
        try:
            await self.client.start_notify(RESPONSE, self._on_notify)
        except Exception as e:  # not fatal — we just lose battery readouts
            print(f"(notifications unavailable: {e})", file=sys.stderr)
        await asyncio.sleep(0.3)
        await self.stabilization(True)
        return self

    async def disconnect(self):
        if self.client.is_connected:
            await self.client.disconnect()

    async def __aenter__(self):
        return await self.connect()

    async def __aexit__(self, *exc):
        try:
            if self.client.is_connected:
                await self.stop()
        except Exception:
            pass
        finally:
            await self.disconnect()

    def _on_notify(self, _char, data: bytearray):
        self._buf.extend(data)
        for pkt in parse_responses(self._buf):
            if pkt[0] == "response":
                fut = self._pending.pop(pkt[2], None)
                if fut and not fut.done():
                    fut.set_result(pkt)

    async def _send(self, built, timeout: float = 2.0):
        seq, data = built
        fut = None
        if data[1] == 0xFF:
            fut = asyncio.get_running_loop().create_future()
            self._pending[seq] = fut
        async with self._lock:
            await self.client.write_gatt_char(COMMANDS, data, response=False)
        if fut:
            try:
                return await asyncio.wait_for(fut, timeout)
            finally:
                self._pending.pop(seq, None)

    async def roll(self, speed, heading, go=True): await self._send(self.proto.roll(speed, heading, go))
    async def stop(self, heading=0):              await self._send(self.proto.roll(0, heading, False))
    async def rgb(self, r, g, b):                 await self._send(self.proto.rgb(r, g, b))
    async def back_led(self, v):                  await self._send(self.proto.back_led(v))
    async def set_heading(self, h=0):             await self._send(self.proto.set_heading(h))
    async def stabilization(self, on):            await self._send(self.proto.stabilization(on))
    async def raw_motors(self, lm, lp, rm, rp):   await self._send(self.proto.raw_motors(lm, lp, rm, rp))
    async def sleep(self):                        await self._send(self.proto.sleep())

    async def battery(self):
        """Returns (volts, state) — state is one of charging / OK / low / critical."""
        _, code, _, d = await self._send(self.proto.power_state())
        if code != 0 or len(d) < 4:
            return None
        return ((d[2] << 8) | d[3]) / 100, {1: "charging", 2: "OK", 3: "low", 4: "critical"}.get(d[1], "?")


class _Finder:
    def __init__(self, cls, name, timeout):
        self.cls, self.name, self.timeout, self.ollie = cls, name, timeout, None

    async def __aenter__(self):
        from bleak import BleakScanner

        want = self.name or NAME_PREFIX

        def match(d, adv):
            n = d.name or adv.local_name or ""
            return n == want if self.name else n.startswith(want)

        dev = await BleakScanner.find_device_by_filter(match, timeout=self.timeout)
        if not dev:
            raise SystemExit(f"No Ollie found ({want}). Plug it into USB for a few seconds to wake it, and keep it close.")
        print(f"Found {dev.name or want} ({dev.address}), connecting…")
        self.ollie = self.cls(dev)
        return await self.ollie.connect()

    async def __aexit__(self, *exc):
        await self.ollie.__aexit__(*exc)


# ----------------------------------------------------------------------------- keyboard
def _key_reader(loop: asyncio.AbstractEventLoop, q: asyncio.Queue, stop: threading.Event):
    arrows = {"A": "up", "B": "down", "D": "left", "C": "right", "H": "up", "P": "down", "K": "left", "M": "right"}
    put = lambda k: loop.call_soon_threadsafe(q.put_nowait, k)
    if sys.platform == "win32":
        import msvcrt

        while not stop.is_set():
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                put(arrows.get(msvcrt.getwch(), ""))
            else:
                put(ch)
        return
    while not stop.is_set():  # terminal is put in cbreak mode by _raw_terminal()
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            if sys.stdin.read(1) == "[":
                put(arrows.get(sys.stdin.read(1), ""))
        else:
            put(ch)


class _raw_terminal:
    """cbreak mode on Unix, restored on exit even if the reader thread is still blocked."""

    def __enter__(self):
        self.old = None
        if sys.platform != "win32" and sys.stdin.isatty():
            import termios
            import tty

            self.old = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())

    def __exit__(self, *exc):
        if self.old is not None:
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old)


COLOURS = [(42, 167, 232), (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 140, 0), (200, 0, 255), (255, 255, 255)]


async def drive(name: str | None, top: int):
    async with Ollie.find(name) as o:
        try:
            bat = await o.battery()
            if bat:
                print(f"Battery {bat[0]:.2f} V ({bat[1]})")
        except Exception:
            pass
        await o.rgb(*COLOURS[0])
        print(__doc__.split("Keys")[1].split("Also usable")[0].strip("\n "))

        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        halt = threading.Event()
        with _raw_terminal():
            threading.Thread(target=_key_reader, args=(loop, q, halt), daemon=True).start()
            await _loop(o, q, top)
        halt.set()
        print()


async def _loop(o: "Ollie", q: asyncio.Queue, top: int):
    step = max(10, top // 5)
    throttle, heading, colour, aiming = 0, 0, 0, False

    async def apply():
        if aiming:
            await o.roll(0, heading, True)
        elif throttle == 0:
            await o.stop(heading)
        else:
            await o.roll(abs(throttle), heading if throttle > 0 else heading + 180, True)
        mode = "AIM " if aiming else ""
        print(f"\r{mode}speed {throttle:+4d}/{top}  heading {heading % 360:3d}°   ", end="", flush=True)

    while True:
        try:
            k = await asyncio.wait_for(q.get(), 0.5)
        except asyncio.TimeoutError:
            if throttle:
                await apply()  # keep-alive re-send
            continue
        k = k.lower() if len(k) == 1 else k
        if k in ("w", "up"):
            throttle = min(top, throttle + step)
        elif k in ("s", "down"):
            throttle = max(-top, throttle - step)
        elif k in ("a", "left"):
            heading = (heading - (10 if aiming else 15)) % 360
        elif k in ("d", "right"):
            heading = (heading + (10 if aiming else 15)) % 360
        elif k == " ":
            throttle = 0
        elif k == "c":
            colour = (colour + 1) % len(COLOURS)
            await o.rgb(*COLOURS[colour])
        elif k == "h":
            if not aiming:
                aiming, throttle = True, 0
                await o.stop(heading)
                await o.rgb(0, 0, 0)
                await o.back_led(255)
                print("\nAim: a/d to rotate until the tail light faces you, then h.")
            else:
                aiming, heading = False, 0
                await o.set_heading(0)
                await o.back_led(0)
                await o.rgb(*COLOURS[colour])
                print("\nForward set.")
        elif k == "z":
            await o.stop(heading)
            await o.sleep()
            break
        elif k in ("q", "\x03", "\x04"):
            break
        else:
            continue
        await apply()


def main():
    ap = argparse.ArgumentParser(description="Drive a Sphero Ollie over BLE")
    ap.add_argument("--name", help="exact device name, e.g. 2B-1A2B")
    ap.add_argument("--top", type=int, default=120, help="top speed 0-255 (default 120; Ollie is quick)")
    ap.add_argument("--scan", action="store_true", help="just list nearby Ollies")
    a = ap.parse_args()
    try:
        import bleak  # noqa: F401
    except ImportError:
        raise SystemExit("Needs bleak:  pip install bleak")
    if a.scan:
        for d in asyncio.run(Ollie.scan()):
            print(d.name, d.address)
        return
    try:
        asyncio.run(drive(a.name, max(10, min(255, a.top))))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
