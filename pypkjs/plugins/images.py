"""`icon` / `image` shape payloads in the phone app's format (libpebble3 ImageEncoder / EncodedImage):
4 bits per pixel, rows padded to whole bytes with even x in the high nibble, and a palette of up to
16 GColor8 bytes. Both parts are base64'd. A transparent entry, when there is one, comes last.
"""

import base64

TRANSPARENT = 0x00


def gcolor8(hex_color):
    """'#RRGGBB' -> the nearest opaque GColor8 (0b11RRGGBB)."""
    value = int(hex_color.lstrip('#'), 16)
    r, g, b = (value >> 16) & 0xff, (value >> 8) & 0xff, value & 0xff
    return 0xC0 | (round(r / 85) << 4) | (round(g / 85) << 2) | round(b / 85)


def encode(width, height, palette, index_at):
    """index_at(x, y) -> palette index for every pixel."""
    stride = (width + 1) // 2
    pixels = bytearray(stride * height)
    for y in range(height):
        for x in range(width):
            shift = 4 if x % 2 == 0 else 0
            pixels[y * stride + x // 2] |= (index_at(x, y) & 0x0f) << shift
    return {
        'pixels': base64.b64encode(bytes(pixels)).decode('ascii'),
        'palette': base64.b64encode(bytes(palette)).decode('ascii'),
        'width': width,
        'height': height,
    }


WHITE = 0xFF


def _in_glyph(glyph, dx, dy, r):
    """Whether (dx, dy) from the centre of a disc of radius r falls in the knocked-out glyph."""
    stroke = max(1.0, r * 0.12)
    if glyph == 'sun':
        return r * 0.3 <= (dx * dx + dy * dy) ** 0.5 <= r * 0.3 + stroke
    if glyph == 'cloud':
        return abs(dy) <= stroke and abs(dx) <= r * 0.55
    if glyph == 'rain':
        return abs(dy) <= r * 0.5 and any(abs(dx - offset * r) <= stroke / 2 for offset in (-0.35, 0, 0.35))
    if glyph == 'snow':
        return any((dx - ox * r) ** 2 + (dy - oy * r) ** 2 <= stroke * stroke
                   for ox, oy in ((0, 0), (-0.35, -0.35), (0.35, -0.35), (-0.35, 0.35), (0.35, 0.35)))
    return False


def disc(size, hex_color, glyph=None):
    """A filled disc on a transparent ground, size x size, with an optional white glyph knocked out
    of it ('sun', 'cloud', 'rain' or 'snow') the way the app draws weather conditions."""
    centre = (size - 1) / 2.0
    radius = size / 2.0

    def index_at(x, y):
        dx, dy = x - centre, y - centre
        if dx * dx + dy * dy > radius * radius:
            return 2
        return 1 if _in_glyph(glyph, dx, dy, radius) else 0
    return encode(size, size, [gcolor8(hex_color), WHITE, TRANSPARENT], index_at)


BLACK = 0xC0


def fill(width, height, hex_color):
    """A width x height block of colour, framed in black and crossed top-left to bottom-right in
    white: a stand-in for album art or an app icon. Full-colour, as those are, so it isn't
    mistaken for a monochrome glyph (two palette entries or fewer)."""
    def index_at(x, y):
        if x in (0, width - 1) or y in (0, height - 1):
            return 2
        return 1 if abs(x * (height - 1) - y * (width - 1)) < max(width, height) else 0
    return encode(width, height, [gcolor8(hex_color), WHITE, BLACK], index_at)


def render(placeholder, icon_pixel_size):
    """A fixture's {"placeholder": "disc"|"fill", "color": "#RRGGBB", "glyph": ...} drawn at the
    subscriber's size.

    A disc is square, at the smaller of the two sides, as the app draws weather glyphs.
    """
    w, h = int(icon_pixel_size['w']), int(icon_pixel_size['h'])
    color = placeholder.get('color', '#000000')
    if placeholder.get('placeholder') == 'disc':
        return disc(min(w, h), color, placeholder.get('glyph'))
    return fill(w, h, color)
