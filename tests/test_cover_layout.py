import random
import unittest
from unittest.mock import patch

from PIL import ImageDraw

from server import main


class CoverLayoutTest(unittest.TestCase):
    def test_fallback_font_preserves_requested_size(self):
        with patch.object(main.os.path, "exists", return_value=False):
            small = main.get_font(20)
            large = main.get_font(80)
        self.assertEqual(large.size, 80)
        self.assertGreater(large.getlength("VIDEO LEGIVEL"), small.getlength("VIDEO LEGIVEL") * 3)

    def test_linux_fonts_are_used_when_windows_fonts_are_missing(self):
        for bold, filename in ((True, "DejaVuSans-Bold.ttf"), (False, "DejaVuSans.ttf")):
            with patch.object(main.os.path, "exists", side_effect=lambda path: path.startswith("/usr/share/fonts/")):
                with patch.object(main.ImageFont, "truetype") as load:
                    self.assertIs(main.get_font(72, bold), load.return_value)
            load.assert_called_once_with("/usr/share/fonts/truetype/dejavu/" + filename, 72)

    def test_cover_text_stays_inside_canvas_and_above_button(self):
        draw_rectangle = ImageDraw.ImageDraw.rounded_rectangle
        draw_centered = main._centered_text
        for niche in ("emagrecimento", "renda extra", "relacionamento", "pele", "assunto geral muito extenso"):
            for seed in range(6):
                with self.subTest(niche=niche, seed=seed):
                    random.seed(seed)
                    rectangles, text = [], []
                    def rectangle(draw, xy, *args, **kwargs):
                        rectangles.append(tuple(xy))
                        return draw_rectangle(draw, xy, *args, **kwargs)
                    def centered(draw, value, y, font, fill, canvas_w=1080):
                        bounds = draw.textbbox((0, y), value, font=font)
                        width = bounds[2] - bounds[0]
                        self.assertLessEqual(width, canvas_w - 80, value)
                        text.append((value, y, bounds[3], font.size))
                        return draw_centered(draw, value, y, font, fill, canvas_w)
                    with patch.object(ImageDraw.ImageDraw, "rounded_rectangle", rectangle):
                        with patch.object(main, "_centered_text", centered):
                            image = main.create_niche_cover_image(niche, "CONHECA TODAS AS ETAPAS PARA COMECAR AGORA MESMO")
                    self.assertEqual(image.size, (1080, 1920))
                    button = rectangles[-1]
                    self.assertGreaterEqual(text[2][3], 72)
                    self.assertTrue(all(item[2] < button[1] - 16 for item in text[2:-2]))
                    self.assertLessEqual(text[-2][2], button[3] - 8)

    def test_button_and_badge_ink_contrast(self):
        self.assertEqual(main._cover_text_color((255, 255, 255)), (0, 0, 0))
        self.assertEqual(main._cover_text_color((251, 191, 36)), (0, 0, 0))
        self.assertEqual(main._cover_text_color((10, 15, 25)), (255, 255, 255))
