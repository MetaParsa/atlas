"""Builds the radish mascot's animation states from Torob's original 19x19 pixel radish.

Writes web/radish-sheet.png (every frame side by side, 1 px per pixel) and web/radish-sprites.js
(which frames each state plays). Pure standard library, so it runs anywhere:

    python torob/tools/radish_sprites.py            # regenerate the sheet
    python torob/tools/radish_sprites.py --preview DIR   # also write a scaled contact sheet per state

Every state is made of three parts: `in` (props appear, played once), `loop`, and `out` (`in` reversed).
The page plays out(old) → a squash-and-pop → in(new) → loop(new), so every switch is a clean transition.
"""
from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path

W, H = 25, 27                   # canvas: the 19x19 radish plus room for bubbles, props and sparkles
OX, OY = 3, 6                   # where the radish's own 19x19 grid sits on the canvas
WEB = Path(__file__).resolve().parents[1] / "web"

PALETTE = {
    "A": "#587925", "B": "#90b35b", "C": "#860426", "D": "#f04f78", "E": "#000000", "F": "#ffdae3",  # original
    "K": "#475569", "W": "#ffffff", "L": "#bfe3ff", "P": "#f6e7c1", "Q": "#d4b483",                  # props
    "R": "#2563eb", "S": "#9ce76a", "Y": "#ffd23f",
}

LEAVES = {
    "up": [".....AAAA.AAAA.....",
           ".....ABBAAABBA.....",
           ".....ABBBABBBA.....",
           ".....AABBBBBAA.....",
           "......AABBBAA......"],
    "droop": ["...................",
              "..AAA.........AAA..",
              ".ABBAAA.....AAABBA.",
              "..AABBBAA.AABBBAA..",
              "......AABBBAA......"],
}
BODY = ["....CCCDDDDDCCC....",
        "...CCDDDDDDDDDCC...",
        "..CCDDDDDDDDDDDCC..",
        "..CDDDDDDDDDDDDDC..",
        ".CCDDDDDDDDDDDDDCC.",
        ".CDDDDDDDDDDDDDDDC.",
        ".CDDDDDDDDDDDDDDDC.",
        ".CDDDDDDDDDDDDDDDC.",
        ".CCDDDDDDDDDDDDDCC.",
        "..CDDDDDDDDDDDDDC..",
        "..CCDDDDDDDDDDDCC..",
        "...CCCFFFFFFFCCC...",
        ".....CCCFFFCCC.....",
        ".......CCFCC......."]

# Face pixels as (column, body row) of the 19x19 grid; the original face is "open" + "dot".
EYES = {
    "open": [(4, 6), (4, 7), (11, 6), (11, 7)],
    "closed": [(4, 7), (5, 7), (11, 7), (12, 7)],
    "up": [(5, 5), (5, 6), (12, 5), (12, 6)],          # up and to the right, where thought bubbles go
    "down": [(4, 7), (4, 8), (11, 7), (11, 8)],
    "right": [(5, 6), (5, 7), (12, 6), (12, 7)],
    "happy": [(3, 7), (4, 6), (5, 7), (10, 7), (11, 6), (12, 7)],
    "squeeze": [(3, 5), (4, 6), (3, 7), (12, 5), (11, 6), (12, 7)],
}
MOUTHS = {
    "dot": [(7, 7), (8, 7)],
    "open": [(7, 7), (8, 7), (7, 8), (8, 8)],
    "smile": [(6, 7), (7, 8), (8, 8), (9, 7)],
    "frown": [(6, 8), (7, 7), (8, 7), (9, 8)],
    "low": [(7, 8), (8, 8)],
}
BLUSH = [(3, 8), (4, 8), (11, 8), (12, 8)]


class Frame:
    def __init__(self):
        self.px = [["." for _ in range(W)] for _ in range(H)]

    def put(self, x, y, c):
        if c != "." and 0 <= x < W and 0 <= y < H:
            self.px[y][x] = c
        return self

    def blit(self, rows, x0, y0):
        for j, row in enumerate(rows):
            for i, c in enumerate(row):
                self.put(x0 + i, y0 + j, c)
        return self

    def rect(self, x0, y0, w, h, c):
        for y in range(y0, y0 + h):
            for x in range(x0, x0 + w):
                self.put(x, y, c)
        return self

    def key(self):
        return "".join("".join(r) for r in self.px)


def radish(f=None, dy=0, dx=0, squash=False, leaves="up", sway=0, eyes="open", mouth="dot", blush=False, big=None):
    """Draws the radish. `sway` shifts the top three leaf rows; `big` = which eye column a lens magnifies."""
    f = f or Frame()
    x0, y0 = OX + dx, OY + dy
    rows = LEAVES[leaves]
    rows = [(" " * max(sway, 0) + r[:len(r) - sway] if sway > 0 else r[-sway:] + "." * -sway) if j < 3 and sway else r
            for j, r in enumerate(rows)]
    f.blit([r.replace(" ", ".") for r in rows], x0, y0 + (1 if squash else 0))
    f.blit(BODY, x0, y0 + 5)
    face = lambda c, k, col: f.put(x0 + c, y0 + 5 + k, col)
    if blush:
        for c, k in BLUSH:
            face(c, k, "F")
    for c, k in EYES[eyes]:
        face(c, k, "E")
        if big is not None and c == big:          # under the magnifier the eye is twice as wide
            face(c + 1, k, "E")
    for c, k in MOUTHS[mouth]:
        face(c, k, "C")
    return f


BOUNCE = [0, 1, 2, 3, 2, 1]     # the original squash-bounce: frame 3 is squashed


def bounce(**kw):
    return [radish(dy=d, squash=d == 3, **kw) for d in BOUNCE]


# --- props --------------------------------------------------------------------------------------------------------

def bubble(f, x0, y0, w, h, fill="W", edge="K"):
    """Rounded rectangle (corners cut)."""
    f.rect(x0 + 1, y0, w - 2, 1, edge).rect(x0 + 1, y0 + h - 1, w - 2, 1, edge)
    f.rect(x0, y0 + 1, 1, h - 2, edge).rect(x0 + w - 1, y0 + 1, 1, h - 2, edge)
    return f.rect(x0 + 1, y0 + 1, w - 2, h - 2, fill)


def dots(f, x0, y, n, c="K"):
    for i in range(n):
        f.put(x0 + 2 * i, y, c)
    return f


def lens(f, cx, cy):
    """Magnifier centred on (cx, cy): 5x5 ring, glass with a glint, handle down-right."""
    f.blit([".KKK.", "KLLLK", "KLLLK", "KLLLK", ".KKK."], cx - 2, cy - 2)
    f.put(cx - 1, cy - 1, "W")
    for i in range(3):
        f.put(cx + 2 + i, cy + 2 + i, "K")
        f.put(cx + 3 + i, cy + 2 + i, "K")
    return f


def phone(f, x0, y0, lit=True):
    f.rect(x0, y0, 3, 6, "K")
    f.rect(x0 + 1, y0 + 1, 1, 4, "S" if lit else "K")
    return f


def zee(f, x0, y0, c="R"):
    return f.blit([c * 4, ".." + c + ".", "." + c + "..", c * 4], x0, y0)


def sparkle(f, x, y, big):
    if big:
        f.blit([".Y.", "YWY", ".Y."], x - 1, y - 1)
    else:
        f.put(x, y, "Y")
    return f


# --- states -------------------------------------------------------------------------------------------------------

def st_idle():
    loop = bounce() + bounce()
    loop[7] = radish(dy=BOUNCE[1], eyes="closed")      # a blink every second bounce
    return {"ms": 85, "in": [], "loop": loop}


def st_happy():
    hop = [0, -1, -2, -2, -1, 0, 2, 3]
    spots = [(1, 9, 3), (23, 7, 1), (22, 16, 3), (1, 18, 1)]   # (x, y, phase)
    loop = []
    for i, d in enumerate(hop):
        f = radish(dy=d, squash=d == 3, sway=1 if i % 4 < 2 else -1, eyes="happy", mouth="smile", blush=True)
        for x, y, ph in spots:
            if (i + ph) % 4 != 3:
                sparkle(f, x, y, (i + ph) % 4 == 0)
        loop.append(f)
    intro = [sparkle(radish(dy=2, eyes="happy", mouth="smile", blush=True), 1, 9, False)]
    return {"ms": 85, "in": intro, "loop": loop}


def st_think():
    base = dict(dy=1, eyes="up", mouth="low")
    def frame(n_dots, puff=3, sway=0, eyes="up"):
        f = radish(**{**base, "sway": sway, "eyes": eyes})
        if puff >= 1:
            f.put(19, 9, "K")
        if puff >= 2:
            f.rect(20, 6, 2, 2, "K")
        if puff >= 3:
            bubble(f, 14, 0, 11, 5)
            dots(f, 17, 2, n_dots)
        return f
    intro = [frame(0, 1), frame(0, 2), frame(0, 3)]
    loop = [frame(n) for n in (0, 1, 2, 3, 3, 3)]
    loop += [frame(0, sway=1), frame(1), frame(2, eyes="closed"), frame(3), frame(3, sway=-1), frame(3)]
    return {"ms": 150, "in": intro, "loop": loop}


def st_search():
    # The lens sweeps from the left eye to the right eye and back; an eye under the glass looks magnified.
    path = [7, 8, 9, 10, 11, 12, 13, 14, 14, 13, 12, 11, 10, 9, 8, 7]
    loop = []
    for cx in path:
        eye = 4 if cx <= 8 else 11 if cx >= 13 else None
        loop.append(lens(radish(dy=1, eyes="open", mouth="low", big=eye), cx, 17))
    intro = [lens(radish(dy=1, mouth="low"), 6, 23), lens(radish(dy=1, mouth="low"), 7, 20)]
    return {"ms": 90, "in": intro, "loop": loop}


def paper_map(f, width, route_n=0, pin_dy=None):
    """A folded paper map held in front of the radish, unfolding from the centre."""
    w = width
    x0, y0 = 12 - w // 2, 20
    f.rect(x0, y0, w, 6, "P")
    f.rect(x0, y0, w, 1, "Q")
    for fx in (x0 + w // 3, x0 + 2 * w // 3):
        if w >= 9:
            f.rect(fx, y0 + 1, 1, 5, "Q")
    if w >= 21:
        f.put(x0 - 1, y0, "D").put(x0 + w, y0, "D")      # the radish's little hands
        route = [(3, 24), (4, 24), (6, 23), (7, 23), (9, 23), (10, 22), (12, 22), (13, 23), (15, 23), (16, 22),
                 (18, 22), (19, 21)]
        for x, y in route[:route_n]:
            f.put(x, y, "R")
    if pin_dy is not None:
        f.blit([".R.", "RWR", ".R.", ".R."], 18, 17 + pin_dy)
    return f


def st_map():
    look = dict(dy=1, eyes="down", mouth="dot")
    intro = [paper_map(radish(**look), w) for w in (3, 9, 15, 21)]
    loop = []
    for n in range(0, 13, 2):
        loop.append(paper_map(radish(**look), 21, n))
    for pin in (-3, -2, 0, 0, -1, 0, 0, 0):
        loop.append(paper_map(radish(**{**look, "eyes": "down" if pin else "right"}), 21, 12, pin))
    loop[-2] = paper_map(radish(dy=1, eyes="closed"), 21, 12, 0)
    return {"ms": 110, "in": intro, "loop": loop}


def waves(f, n):
    if n >= 1:
        f.rect(22, 14, 1, 3, "R")
    if n >= 2:
        f.put(23, 12, "R").rect(24, 13, 1, 5, "R").put(23, 18, "R")
    return f


def st_call():
    def frame(talk, n, rise=0, eyes="open"):
        f = radish(dy=1, eyes=eyes, mouth="open" if talk else "dot")
        phone(f, 19, 13 + rise)
        f.put(19, 19 + rise, "D")                        # hand holding the phone
        return waves(f, n)
    intro = [frame(False, 0, 7), frame(False, 0, 4), frame(False, 0, 1)]
    loop = [frame(t, n) for t, n in ((1, 1), (0, 2), (1, 2), (0, 0), (1, 1), (1, 2), (0, 2), (0, 0))]
    loop += [frame(0, 1), frame(0, 2), frame(0, 0, eyes="closed"), frame(0, 1)]   # listening to the shop
    return {"ms": 140, "in": intro, "loop": loop}


def st_text():
    def frame(typed, bub=None, rise=0, eyes="down"):
        f = radish(dy=1, eyes=eyes, mouth="dot")
        f.rect(10, 20 + rise, 5, 6, "K")                    # phone held in front, screen up
        f.rect(11, 21 + rise, 3, 4, "S")
        for i in range(min(typed, 3)):
            f.rect(11, 21 + rise + i, 1 + (i + typed) % 3, 1, "K")
        f.put(9, 22 + rise, "D").put(15, 22 + rise, "D")
        if bub == "typing":
            bubble(f, 0, 1, 9, 5)
            dots(f, 2, 3, 1 + typed % 3)
        elif bub == "sent":
            bubble(f, 0, 0, 9, 5, fill="S")
            f.rect(2, 2, 5, 1, "K")
            f.put(1, 5, "K")
        return f
    intro = [frame(0, rise=5), frame(0, rise=2), frame(0)]
    loop = [frame(t, "typing") for t in range(6)]
    loop += [frame(3, "sent", eyes="up"), frame(3, "sent", eyes="up"), frame(0), frame(1, "typing")]
    return {"ms": 140, "in": intro, "loop": loop}


def st_oops():
    loop = []
    for i, drop in enumerate([12, 13, 14, 15, None, None, 12, 13, 14, 15, None, None]):
        f = radish(dy=2, dx=(1 if i in (1, 3) else -1 if i == 2 else 0), leaves="droop", eyes="squeeze", mouth="frown")
        if drop is not None:
            f.rect(21, drop, 1, 2, "L").put(21, drop + 2, "R")
        loop.append(f)
    return {"ms": 110, "in": [radish(dy=2, leaves="droop", eyes="open", mouth="frown")], "loop": loop}


def st_sleep():
    def frame(squash, z):
        f = radish(dy=3 if squash else 2, squash=squash, eyes="closed", mouth="dot")
        for k, (x, y) in enumerate([(17, 9), (19, 5), (21, 1)]):
            if (z - k) % 4 == 0 or (z - k) % 4 == 1:
                zee(f, x, y + (z % 2))
        return f
    loop = [frame(i % 4 >= 2, i) for i in range(8)]
    return {"ms": 380, "in": [radish(dy=2, eyes="closed")], "loop": loop}


def st_listen():
    loop = [radish(dy=d, squash=d == 3, eyes="down", mouth="dot") for d in (1, 1, 2, 2, 1, 1, 2, 2)]
    loop[5] = radish(dy=1, eyes="closed")
    return {"ms": 140, "in": [], "loop": loop}


STATES = {"idle": st_idle, "happy": st_happy, "think": st_think, "search": st_search, "map": st_map,
          "call": st_call, "text": st_text, "oops": st_oops, "sleep": st_sleep, "listen": st_listen}


def build():
    frames, index = [], {}
    def ids(seq):
        out = []
        for f in seq:
            k = f.key()
            if k not in index:
                index[k] = len(frames)
                frames.append(f)
            out.append(index[k])
        return out
    pop = ids([radish(dy=3, squash=True, eyes="closed"), radish(dy=-1)])     # squash-and-pop between states
    meta = {"w": W, "h": H, "pop": pop, "popMs": 70, "states": {}}
    for name, make in STATES.items():
        s = make()
        meta["states"][name] = {"ms": s["ms"], "in": ids(s["in"]), "loop": ids(s["loop"])}
    return frames, meta


# --- PNG (stdlib only) ----------------------------------------------------------------------------------------------

def rgba(c):
    return (0, 0, 0, 0) if c == "." else tuple(int(PALETTE[c][i:i + 2], 16) for i in (1, 3, 5)) + (255,)


def write_png(path, rows):
    h, w = len(rows), len(rows[0])
    raw = b"".join(b"\x00" + b"".join(bytes(p) for p in row) for row in rows)
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def sheet_rows(frames, scale=1, bg=None):
    rows = []
    for y in range(H * scale):
        row = []
        for f in frames:
            for x in range(W * scale):
                c = f.px[y // scale][x // scale]
                row.append(bg if (c == "." and bg) else rgba(c))
        rows.append(row)
    return rows


def main():
    frames, meta = build()
    write_png(WEB / "radish-sheet.png", sheet_rows(frames))
    (WEB / "radish-sprites.js").write_text(
        "// Generated by torob/tools/radish_sprites.py — do not edit by hand.\n"
        f"window.RADISH_SPRITES = {json.dumps(meta, separators=(',', ':'))};\n", encoding="utf-8")
    print(f"{len(frames)} frames, {len(meta['states'])} states")
    if "--preview" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--preview") + 1])
        out.mkdir(parents=True, exist_ok=True)
        for name, s in meta["states"].items():
            seq = [frames[i] for i in s["in"] + s["loop"]]
            write_png(out / f"{name}.png", sheet_rows(seq, 6, (241, 245, 249, 255)))


if __name__ == "__main__":
    main()
