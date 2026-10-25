"""Tiny image helpers (stdlib only): PPM read, PNG write, region diffs."""

import struct
import zlib


class Image:
    """8-bit RGB raster. `data` is width*height*3 bytes."""

    def __init__(self, width, height, data):
        self.width, self.height, self.data = width, height, data

    @classmethod
    def from_ppm(cls, raw):
        # P6\n<w> <h>\n<maxval>\n<data>, possibly with comments.
        if raw[:2] != b"P6":
            raise ValueError("not a binary PPM")
        pos, fields = 2, []
        while len(fields) < 3:
            while raw[pos:pos + 1].isspace():
                pos += 1
            if raw[pos:pos + 1] == b"#":
                while raw[pos:pos + 1] != b"\n":
                    pos += 1
                continue
            start = pos
            while not raw[pos:pos + 1].isspace():
                pos += 1
            fields.append(int(raw[start:pos]))
        pos += 1  # single whitespace after maxval
        w, h, maxval = fields
        if maxval != 255:
            raise ValueError("only 8-bit PPM supported")
        data = raw[pos:pos + w * h * 3]
        if len(data) != w * h * 3:
            raise ValueError("truncated PPM")
        return cls(w, h, data)

    def to_png(self):
        w, h = self.width, self.height
        stride = w * 3
        raw = b"".join(b"\x00" + self.data[y * stride:(y + 1) * stride] for y in range(h))

        def chunk(tag, body):
            c = struct.pack(">I", len(body)) + tag + body
            return c + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 3))
                + chunk(b"IEND", b""))

    def row(self, y):
        s = self.width * 3
        return self.data[y * s:(y + 1) * s]

    def pixel(self, x, y):
        i = (y * self.width + x) * 3
        return tuple(self.data[i:i + 3])

    def distinct_colors(self, limit=4096, stride=7):
        """Number of distinct pixel values in a sparse sample (capped)."""
        seen = set()
        d = self.data
        step = 3 * stride
        for i in range(0, len(d) - 2, step):
            seen.add(d[i:i + 3])
            if len(seen) >= limit:
                break
        return len(seen)

    def is_blank(self):
        """True for a black/uniform frame (what a stalled display dumps)."""
        return self.distinct_colors(limit=16) < 8

    def mean_color(self):
        n = self.width * self.height
        s = [0, 0, 0]
        for c in range(3):
            s[c] = sum(self.data[c::3 * 11]) / (n / 11)
        return tuple(round(v) for v in s)


def diff_region(a, b, ignore_top=0, threshold=12):
    """Compare two same-size images.

    Returns dict(count, bbox=(x0, y0, x1, y1) or None) for pixels whose
    summed channel difference exceeds *threshold*, ignoring rows above
    *ignore_top* (the shell's top bar carries a clock).
    """
    if (a.width, a.height) != (b.width, b.height):
        raise ValueError("size mismatch")
    w = a.width
    count = 0
    x0 = y0 = None
    x1 = y1 = -1
    for y in range(ignore_top, a.height):
        ra, rb = a.row(y), b.row(y)
        if ra == rb:
            continue
        row_hits = []
        for x in range(w):
            i = x * 3
            d = (abs(ra[i] - rb[i]) + abs(ra[i + 1] - rb[i + 1]) + abs(ra[i + 2] - rb[i + 2]))
            if d > threshold:
                row_hits.append(x)
        if row_hits:
            count += len(row_hits)
            y0 = y if y0 is None else y0
            y1 = y
            x0 = row_hits[0] if x0 is None else min(x0, row_hits[0])
            x1 = max(x1, row_hits[-1])
    return {"count": count, "bbox": None if count == 0 else (x0, y0, x1, y1)}
