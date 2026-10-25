#!/usr/bin/python3
"""A uinput relative mouse driven by commands on stdin. Run as root.

Why a persistent process: creating or removing an input device mid-test
changes compositor behaviour (and the click lock drops its latch when the
device goes away). One process owns one device for the whole test session;
the host feeds it commands over a single ssh channel.

Protocol: one command per line on stdin, one reply line on stdout per command:

  ok <t_start_ns> <t_end_ns>      CLOCK_MONOTONIC of the guest, same clock the
                                  probe uses for its timestamps
  err <message>

Commands:
  move DX DY [STEPS [MS]]  relative motion, split into STEPS REL events
                        (default: one step per ~10 units), MS apart (default 2)
  press [BTN]           button down   (BTN: left|right|middle, default left)
  release [BTN]         button up
  click [BTN]           press, 20 ms, release
  hold MS [BTN]         press, wait MS milliseconds, release (blocks)
  sleep MS              wait
  ping                  reply immediately
  quit                  remove the device and exit

On start it prints "ready <name>" once the device exists.
"""

import math
import signal
import sys
import time

import evdev
from evdev import UInput, ecodes as e

NAME = "e2e-uinput-mouse"
BUTTONS = {"left": e.BTN_LEFT, "right": e.BTN_RIGHT, "middle": e.BTN_MIDDLE}
CAPS = {
    e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE],
    e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL],
}


def mono_ns():
    return time.monotonic_ns()


class Mouse:
    def __init__(self):
        self.ui = UInput(CAPS, name=NAME, vendor=0x046D, product=0xC077,
                         bustype=e.BUS_USB, version=1)

    def close(self):
        try:
            self.ui.close()
        except Exception:
            pass

    @staticmethod
    def button(arg):
        try:
            return BUTTONS[arg]
        except KeyError:
            raise ValueError(f"unknown button {arg!r}")

    def key(self, code, value):
        self.ui.write(e.EV_KEY, code, value)
        self.ui.syn()

    def move(self, dx, dy, steps=None, delay_ms=2.0):
        if steps is None:
            steps = max(1, math.ceil(max(abs(dx), abs(dy)) / 10))
        sx = sy = 0
        for i in range(1, steps + 1):
            tx = round(dx * i / steps)
            ty = round(dy * i / steps)
            if tx - sx:
                self.ui.write(e.EV_REL, e.REL_X, tx - sx)
            if ty - sy:
                self.ui.write(e.EV_REL, e.REL_Y, ty - sy)
            if tx - sx or ty - sy:
                self.ui.syn()
            sx, sy = tx, ty
            if steps > 1:
                time.sleep(delay_ms / 1000.0)

    def run(self, line):
        parts = line.split()
        if not parts:
            return
        cmd, args = parts[0], parts[1:]
        if cmd == "move":
            self.move(int(args[0]), int(args[1]), int(args[2]) if len(args) > 2 else None,
                      float(args[3]) if len(args) > 3 else 2.0)
        elif cmd == "press":
            self.key(self.button(args[0] if args else "left"), 1)
        elif cmd == "release":
            self.key(self.button(args[0] if args else "left"), 0)
        elif cmd == "click":
            b = self.button(args[0] if args else "left")
            self.key(b, 1)
            time.sleep(0.02)
            self.key(b, 0)
        elif cmd == "hold":
            ms = float(args[0])
            b = self.button(args[1] if len(args) > 1 else "left")
            self.key(b, 1)
            time.sleep(ms / 1000.0)
            self.key(b, 0)
        elif cmd == "sleep":
            time.sleep(float(args[0]) / 1000.0)
        elif cmd == "ping":
            pass
        else:
            raise ValueError(f"unknown command {cmd!r}")


def main():
    mouse = Mouse()

    def bye(*_):
        mouse.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGHUP, bye)
    print(f"ready {NAME}", flush=True)
    try:
        for line in sys.stdin:
            line = line.strip()
            if line == "quit":
                print("ok 0 0", flush=True)
                break
            t0 = mono_ns()
            try:
                mouse.run(line)
            except Exception as exc:  # report, keep the device alive
                print(f"err {exc}", flush=True)
                continue
            print(f"ok {t0} {mono_ns()}", flush=True)
    finally:
        mouse.close()


if __name__ == "__main__":
    main()
