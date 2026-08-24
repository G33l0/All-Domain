#!/usr/bin/env python3
"""Generate the brand assets from the single source of truth in ``logo.py``.

    python packaging/make_icons.py

Writes into ``assets/``:

* ``domain-atlas.ico``  - multi-resolution Windows icon (16-256px)
* ``domain-atlas.png``  - 512px app image
* ``logo.svg``              - vector mark
* ``logo-wordmark.svg``     - mark plus product name, for the README

The .ico is assembled by hand: PNG-compressed entries for the large sizes (the
format Windows prefers since Vista) and uncompressed 32-bit BMP entries for
16-48px, which is what older shells and some installers still expect.
"""

from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from domainatlas.qtui.logo import (  # noqa: E402
    BRAND_END,
    BRAND_START,
    ICON_SIZES,
    logo_pixmap,
    logo_svg,
)

#: Sizes stored as BMP; everything larger is stored as PNG.
BMP_MAX = 48


def _png_bytes(image: QImage) -> bytes:
    # The QByteArray must outlive the QBuffer that writes into it - handing a
    # temporary to QBuffer() leaves it pointing at freed memory.
    storage = QByteArray()
    buffer = QBuffer(storage)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(storage)


def _bmp_bytes(image: QImage) -> bytes:
    """A 32-bit bottom-up DIB with the AND mask an .ico entry requires."""
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    width, height = image.width(), image.height()
    header = struct.pack(
        "<IiiHHIIiiII",
        40,             # biSize
        width,
        height * 2,     # biHeight: XOR image + AND mask
        1,              # biPlanes
        32,             # biBitCount
        0,              # biCompression = BI_RGB
        0,              # biSizeImage
        0, 0, 0, 0,     # resolution and palette counts
    )
    pixels = bytearray()
    for y in range(height - 1, -1, -1):  # bottom-up
        for x in range(width):
            colour = image.pixelColor(x, y)
            pixels += bytes((colour.blue(), colour.green(), colour.red(), colour.alpha()))
    # The AND mask is unused for 32-bit icons but must be present and padded.
    mask_row = ((width + 31) // 32) * 4
    mask = bytes(mask_row * height)
    return bytes(header) + bytes(pixels) + mask


def build_ico(path: str) -> None:
    entries = []
    for size in ICON_SIZES:
        image = logo_pixmap(size).toImage()
        payload = _bmp_bytes(image) if size <= BMP_MAX else _png_bytes(image)
        entries.append((size, payload))

    offset = 6 + 16 * len(entries)
    directory = struct.pack("<HHH", 0, 1, len(entries))
    body = b""
    for size, payload in entries:
        directory += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,   # 0 means 256
            0 if size >= 256 else size,
            0,                            # colours in palette
            0,                            # reserved
            1,                            # colour planes
            32,                           # bits per pixel
            len(payload),
            offset,
        )
        body += payload
        offset += len(payload)
    with open(path, "wb") as handle:
        handle.write(directory + body)


def build_wordmark(path: str) -> None:
    mark = logo_svg(64)
    inner = mark.split(">", 1)[1].rsplit("</svg>", 1)[0]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 360 72" width="360" height="72">\n'
            f'  <g transform="translate(4 4)">{inner}</g>\n'
            f'  <text x="84" y="38" font-family="Segoe UI Variable Display, Segoe UI, Inter, '
            f'sans-serif" font-size="27" font-weight="600" fill="{BRAND_START}">Domain Atlas</text>\n'
            f'  <text x="85" y="58" font-family="Segoe UI, Inter, sans-serif" font-size="13" '
            f'fill="{BRAND_END}">Asynchronous domain intelligence</text>\n'
            f'</svg>\n'
        )


def main() -> int:
    QApplication.instance() or QApplication([])  # QPixmap needs an application
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assets = os.path.join(root, "assets")
    os.makedirs(assets, exist_ok=True)

    ico_path = os.path.join(assets, "domain-atlas.ico")
    build_ico(ico_path)
    logo_pixmap(512).save(os.path.join(assets, "domain-atlas.png"))
    with open(os.path.join(assets, "logo.svg"), "w", encoding="utf-8") as handle:
        handle.write(logo_svg(256))
    build_wordmark(os.path.join(assets, "logo-wordmark.svg"))

    print(f"wrote {ico_path} ({os.path.getsize(ico_path):,} bytes)")
    for name in ("domain-atlas.png", "logo.svg", "logo-wordmark.svg"):
        print(f"wrote {os.path.join(assets, name)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
