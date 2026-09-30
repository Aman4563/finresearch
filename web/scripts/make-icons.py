"""Draw the PWA icons (web/public) from the header logo: a teal-to-indigo tile with the rising line. Run with
`uv run python web/scripts/make-icons.py` (Pillow is a backend dependency)."""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "public"
TEAL, INDIGO = (15, 118, 110), (79, 70, 229)


def tile(size: int, *, maskable: bool = False) -> Image.Image:
    s = size * 4  # draw large, downsample for smooth edges
    grad = Image.new("RGB", (s, s))
    px = grad.load()
    for y in range(s):
        for x in range(s):
            t = (x + y) / (2 * (s - 1))
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(TEAL, INDIGO, strict=True))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=0 if maskable else s // 5, fill=255)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    pad = s * (0.28 if maskable else 0.2)  # maskable icons keep the mark inside the 80 % safe zone
    box = s - 2 * pad

    def p(x: float, y: float) -> tuple[float, float]:  # the logo's 24x24 viewBox
        return pad + (x - 3) / 18 * box, pad + (y - 5) / 14 * box

    w = round(s * 0.075)
    d.line([p(3, 17), p(8, 12), p(12, 15), p(19, 7)], fill="white", width=w, joint="curve")
    d.line([p(15, 7), p(19, 7), p(19, 11)], fill="white", width=w, joint="curve")
    for x, y in (p(3, 17), p(19, 7), p(15, 7), p(19, 11)):
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill="white")
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for n in (192, 512):
        tile(n).save(OUT / f"icon-{n}.png", optimize=True)
    tile(512, maskable=True).save(OUT / "icon-maskable-512.png", optimize=True)
    tile(180, maskable=True).convert("RGB").save(OUT / "apple-touch-icon.png", optimize=True)
