#!/usr/bin/python3
"""GTK4 pointer probe: the oracle for "is the button held?".

Opens a (maximized by default) window and appends one JSON object per line to
--out for every pointer event the toplevel receives:

  {"t_ns": <CLOCK_MONOTONIC ns at receipt>, "evt_ms": <GDK event time>,
   "type": "press"|"release"|"motion"|"enter"|"leave"|"scroll",
   "button": 1..,            (press/release)
   "x": .., "y": ..,         (surface coordinates)
   "state": <GdkModifierType int>, "b1": <BUTTON1_MASK set>, "b3": <BUTTON3_MASK set>}

plus bookkeeping lines: {"type": "ready", "pid": N}, {"type": "layout",
"width": W, "height": H} whenever the surface is (re)laid out.

The window content is static (no redraw on events) so that screenshots taken
before and during a button hold differ only because of what the shell draws.

  probe.py --out FILE [--fullscreen] [--title T]
Stops on SIGTERM/SIGINT.
"""

import argparse
import json
import os
import signal
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, GLibUnix, Gtk  # noqa: E402

ET = Gdk.EventType
NAMES = {
    ET.BUTTON_PRESS: "press",
    ET.BUTTON_RELEASE: "release",
    ET.MOTION_NOTIFY: "motion",
    ET.ENTER_NOTIFY: "enter",
    ET.LEAVE_NOTIFY: "leave",
    ET.SCROLL: "scroll",
}
CSS = b"""
window.e2e-probe { background: #204a87; }
window.e2e-probe label { color: #ffffff; font-size: 28px; }
"""


class Probe(Gtk.Application):
    def __init__(self, out, fullscreen, title):
        super().__init__(application_id="org.gnome.E2EProbe",
                         flags=0)
        self.out_path = out
        self.fullscreen_mode = fullscreen
        self.title = title
        self.out = None

    def emit(self, **kw):
        kw.setdefault("t_ns", time.monotonic_ns())
        self.out.write(json.dumps(kw, separators=(",", ":")) + "\n")
        self.out.flush()

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.out = open(self.out_path, "a", buffering=1)

    def do_activate(self):
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        win = Gtk.ApplicationWindow(application=self, title=self.title)
        win.add_css_class("e2e-probe")
        win.set_child(Gtk.Label(label="gnome-e2e probe"))
        ctl = Gtk.EventControllerLegacy()
        ctl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        ctl.connect("event", self.on_event)
        win.add_controller(ctl)
        win.connect("map", self.on_map)
        if self.fullscreen_mode:
            win.fullscreen()
        else:
            win.maximize()
        win.present()
        self.win = win

    def on_map(self, win):
        surface = win.get_surface()
        surface.connect("layout", lambda s, w, h: self.emit(type="layout", width=w, height=h))
        self.emit(type="ready", pid=os.getpid())

    def on_event(self, ctl, ev):
        # PyGObject may hand the "event" signal's GdkEvent over as None;
        # the controller still knows the event it is handling.
        ev = ev or ctl.get_current_event()
        if ev is None:
            return False
        name = NAMES.get(ev.get_event_type())
        if name is None:
            return False
        state = int(ev.get_modifier_state())
        rec = {"type": name, "evt_ms": ev.get_time(), "state": state,
               "b1": bool(state & Gdk.ModifierType.BUTTON1_MASK),
               "b3": bool(state & Gdk.ModifierType.BUTTON3_MASK)}
        ok, x, y = ev.get_position()
        if ok:
            rec["x"], rec["y"] = round(x, 2), round(y, 2)
        if name in ("press", "release"):
            rec["button"] = ev.get_button()
        self.emit(**rec)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--fullscreen", action="store_true")
    ap.add_argument("--title", default="e2e-probe")
    args = ap.parse_args()
    app = Probe(args.out, args.fullscreen, args.title)
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: app.quit() or False)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
