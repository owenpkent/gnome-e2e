"""Demo window for recording ClickLock videos.

Draws a trail wherever the pointer moves while the client sees a button held
(blue primary, red secondary, green middle), shows which buttons the client
sees held right now, and shows a caption read from a file, which the demo
script uses to say what the physical mouse is doing:

  python3 demo_canvas.py CAPTION_FILE [POINTER_FILE]

POINTER_FILE gets the last pointer position ("x y", window coordinates), so
the demo script can steer the pointer.
"""

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

M = Gdk.ModifierType
BUTTONS = (("Primary", M.BUTTON1_MASK, (0.11, 0.42, 0.85)),
           ("Middle", M.BUTTON2_MASK, (0.15, 0.62, 0.30)),
           ("Secondary", M.BUTTON3_MASK, (0.85, 0.20, 0.20)))
PANEL_H = 150


class Canvas(Gtk.Application):
    def __init__(self, caption_file, pointer_file=None):
        super().__init__(application_id="org.gnome.E2EDemoCanvas")
        self.caption_file = caption_file
        self.pointer_file = pointer_file
        self.caption = ""
        self.state = 0
        self.last = None
        self.segments = []  # (x0, y0, x1, y1, rgb)

    def do_activate(self):
        win = Gtk.ApplicationWindow(application=self, title="ClickLock demo")
        win.set_default_size(1200, 700)
        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.area.set_draw_func(self.draw)
        win.set_child(self.area)
        legacy = Gtk.EventControllerLegacy()
        legacy.connect("event", self.on_event)
        self.area.add_controller(legacy)
        GLib.timeout_add(100, self.poll_caption)
        win.maximize()
        win.present()

    def poll_caption(self):
        try:
            with open(self.caption_file) as f:
                text = f.read().strip()
        except OSError:
            text = ""
        if text == "#clear":
            self.segments.clear()
            text = ""
            try:
                open(self.caption_file, "w").close()
            except OSError:
                pass
        if text != self.caption:
            self.caption = text
        self.area.queue_draw()
        return GLib.SOURCE_CONTINUE

    def on_event(self, ctl, ev):
        ev = ev or ctl.get_current_event()
        if ev is None:
            return False
        t = ev.get_event_type()
        if t not in (Gdk.EventType.MOTION_NOTIFY, Gdk.EventType.BUTTON_PRESS,
                     Gdk.EventType.BUTTON_RELEASE, Gdk.EventType.ENTER_NOTIFY):
            return False
        state = int(ev.get_modifier_state())
        # The state on a press/release is from before it; apply this event.
        if t in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.BUTTON_RELEASE):
            mask = {1: M.BUTTON1_MASK, 2: M.BUTTON2_MASK, 3: M.BUTTON3_MASK}.get(ev.get_button(), 0)
            state = state | int(mask) if t == Gdk.EventType.BUTTON_PRESS else state & ~int(mask)
        ok, x, y = ev.get_position()
        if ok:
            if self.last and t == Gdk.EventType.MOTION_NOTIFY:
                for _, mask, rgb in BUTTONS:
                    if state & mask:
                        self.segments.append((*self.last, x, y, rgb))
            self.last = (x, y)
            if self.pointer_file:
                with open(self.pointer_file, "w") as f:
                    f.write(f"{x:.0f} {y:.0f}\n")
        self.state = state
        self.area.queue_draw()
        return False

    def draw(self, area, cr, w, h):
        cr.set_source_rgb(0.97, 0.97, 0.96)
        cr.paint()
        cr.set_line_width(7)
        cr.set_line_cap(1)
        for x0, y0, x1, y1, rgb in self.segments:
            cr.set_source_rgb(*rgb)
            cr.move_to(x0, y0)
            cr.line_to(x1, y1)
            cr.stroke()

        cr.set_source_rgba(1, 1, 1, 0.94)
        cr.rectangle(0, 0, w, PANEL_H)
        cr.fill()
        cr.select_font_face("Sans", 0, 1)
        cr.set_font_size(22)
        cr.set_source_rgb(0.2, 0.2, 0.2)
        cr.move_to(24, 40)
        cr.show_text("What the app sees held:")
        x = 330
        for label, mask, rgb in BUTTONS:
            held = bool(self.state & mask)
            cr.set_source_rgb(*(rgb if held else (0.80, 0.80, 0.80)))
            cr.arc(x, 32, 13, 0, 6.2832)
            cr.fill()
            cr.set_source_rgb(*(rgb if held else (0.55, 0.55, 0.55)))
            cr.move_to(x + 22, 40)
            cr.show_text(f"{label}{' HELD' if held else ''}")
            x += 250
        cr.set_font_size(26)
        cr.set_source_rgb(0.1, 0.1, 0.1)
        for i, line in enumerate(self.caption.splitlines()[:2]):
            cr.move_to(24, 88 + i * 36)
            cr.show_text(line)


if __name__ == "__main__":
    sys.exit(Canvas(*sys.argv[1:3]).run([sys.argv[0]]))
