#!/usr/bin/python3
"""Drive gnome-control-center through AT-SPI (gi Atspi), by visible names.

Generic on purpose: "toggle the row titled X", "set the scale named Y under
row X". Works for rows that do not exist in the installed Settings (yet): with
a gsettings fallback (unless --no-fallback) and `has-row` to probe.

Commands (all print one JSON object on the last stdout line, exit 0/1):
  open [--panel universal-access] [--page "Pointing & Clicking"]
        Launch Settings (re-use a running one) and go to the page. Navigation
        into a sub-page needs a key press, because GTK4 list rows expose no
        AT-SPI action: the row is focused by walking Tab (focus is read back
        from AT-SPI) and Enter is sent through mutter's RemoteDesktop D-Bus API
        (a short-lived virtual keyboard, no root needed).
  close                      quit Settings
  has-row TITLE              is a visible row/label with this name on screen?
  toggle TITLE [on|off|flip] set the switch in row TITLE (default flip)
  get-switch TITLE           state of that switch
  get-scale ROW SCALE        value of slider SCALE under row ROW
  set-scale ROW SCALE VALUE  set it (the row must be expanded/visible)
  dump [--depth N]           print the accessibility tree (text, for debugging)

Options: --no-fallback  never fall back to gsettings (fail instead)
         --timeout S    per-wait timeout (default 20)
Slider SCALE matches the slider's own accessible name, or the title of the
list row that contains it ("Double-Click Delay", "Acceptance Delay", ...).
"""

import argparse
import json
import os
import subprocess
import sys
import time
import warnings

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, Gio, GLib  # noqa: E402

warnings.simplefilter("ignore")

APP_NAME = "gnome-control-center"
SWITCH_ROLES = ("switch", "toggle button", "check box")
ROW_ROLES = ("list item", "switch", "toggle button", "check box", "button", "row")
A11Y_MOUSE = "org.gnome.desktop.a11y.mouse"
KEY_TAB, KEY_ENTER, KEY_ESC = 15, 28, 1

# Fallback maps: visible name -> gsettings key.
FALLBACK_SWITCH = {
    "Simulated Secondary Click": (A11Y_MOUSE, "secondary-click-enabled"),
    "Click Lock": (A11Y_MOUSE, "click-lock-enabled"),
    "Hover Click": (A11Y_MOUSE, "dwell-click-enabled"),
    "Mouse Keys": ("org.gnome.desktop.a11y.keyboard", "mousekeys-enable"),
}
FALLBACK_SCALE = {
    ("Simulated Secondary Click", "Acceptance Delay"): (A11Y_MOUSE, "secondary-click-time"),
    ("Simulated Secondary Click", "Delay"): (A11Y_MOUSE, "secondary-click-time"),
    ("Click Lock", "Delay"): (A11Y_MOUSE, "click-lock-time"),
    ("Click Lock", "Motion Threshold"): (A11Y_MOUSE, "click-lock-threshold"),
    ("Hover Click", "Delay"): (A11Y_MOUSE, "dwell-time"),
    ("Hover Click", "Motion Threshold"): (A11Y_MOUSE, "dwell-threshold"),
}


class DriverError(Exception):
    pass


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# -- AT-SPI helpers -------------------------------------------------------
def kids(acc):
    try:
        n = acc.get_child_count()
    except GLib.Error:
        return
    for i in range(n):
        try:
            c = acc.get_child_at_index(i)
        except GLib.Error:
            continue
        if c is not None:
            yield c


def walk(acc, depth=0, max_depth=60):
    """Pre-order traversal yielding (node, depth)."""
    yield acc, depth
    if depth >= max_depth:
        return
    for c in kids(acc):
        yield from walk(c, depth + 1, max_depth)


def name(acc):
    try:
        return acc.get_name() or ""
    except GLib.Error:
        return ""


def role(acc):
    try:
        return acc.get_role_name() or ""
    except GLib.Error:
        return ""


def states(acc):
    try:
        return {s.value_nick for s in acc.get_state_set().get_states()}
    except GLib.Error:
        return set()


def interfaces(acc):
    try:
        return set(acc.get_interfaces() or [])
    except GLib.Error:
        return set()


def actions(acc):
    out = []
    try:
        for i in range(acc.get_n_actions()):
            out.append(acc.get_action_name(i))
    except GLib.Error:
        pass
    return out


def parent(acc):
    try:
        return acc.get_parent()
    except GLib.Error:
        return None


def find_app():
    desk = Atspi.get_desktop(0)
    for app in kids(desk):
        if name(app) == APP_NAME:
            return app
    return None


def window_of(app):
    for c in kids(app):
        if role(c) in ("frame", "window") and "showing" in states(c):
            return c
    return None


def wait_for(fn, timeout, desc, interval=0.2):
    deadline = time.monotonic() + timeout
    while True:
        val = fn()
        if val:
            return val
        if time.monotonic() >= deadline:
            raise DriverError(f"timed out after {timeout:g}s waiting for {desc}")
        time.sleep(interval)


def visible(root):
    """Visible nodes in pre-order."""
    return [n for n, _ in walk(root) if "showing" in states(n)]


def find_row(root, title):
    """The visible node that represents row *title*: prefer a row-like node
    (list item / switch / button), fall back to any node with that name."""
    first_any = None
    for n in visible(root):
        if name(n) != title:
            continue
        if role(n) in ROW_ROLES:
            return n
        if first_any is None:
            first_any = n
    return first_any


def toggle_widget(node):
    """The node in node's subtree (or itself) that can be toggled."""
    for n, _ in walk(node, max_depth=8):
        if role(n) in SWITCH_ROLES and any(a in ("toggle", "click", "activate") for a in actions(n)):
            return n
    return None


def checked(node, widget):
    return "checked" in states(widget) or "pressed" in states(widget) or "checked" in states(node)


def do_action(acc, prefer=("toggle", "click", "activate", "press")):
    names = actions(acc)
    for want in prefer:
        if want in names:
            acc.do_action(names.index(want))
            return want
    raise DriverError(f"{name(acc)!r} ({role(acc)}) exposes no usable AT-SPI action: {names}")


# -- keyboard through mutter's RemoteDesktop API ----------------------------
class RemoteKeys:
    BUS = "org.gnome.Mutter.RemoteDesktop"
    SESSION_IFACE = "org.gnome.Mutter.RemoteDesktop.Session"

    def __enter__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        r = self._call("/org/gnome/Mutter/RemoteDesktop", self.BUS, "CreateSession", None,
                       GLib.VariantType("(o)"))
        self.path = r.unpack()[0]
        self._call(self.path, self.SESSION_IFACE, "Start", None)
        return self

    def _call(self, path, iface, method, params, reply=None):
        return self.bus.call_sync(self.BUS, path, iface, method, params, reply,
                                  Gio.DBusCallFlags.NONE, 5000, None)

    def key(self, code):
        for down in (True, False):
            self._call(self.path, self.SESSION_IFACE, "NotifyKeyboardKeycode",
                       GLib.Variant("(ub)", (code, down)))
            time.sleep(0.03)

    def __exit__(self, *exc):
        try:
            self._call(self.path, self.SESSION_IFACE, "Stop", None)
        except GLib.Error:
            pass


def shell_prop(prop, value=None):
    """Read (or set, for booleans) a property of org.gnome.Shell."""
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    args = ("org.gnome.Shell", "/org/gnome/Shell", "org.freedesktop.DBus.Properties")
    if value is None:
        r = bus.call_sync(args[0], args[1], args[2], "Get",
                          GLib.Variant("(ss)", ("org.gnome.Shell", prop)), GLib.VariantType("(v)"),
                          Gio.DBusCallFlags.NONE, 5000, None)
        return r.unpack()[0]
    bus.call_sync(args[0], args[1], args[2], "Set",
                  GLib.Variant("(ssv)", ("org.gnome.Shell", prop, GLib.Variant("b", value))), None,
                  Gio.DBusCallFlags.NONE, 5000, None)
    return value


# -- Settings lifecycle ---------------------------------------------------
def launch(panel):
    return subprocess.Popen(["gnome-control-center", panel], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def get_window(timeout):
    return wait_for(lambda: (lambda a: window_of(a) if a else None)(find_app()), timeout,
                    "the Settings window in AT-SPI")


def page_open(win, page):
    """Sub-page `page` is the visible one: its grouping (named by the page
    title) shows and carries a Back button."""
    for n in visible(win):
        if role(n) == "grouping" and name(n) == page:
            return True
    return False


def cmd_open(a):
    # A window that opens while the Activities overview is up never gets key
    # focus, and the overview is where a fresh GNOME session starts.
    try:
        if shell_prop("OverviewActive"):
            shell_prop("OverviewActive", False)
            wait_for(lambda: not shell_prop("OverviewActive"), a.timeout, "the overview to close")
            time.sleep(0.5)
    except GLib.Error as exc:
        log(f"could not query/close the overview: {exc.message}")
    app = find_app()
    if app is None or window_of(app) is None:
        launch(a.panel)
    win = get_window(a.timeout)
    wait_for(lambda: "active" in states(get_window(a.timeout)), a.timeout,
             "the Settings window to become the active (focused) window")
    if a.page and not page_open(win, a.page):
        nav_title = a.nav_row or a.page.replace("&", "and")
        row = wait_for(lambda: next((n for n in visible(get_window(a.timeout))
                                     if name(n) == nav_title and role(n) == "button"), None),
                       a.timeout, f"navigation row {nav_title!r}")
        with RemoteKeys() as keys:
            for _ in range(40):
                if "focused" in states(row):
                    break
                keys.key(KEY_TAB)
                time.sleep(0.12)
            else:
                raise DriverError(f"could not focus {nav_title!r} with Tab")
            keys.key(KEY_ENTER)
        wait_for(lambda: page_open(get_window(a.timeout), a.page), a.timeout,
                 f"page {a.page!r} to open")
        time.sleep(0.4)  # the push animation
    return {"ok": True, "window": name(win), "page": a.page}


def cmd_close(a):
    was = find_app() is not None
    subprocess.run(["pkill", "-x", "gnome-control-c"], check=False)
    if was:
        wait_for(lambda: find_app() is None, a.timeout, "Settings to exit")
    return {"ok": True, "was_running": was}


def current_window():
    app = find_app()
    return window_of(app) if app else None


def cmd_has_row(a):
    win = current_window()
    return {"ok": True, "found": bool(win and find_row(win, a.title) is not None), "title": a.title}


def locate_switch(win, title):
    row = find_row(win, title)
    if row is None:
        return None, None
    return row, toggle_widget(row)


def cmd_toggle(a):
    win = get_window(a.timeout)
    row, widget = locate_switch(win, a.title)
    if widget is None:
        if a.no_fallback or a.title not in FALLBACK_SWITCH:
            raise DriverError(f"no switch row titled {a.title!r} in the Settings UI")
        return fallback_switch(a)
    before = checked(row, widget)
    want = (not before) if a.state == "flip" else (a.state == "on")
    if before != want:
        used = do_action(widget)
        wait_for(lambda: checked(row, widget) == want, a.timeout, f"{a.title!r} to become {a.state}")
        log(f"switch {a.title!r} via {used}")
    return {"ok": True, "via": "ui", "title": a.title, "before": before, "after": checked(row, widget)}


def cmd_get_switch(a):
    win = get_window(a.timeout)
    row, widget = locate_switch(win, a.title)
    if widget is None:
        if a.no_fallback or a.title not in FALLBACK_SWITCH:
            raise DriverError(f"no switch row titled {a.title!r}")
        schema, key = FALLBACK_SWITCH[a.title]
        return {"ok": True, "via": "gsettings", "state": gsettings_for(schema, key).get_boolean(key)}
    return {"ok": True, "via": "ui", "state": checked(row, widget)}


def gsettings_for(schema, key):
    """Gio.Settings, or DriverError when the schema/key is not installed
    (Gio aborts the whole process on a missing key)."""
    src = Gio.SettingsSchemaSource.get_default()
    sch = src.lookup(schema, True) if src else None
    if sch is None or not sch.has_key(key):
        raise DriverError(f"{schema} has no key {key!r} (feature not installed)")
    return Gio.Settings.new(schema)


def fallback_switch(a):
    schema, key = FALLBACK_SWITCH[a.title]
    s = gsettings_for(schema, key)
    before = s.get_boolean(key)
    want = (not before) if a.state == "flip" else (a.state == "on")
    s.set_boolean(key, want)
    Gio.Settings.sync()
    return {"ok": True, "via": "gsettings", "title": a.title, "before": before, "after": want}


def row_title_of(node):
    """Title of the closest enclosing list item with a name."""
    cur = parent(node)
    for _ in range(6):
        if cur is None:
            return ""
        if role(cur) == "list item" and name(cur):
            return name(cur)
        cur = parent(cur)
    return ""


def find_scale(win, row_title, scale_name):
    """First slider after row *row_title* (document order) matching
    *scale_name* by its own name or by its row's title; the search stops at
    the next switch row."""
    nodes = visible(win)
    start = None
    for i, n in enumerate(nodes):
        if name(n) == row_title and role(n) in ROW_ROLES:
            start = i
            break
    if start is None:
        return None
    for n in nodes[start + 1:]:
        r = role(n)
        if r == "slider":
            if name(n) == scale_name or row_title_of(n) == scale_name:
                return n
        elif (r in SWITCH_ROLES and "toggle" in actions(n) and name(n)
              and not name(n).startswith(row_title)):
            break  # another switch row begins
    return None


def cmd_get_scale(a):
    sc = find_scale(get_window(a.timeout), a.row, a.scale)
    if sc is None:
        if a.no_fallback or (a.row, a.scale) not in FALLBACK_SCALE:
            raise DriverError(f"no scale {a.scale!r} under row {a.row!r} (expanded?)")
        schema, key = FALLBACK_SCALE[(a.row, a.scale)]
        return {"ok": True, "via": "gsettings", "value": gsettings_for(schema, key).get_value(key).unpack()}
    return {"ok": True, "via": "ui", "value": sc.get_current_value(),
            "min": sc.get_minimum_value(), "max": sc.get_maximum_value()}


def cmd_set_scale(a):
    sc = find_scale(get_window(a.timeout), a.row, a.scale)
    value = float(a.value)
    if sc is None:
        if a.no_fallback or (a.row, a.scale) not in FALLBACK_SCALE:
            raise DriverError(f"no scale {a.scale!r} under row {a.row!r} (expanded?)")
        schema, key = FALLBACK_SCALE[(a.row, a.scale)]
        s = gsettings_for(schema, key)
        t = s.get_value(key).get_type_string()
        s.set_value(key, GLib.Variant(t, int(value) if t == "i" else value))
        Gio.Settings.sync()
        return {"ok": True, "via": "gsettings", "value": value}
    sc.set_current_value(value)
    wait_for(lambda: abs(sc.get_current_value() - value) < 0.051 * max(1.0, abs(value)),
             a.timeout, f"{a.scale!r} to reach {value}")
    return {"ok": True, "via": "ui", "value": sc.get_current_value()}


def cmd_dump(a):
    app = find_app()
    if app is None:
        print("(no Settings app on the AT-SPI bus)")
        return None
    keep = {"showing", "checked", "pressed", "focused", "selected", "active", "expanded"}
    for node, depth in walk(app, max_depth=a.depth):
        ifs = ",".join(sorted(interfaces(node) - {"Accessible", "Component", "Action"}))
        acts = [x for x in actions(node) if "." not in x]
        print("  " * depth + f"[{role(node)}] {name(node)!r} {{{','.join(sorted(states(node) & keep))}}}"
              + (f" actions={acts}" if acts else "") + (f" {ifs}" if ifs else ""))
    return None


COMMANDS = {"open": cmd_open, "close": cmd_close, "has-row": cmd_has_row,
            "toggle": cmd_toggle, "get-switch": cmd_get_switch, "get-scale": cmd_get_scale,
            "set-scale": cmd_set_scale, "dump": cmd_dump}


def parse(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-fallback", action="store_true")
    ap.add_argument("--timeout", type=float, default=20.0)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("open")
    p.add_argument("--panel", default="universal-access")
    p.add_argument("--page", default="Pointing & Clicking")
    p.add_argument("--nav-row", help='title of the list row that opens the page '
                   '(default: the page title with "&" spelled "and")')
    sub.add_parser("close")
    p = sub.add_parser("has-row"); p.add_argument("title")
    p = sub.add_parser("toggle"); p.add_argument("title")
    p.add_argument("state", nargs="?", default="flip", choices=["on", "off", "flip"])
    p = sub.add_parser("get-switch"); p.add_argument("title")
    p = sub.add_parser("get-scale"); p.add_argument("row"); p.add_argument("scale")
    p = sub.add_parser("set-scale"); p.add_argument("row"); p.add_argument("scale"); p.add_argument("value")
    p = sub.add_parser("dump"); p.add_argument("--depth", type=int, default=40)
    # global options are also accepted after the sub-command
    args, rest = ap.parse_known_args(argv)
    for flag in rest:
        if flag == "--no-fallback":
            args.no_fallback = True
        else:
            ap.error(f"unrecognized argument {flag}")
    return args


def main(argv):
    args = parse(argv)
    try:
        out = COMMANDS[args.cmd](args)
    except (DriverError, GLib.Error) as exc:
        print(json.dumps({"ok": False, "error": getattr(exc, "message", None) or str(exc)}))
        return 1
    if out is not None:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
