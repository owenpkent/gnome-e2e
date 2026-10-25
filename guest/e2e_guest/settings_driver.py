#!/usr/bin/python3
"""Drive gnome-control-center through AT-SPI (gi Atspi), by visible names.

Generic on purpose: "toggle the row titled X", "set the scale named Y under
row X". Works for rows that do not exist yet in the installed Settings: with
a gsettings fallback (unless --no-fallback) and `has-row` to probe.

Commands (all print one JSON object on the last stdout line):
  open [--panel universal-access] [--page "Pointing & Clicking"]
                              launch Settings (reuse a running one), go to the page
  close                       quit Settings
  has-row TITLE               is a visible row/label with this name on screen?
  toggle TITLE [on|off|flip]  set the switch/toggle in row TITLE (default flip)
  get-switch TITLE            current state of that switch
  get-scale ROW SCALE         value of slider SCALE under row ROW
  set-scale ROW SCALE VALUE   set it
  dump [--depth N]            print the accessibility tree (debug; text, not JSON)

Options: --no-fallback  never fall back to gsettings (fail instead)
         --timeout S    per-wait timeout (default 20)
Exit status 0 on success, 1 on failure (JSON has "ok": false and "error").
"""

import json
import os
import subprocess
import sys
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, Gio, GLib  # noqa: E402

APP_NAMES = ("gnome-control-center", "Settings")
SWITCH_ROLES = ("switch", "toggle button", "check box")
SLIDER_ROLES = ("slider", "spin button", "scroll bar")
A11Y_MOUSE = "org.gnome.desktop.a11y.mouse"

# Fallback maps: visible name -> gsettings key.
FALLBACK_SWITCH = {
    "Simulated Secondary Click": (A11Y_MOUSE, "secondary-click-enabled"),
    "Click Lock": (A11Y_MOUSE, "click-lock-enabled"),
    "Hover Click": (A11Y_MOUSE, "dwell-click-enabled"),
    "Mouse Keys": ("org.gnome.desktop.a11y.keyboard", "mousekeys-enable"),
}
FALLBACK_SCALE = {
    ("Simulated Secondary Click", "Delay"): (A11Y_MOUSE, "secondary-click-time"),
    ("Click Lock", "Delay"): (A11Y_MOUSE, "click-lock-time"),
    ("Click Lock", "Movement Threshold"): (A11Y_MOUSE, "click-lock-threshold"),
    ("Hover Click", "Delay"): (A11Y_MOUSE, "dwell-time"),
    ("Hover Click", "Motion Threshold"): (A11Y_MOUSE, "dwell-threshold"),
}


class DriverError(Exception):
    pass


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


def showing(acc):
    s = states(acc)
    return "showing" in s or "visible" in s and "showing" not in s and False


def parent(acc):
    try:
        return acc.get_parent()
    except GLib.Error:
        return None


def describe(acc):
    return {"name": name(acc), "role": role(acc), "states": sorted(states(acc)),
            "actions": actions(acc)}


def find_app():
    desk = Atspi.get_desktop(0)
    for app in kids(desk):
        if name(app) in APP_NAMES or "control-center" in name(app).lower():
            return app
    return None


def wait_for(fn, timeout, desc, interval=0.25):
    deadline = time.monotonic() + timeout
    while True:
        val = fn()
        if val:
            return val
        if time.monotonic() >= deadline:
            raise DriverError(f"timed out after {timeout}s waiting for {desc}")
        time.sleep(interval)


def visible_nodes(root):
    for node, depth in walk(root):
        if "showing" in states(node):
            yield node, depth


def find_named(root, wanted, roles=None, exact=True):
    """First visible node (pre-order) whose accessible name matches."""
    for node, _ in visible_nodes(root):
        n = name(node)
        if (n == wanted if exact else wanted.lower() in n.lower()):
            if roles is None or role(node) in roles:
                return node
    return None


def container_with(node, want_roles, max_up=6):
    """Climb from *node* to the nearest ancestor whose subtree holds a node
    with one of *want_roles*; return (ancestor, that_node)."""
    cur = node
    for _ in range(max_up):
        if cur is None:
            break
        for n, _ in walk(cur, max_depth=8):
            if n is not cur and role(n) in want_roles and "showing" in states(n):
                return cur, n
        cur = parent(cur)
    return None, None


def row_widget(root, title, want_roles):
    label = find_named(root, title)
    if label is None:
        return None, None, None
    cont, widget = container_with(label, want_roles)
    # The title node can itself be the switch (named by its label).
    if widget is None and role(label) in want_roles:
        return label, label, label
    return label, cont, widget


def do_action(acc, prefer=("activate", "click", "toggle", "press", "Press", "Activate", "Toggle")):
    names = actions(acc)
    if not names:
        raise DriverError(f"{name(acc)!r} ({role(acc)}) exposes no AT-SPI action")
    for want in prefer:
        if want in names:
            acc.do_action(names.index(want))
            return want
    acc.do_action(0)
    return names[0]


# -- Settings lifecycle ---------------------------------------------------
def launch(panel):
    env = dict(os.environ)
    return subprocess.Popen(["gnome-control-center", panel], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


def window_of(app):
    for c in kids(app):
        if role(c) in ("frame", "window", "dialog") and "showing" in states(c):
            return c
    return None


def get_window(timeout):
    def f():
        app = find_app()
        return window_of(app) if app else None

    return wait_for(f, timeout, "the Settings window in AT-SPI")


def cmd_open(a):
    win = None
    app = find_app()
    if app is None or window_of(app) is None:
        launch(a.panel)
    win = get_window(a.timeout)
    if a.page:
        # Already on the page? (a content row is visible)
        if find_named(win, a.page) is None or not page_is_open(win, a.page):
            nav = wait_for(lambda: find_named(get_window(a.timeout), a.page), a.timeout,
                           f"navigation row {a.page!r}")
            cont, _ = container_with_action(nav)
            used = do_action(cont)
            time.sleep(0.2)
            log(f"activated {name(cont)!r} via {used}")
        wait_for(lambda: page_is_open(get_window(a.timeout), a.page), a.timeout,
                 f"page {a.page!r} to open")
    return {"ok": True, "window": name(win), "page": a.page}


def container_with_action(node, max_up=6):
    cur = node
    for _ in range(max_up):
        if cur is None:
            break
        if actions(cur):
            return cur, actions(cur)
        cur = parent(cur)
    raise DriverError(f"no activatable ancestor for {name(node)!r}")


def page_is_open(win, page):
    """The page is open when a header title with its name shows AND the panel
    listing (several sibling nav rows) is no longer the visible content."""
    heads = [n for n, _ in visible_nodes(win) if name(n) == page]
    # A subpage shows the page name once in the header bar; the Universal
    # Access list view shows it as a row *and* has other category rows.
    others = [n for n, _ in visible_nodes(win)
              if name(n) in ("Seeing", "Hearing", "Typing", "Zoom") and role(n) != "frame"]
    return bool(heads) and not others


def cmd_close(a):
    app = find_app()
    if app is None:
        return {"ok": True, "was_running": False}
    subprocess.run(["pkill", "-f", "gnome-control-center"], check=False)
    wait_for(lambda: find_app() is None, a.timeout, "Settings to exit")
    return {"ok": True, "was_running": True}


def cmd_has_row(a):
    app = find_app()
    win = window_of(app) if app else None
    found = bool(win and find_named(win, a.title) is not None)
    return {"ok": True, "found": found, "title": a.title}


def switch_state(widget):
    s = states(widget)
    return "checked" in s or "pressed" in s


def cmd_toggle(a):
    win = get_window(a.timeout)
    label, cont, widget = row_widget(win, a.title, SWITCH_ROLES)
    if widget is None:
        if a.no_fallback or a.title not in FALLBACK_SWITCH:
            raise DriverError(f"no switch row titled {a.title!r} in the Settings UI")
        return fallback_switch(a)
    before = switch_state(widget)
    want = (not before) if a.state == "flip" else (a.state == "on")
    if before != want:
        used = do_action(widget)
        wait_for(lambda: switch_state(widget) == want, a.timeout, f"{a.title!r} to become {a.state}")
        log(f"switch {a.title!r} via {used}")
    return {"ok": True, "via": "ui", "title": a.title, "before": before, "after": switch_state(widget)}


def cmd_get_switch(a):
    win = get_window(a.timeout)
    label, cont, widget = row_widget(win, a.title, SWITCH_ROLES)
    if widget is None:
        if a.no_fallback or a.title not in FALLBACK_SWITCH:
            raise DriverError(f"no switch row titled {a.title!r}")
        schema, key = FALLBACK_SWITCH[a.title]
        return {"ok": True, "via": "gsettings", "state": Gio.Settings.new(schema).get_boolean(key)}
    return {"ok": True, "via": "ui", "state": switch_state(widget)}


def fallback_switch(a):
    schema, key = FALLBACK_SWITCH[a.title]
    s = Gio.Settings.new(schema)
    before = s.get_boolean(key)
    want = (not before) if a.state == "flip" else (a.state == "on")
    s.set_boolean(key, want)
    Gio.Settings.sync()
    return {"ok": True, "via": "gsettings", "title": a.title, "before": before, "after": want}


def find_scale(win, row_title, scale_name):
    """First slider after row *row_title* (document order) whose own name, or
    the title of the row holding it, equals *scale_name*."""
    seen_row = False
    row_label = find_named(win, row_title)
    if row_label is None:
        return None
    nodes = [n for n, _ in visible_nodes(win)]
    start = None
    for i, n in enumerate(nodes):
        if n == row_label or (name(n) == row_title and role(n) == role(row_label)):
            start = i
            break
    for n in nodes[(start or 0):]:
        if role(n) not in SLIDER_ROLES:
            continue
        if name(n) == scale_name:
            return n
        # name from the enclosing row's title label
        cur = parent(n)
        for _ in range(4):
            if cur is None:
                break
            titles = [c for c in kids(cur) if role(c) == "label" and name(c) == scale_name]
            if titles:
                return n
            cur = parent(cur)
    return None


def cmd_get_scale(a):
    win = get_window(a.timeout)
    sc = find_scale(win, a.row, a.scale)
    if sc is None:
        if a.no_fallback or (a.row, a.scale) not in FALLBACK_SCALE:
            raise DriverError(f"no scale {a.scale!r} under row {a.row!r}")
        schema, key = FALLBACK_SCALE[(a.row, a.scale)]
        v = Gio.Settings.new(schema).get_value(key).unpack()
        return {"ok": True, "via": "gsettings", "value": v}
    return {"ok": True, "via": "ui", "value": sc.get_current_value(),
            "min": sc.get_minimum_value(), "max": sc.get_maximum_value()}


def cmd_set_scale(a):
    win = get_window(a.timeout)
    sc = find_scale(win, a.row, a.scale)
    value = float(a.value)
    if sc is None:
        if a.no_fallback or (a.row, a.scale) not in FALLBACK_SCALE:
            raise DriverError(f"no scale {a.scale!r} under row {a.row!r}")
        schema, key = FALLBACK_SCALE[(a.row, a.scale)]
        s = Gio.Settings.new(schema)
        cur = s.get_value(key)
        s.set_value(key, GLib.Variant(cur.get_type_string(), int(value) if cur.get_type_string() == "i" else value))
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
    for node, depth in walk(app, max_depth=a.depth):
        ifs = ",".join(sorted(interfaces(node) - {"Accessible", "Component"}))
        print("  " * depth + f"[{role(node)}] {name(node)!r} "
              f"{{{','.join(sorted(states(node) & {'showing','checked','pressed','focused','selected','sensitive','enabled'}))}}}"
              f" actions={actions(node)} {ifs}")
    return None


def log(msg):
    print(msg, file=sys.stderr)


COMMANDS = {"open": cmd_open, "close": cmd_close, "has-row": cmd_has_row,
            "toggle": cmd_toggle, "get-switch": cmd_get_switch, "get-scale": cmd_get_scale,
            "set-scale": cmd_set_scale, "dump": cmd_dump}


def parse(argv):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-fallback", action="store_true")
    ap.add_argument("--timeout", type=float, default=20.0)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("open")
    p.add_argument("--panel", default="universal-access")
    p.add_argument("--page", default="Pointing & Clicking")
    sub.add_parser("close")
    p = sub.add_parser("has-row"); p.add_argument("title")
    p = sub.add_parser("toggle"); p.add_argument("title"); p.add_argument("state", nargs="?", default="flip", choices=["on", "off", "flip"])
    p = sub.add_parser("get-switch"); p.add_argument("title")
    p = sub.add_parser("get-scale"); p.add_argument("row"); p.add_argument("scale")
    p = sub.add_parser("set-scale"); p.add_argument("row"); p.add_argument("scale"); p.add_argument("value")
    p = sub.add_parser("dump"); p.add_argument("--depth", type=int, default=40)
    # global options may also follow the sub-command
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
    except DriverError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    if out is not None:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
