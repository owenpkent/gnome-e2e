"""Minimal QMP client over a unix socket (stdlib only)."""

import json
import socket
import time


class QMPError(RuntimeError):
    pass


class QMP:
    def __init__(self, path, timeout=30.0):
        self.path = path
        self.timeout = timeout
        self.sock = None
        self.buf = b""
        self.events = []

    def connect(self, retries=1, delay=0.5):
        last = None
        for _ in range(max(1, retries)):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(self.timeout)
                s.connect(self.path)
                self.sock = s
                self.buf = b""
                self._read_msg()  # greeting
                self.command("qmp_capabilities")
                return self
            except OSError as e:
                last = e
                if self.sock:
                    self.sock.close()
                    self.sock = None
                time.sleep(delay)
        raise QMPError(f"cannot connect to QMP socket {self.path}: {last}")

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    def _read_msg(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise QMPError("QMP connection closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def command(self, name, **args):
        msg = {"execute": name}
        if args:
            msg["arguments"] = args
        self.sock.sendall(json.dumps(msg).encode() + b"\n")
        while True:
            reply = self._read_msg()
            if "event" in reply:
                self.events.append(reply)
                continue
            if "error" in reply:
                raise QMPError(f"{name}: {reply['error'].get('desc', reply['error'])}")
            return reply.get("return")

    def screendump(self, filename, fmt="ppm"):
        """Write the primary display to *filename* (qemu writes the file)."""
        self.command("screendump", filename=filename, format=fmt)
