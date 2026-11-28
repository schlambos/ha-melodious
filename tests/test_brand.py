"""The integration's own brand images (Home Assistant 2026.3 and newer).

Home Assistant serves custom_components/<domain>/brand/ before the brands
CDN. The brands spec wants square PNG icons, 256 px and 512 px for @2x.
"""

import struct
import unittest
from pathlib import Path

BRAND = Path(__file__).resolve().parents[1] / "custom_components/melodious/brand"


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()[:24]
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise AssertionError(f"{path.name} is not a PNG")
    return struct.unpack(">II", data[16:24])


class BrandTests(unittest.TestCase):
    def test_icons_have_the_brands_sizes(self):
        self.assertEqual(png_size(BRAND / "icon.png"), (256, 256))
        self.assertEqual(png_size(BRAND / "icon@2x.png"), (512, 512))

    def test_only_names_home_assistant_serves(self):
        allowed = {f"{dark}{kind}{hdpi}.png" for dark in ("", "dark_")
                   for kind in ("icon", "logo") for hdpi in ("", "@2x")}
        self.assertLessEqual({p.name for p in BRAND.iterdir()}, allowed)


if __name__ == "__main__":
    unittest.main()
