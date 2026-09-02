#!/usr/bin/env python3
"""Realistic rubber-stamp impression of 'For BRACKET AND COLON / Proprietor'
   in Courier, no signature, as stamped on paper."""
import math, random
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops

random.seed(21)

SS = 4                      # supersample
W, H = 1200, 620
IW, IH = W * SS, H * SS
INK = (44, 52, 120)         # blue-violet stamp ink

TOP = "For COMPANY NAME"
BOT = "Proprietor"
COURIER = "/usr/share/fonts/X11/Type1/c0419bt_.pfb"   # Courier 10 Pitch

# ---------- 1. draw the clean stamp artwork (black on white, grayscale) ----------
art = Image.new("L", (IW, IH), 255)
d = ImageDraw.Draw(art)

def font(px): return ImageFont.truetype(COURIER, px * SS)

def centered(text, f, y):
    b = d.textbbox((0, 0), text, font=f)
    x = (IW - (b[2] - b[0])) // 2 - b[0]
    d.text((x, y), text, font=f, fill=0)

centered(TOP, font(74), int(90 * SS))
centered(BOT, font(66), int(400 * SS))
# gap between TOP and BOT is left blank for a signature

# downsample the crisp artwork
art = art.resize((W, H), Image.LANCZOS)

# ---------- 2. turn it into a patchy rubber-ink mask ----------
# ink coverage = darkness of artwork
ink = ImageChops.invert(art)                       # 0 bg, 255 solid ink

# mottle: multiply by low-freq + high-freq noise so coverage is uneven
low  = Image.effect_noise((W, H), 26).filter(ImageFilter.GaussianBlur(6))
high = Image.effect_noise((W, H), 40).filter(ImageFilter.GaussianBlur(0.5))
def norm(im, lo, hi):
    return im.point(lambda v: int(lo + (hi - lo) * (v / 255)))
low  = norm(low, 110, 255)      # broad light/heavy zones (more contrast)
high = norm(high, 95, 255)      # fine grain / speckle
texture = ImageChops.multiply(low, high)
inked = ImageChops.multiply(ink, texture)

# edge erosion: rubber edges print thinner -> subtract a thin outline
edge = ink.filter(ImageFilter.MaxFilter(3))
edge = ImageChops.subtract(edge, ink)              # 1px halo around strokes
edge = edge.filter(ImageFilter.GaussianBlur(0.4)).point(lambda v: v // 2)
inked = ImageChops.subtract(inked, edge)

# random dry patches (broken bits of the impression)
dry = Image.effect_noise((W, H), 55).filter(ImageFilter.GaussianBlur(1.2))
dry = dry.point(lambda v: 0 if v > 190 else 255)   # holes where noise is high
inked = ImageChops.multiply(inked, dry)

# tiny ink bleed / spread into paper
inked = inked.filter(ImageFilter.GaussianBlur(0.5))

# ---------- 3. build a realistic paper background ----------
paper = Image.new("RGB", (W, H), (253, 252, 248))
# fine paper grain: near-zero-mean so it textures without darkening the sheet
grain = Image.effect_noise((W, H), 10)
speck = grain.point(lambda v: max(0, 128 - v) // 10)   # only faint dark specks
paper = ImageChops.subtract(paper, Image.merge("RGB", (speck, speck, speck)))
# true corner shading: darken OUTSIDE a centred ellipse, center stays clean
vig = Image.new("L", (W, H), 16)                       # corners up to -16
ImageDraw.Draw(vig).ellipse([W//6, H//6, W - W//6, H - H//6], fill=0)
vig = vig.filter(ImageFilter.GaussianBlur(90))
paper = ImageChops.subtract(paper, Image.merge("RGB", (vig, vig, vig)))

# ---------- 4. composite ink onto paper with per-pixel color jitter ----------
stamp_rgba = Image.new("RGBA", (W, H), (0, 0, 0, 0))
sp = stamp_rgba.load()
ip = inked.load()
hp = high.load()
for y in range(H):
    for x in range(W):
        a = ip[x, y]
        if a < 10:
            continue
        j = (hp[x, y] - 187)            # color jitter from fine grain
        r = max(0, min(255, INK[0] + j // 6))
        g = max(0, min(255, INK[1] + j // 6))
        b = max(0, min(255, INK[2] + j // 8))
        sp[x, y] = (r, g, b, a)

# slight rotation — stamps are never perfectly straight
stamp_rgba = stamp_rgba.rotate(2.4, resample=Image.BICUBIC, expand=False)

result = paper.convert("RGBA")
result.alpha_composite(stamp_rgba)
result = result.convert("RGB")

# ---------- 5. save ----------
result.save("rubber_stamp.png")

# also a clean transparent PNG of just the ink (for overlaying on real docs)
stamp_rgba.save("rubber_stamp_transparent.png")
print("saved", result.size)
