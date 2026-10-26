from auth_helpers import authorize_owner
import copy
import hashlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image
from server import main


def average_hash(content):
    with Image.open(io.BytesIO(content)) as image:
        pixels = list(image.convert('L').resize((8, 8)).getdata())
    mean = sum(pixels) / len(pixels)
    return tuple(pixel >= mean for pixel in pixels)


class FunctionalClaimsTest(unittest.TestCase):
    def setUp(self):
        stack = patch.object(main.app.app, 'middleware_stack', None)
        stack.start()
        self.addCleanup(stack.stop)
        temporary = tempfile.TemporaryDirectory(prefix='cads-tests-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.processed = self.root / 'processed'
        self.processed.mkdir()
        patches = patch.multiple(main, PROCESSED_DIR=str(self.processed),
                                 DB_PATH=str(self.root / 'test.db'), STATS=copy.deepcopy(main.STATS))
        patches.start()
        self.addCleanup(patches.stop)
        main.init_db()
        self.client = TestClient(main.app)
        authorize_owner(self, self.client, self.root)

    def tagged_video(self):
        path = self.root / 'original.mp4'
        ffmpeg = main.imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run([ffmpeg, '-y', '-f', 'lavfi', '-i', 'color=c=blue:s=128x128:r=12',
                        '-t', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                        '-metadata', 'title=CADS_PRIVATE_TITLE', '-metadata', 'comment=CADS_PRIVATE_COMMENT',
                        str(path)], check=True, capture_output=True, timeout=30)
        return path.read_bytes()

    def test_image_exif_is_removed_and_orientation_is_preserved(self):
        photo = io.BytesIO()
        exif = Image.Exif()
        exif[305] = 'CADS_PRIVATE_SOFTWARE'
        exif[274] = 6
        Image.new('RGB', (64, 32), 'blue').save(photo, 'JPEG', exif=exif)
        original = photo.getvalue()
        response = self.client.post('/api/metadata/clean', files={'file': ('photo.jpg', original, 'image/jpeg')})
        self.assertEqual(response.status_code, 200, response.text)
        result = (self.processed / response.json()['filename']).read_bytes()
        with Image.open(io.BytesIO(result)) as image:
            self.assertFalse(image.getexif())
            self.assertEqual(image.size, (32, 64))
        self.assertNotIn(b'CADS_PRIVATE_SOFTWARE', result)
        self.assertIn(b'CADS_PRIVATE_SOFTWARE', original)

    def test_video_descriptive_metadata_removed_with_duration_preserved(self):
        original = self.tagged_video()
        self.assertIn(b'CADS_PRIVATE_TITLE', original)
        response = self.client.post('/api/metadata/clean', files={'file': ('movie.mp4', original, 'video/mp4')})
        self.assertEqual(response.status_code, 200, response.text)
        output = self.processed / response.json()['filename']
        self.assertNotIn(b'CADS_PRIVATE_TITLE', output.read_bytes())
        self.assertNotIn(b'CADS_PRIVATE_COMMENT', output.read_bytes())
        duration, _ = main.probe_media(main.imageio_ffmpeg.get_ffmpeg_exe(), str(output))
        self.assertAlmostEqual(duration, 1, delta=0.1)

    def test_video_without_cover_or_padding_has_no_extra_intro(self):
        response = self.client.post('/api/camouflage/video', files={'file': ('movie.mp4', self.tagged_video(), 'video/mp4')})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()['cover'])
        self.assertAlmostEqual(response.json()['duration_seconds'], 1, delta=0.1)

    def test_invalid_files_are_not_reported_as_processed(self):
        for route, name, expected in [('/api/metadata/clean', 'file.txt', 415),
                                       ('/api/metadata/clean', 'file.mp4', 422),
                                       ('/api/camouflage/video', 'file.mp4', 422),
                                       ('/api/camouflage/image', 'file.png', 400)]:
            with self.subTest(route=route, name=name):
                response = self.client.post(route, files={'file': (name, b'not-valid-media')})
                self.assertEqual(response.status_code, expected, response.text)

    def test_changed_binary_hash_does_not_prevent_perceptual_match(self):
        photo = io.BytesIO()
        Image.new('RGB', (64, 64), 'blue').save(photo, 'PNG')
        original = photo.getvalue()
        for level in ('basica', 'normal', 'agressiva'):
            response = self.client.post('/api/camouflage/image', data={'level': level},
                                        files={'file': ('original.png', original, 'image/png')})
            self.assertEqual(response.status_code, 200, response.text)
            result = (self.processed / response.json()['filename']).read_bytes()
            self.assertNotEqual(hashlib.sha256(original).digest(), hashlib.sha256(result).digest())
            self.assertEqual(average_hash(original), average_hash(result))

    def test_scripts_use_product_and_do_not_invent_discounts(self):
        scripts = []
        for product in ('Fone azul', 'Camiseta preta'):
            response = self.client.post('/api/scripts/generate-white-script', data={'black_offer': product})
            self.assertEqual(response.status_code, 200)
            for template in response.json()['templates']:
                self.assertIn(product, template['script'])
                self.assertNotIn('desconto', template['script'])
            scripts.append(response.json()['templates'])
        self.assertNotEqual(*scripts)

    def test_local_term_scan_never_promises_approval_or_hides_product_name(self):
        response = self.client.post('/api/compliance/analyze', data={'copy_text': 'tirzepatida'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['risk_level'], 'MÉDIO')
        self.assertEqual(response.json()['safe_rewrite'], 'tirzepatida')
        response = self.client.post('/api/compliance/analyze', data={'copy_text': 'Produto com informacoes detalhadas.'})
        self.assertIn('nao comprova', response.json()['recommendation'])


if __name__ == '__main__':
    unittest.main()
