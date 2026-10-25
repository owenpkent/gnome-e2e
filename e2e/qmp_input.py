"""Keyboard injection through QMP input-send-event (virtio-keyboard)."""

import time


def send_key(qmp, qcode, hold=0.05):
    ev = lambda down: {"type": "key", "data": {"down": down, "key": {"type": "qcode", "data": qcode}}}
    qmp.command("input-send-event", events=[ev(True)])
    time.sleep(hold)
    qmp.command("input-send-event", events=[ev(False)])
