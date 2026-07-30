"""Generate og-image.png (1200x630) for social share previews.

Run: python scripts/make_og_image.py
Output: og-image.png at repo root
"""
import math, pathlib
from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / 'og-image.png'

W, H = 1200, 630
BG = (250, 250, 250)
BRAND = (43, 108, 176)   # #2b6cb0
ACCENT = (217, 119, 6)   # #d97706
TEXT = (35, 35, 40)
MUTED = (110, 110, 118)


def load_font(size, bold=False):
    candidates = [
        'C:/Windows/Fonts/segoeuib.ttf' if bold else 'C:/Windows/Fonts/segoeui.ttf',
        'C:/Windows/Fonts/arialbd.ttf' if bold else 'C:/Windows/Fonts/arial.ttf',
        '/System/Library/Fonts/Helvetica.ttc',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def main():
    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)

    # Left-side brand block
    d.rectangle([0, 0, 24, H], fill=BRAND)

    # Title
    title_font = load_font(84, bold=True)
    subtitle_font = load_font(36)
    url_font = load_font(26)
    stat_font = load_font(30, bold=True)
    stat_label_font = load_font(20)

    d.text((72, 70), 'Op Shop Corridor', font=title_font, fill=TEXT)
    d.text((72, 180), 'Find op shops along your route across Australia.', font=subtitle_font, fill=MUTED)

    # Stat pills row (below subtitle)
    badges = [('1,697', 'shops'), ('4', 'chains'), ('~450', 'with hours'), ('AU', 'wide')]
    bx = 72
    by = 260
    bh = 78
    for value, label in badges:
        vw = d.textlength(value, font=stat_font)
        lw = d.textlength(label, font=stat_label_font)
        bw = int(max(vw, lw)) + 44
        d.rounded_rectangle([bx, by, bx+bw, by+bh], radius=12, fill=(255,255,255), outline=BRAND, width=2)
        d.text((bx + bw/2 - vw/2, by + 10), value, font=stat_font, fill=BRAND)
        d.text((bx + bw/2 - lw/2, by + 46), label, font=stat_label_font, fill=MUTED)
        bx += bw + 14

    # Stylised route arc across the lower area — Mel -> Syd sweep
    pts = []
    x0, y0 = 130, 490
    x1, y1 = 1080, 430
    cx, cy = 600, 580
    for t in range(0, 101):
        u = t / 100.0
        x = (1-u)**2 * x0 + 2*(1-u)*u * cx + u*u * x1
        y = (1-u)**2 * y0 + 2*(1-u)*u * cy + u*u * y1
        pts.append((x, y))
    d.line(pts, fill=ACCENT, width=6, joint='curve')

    # Endpoint markers
    for (x, y), label in [((x0, y0), 'MEL'), ((x1, y1), 'SYD')]:
        d.ellipse([x-14, y-14, x+14, y+14], fill=BRAND, outline=(255,255,255), width=3)
        d.text((x-28, y+22), label, font=stat_label_font, fill=MUTED)

    # Shop pins along the route
    for idx in (18, 32, 47, 62, 78):
        px, py = pts[idx]
        d.ellipse([px-8, py-8, px+8, py+8], fill=ACCENT, outline=(255,255,255), width=2)

    # Footer URL
    d.text((72, H - 56), 'donkalot.github.io/oppy-nav', font=url_font, fill=BRAND)

    img.save(OUT, 'PNG', optimize=True)
    print(f'Wrote {OUT} ({OUT.stat().st_size // 1024} KB)')


if __name__ == '__main__':
    main()
