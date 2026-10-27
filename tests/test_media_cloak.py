import copy
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from auth_helpers import authorize_owner
from server import accounts, main, media_cloak


class MediaFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="media-cloak-fixtures-")
        cls.root = Path(cls.temporary.name)
        cls.ffmpeg = main.imageio_ffmpeg.get_ffmpeg_exe()
        cls.ffprobe = os.environ.get("FFPROBE_EXE") or shutil.which("ffprobe")
        cls.audio = cls.root / "source.wav"
        cls.video = cls.root / "source.mp4"
        cls.cover = cls.root / "cover.png"
        Image.new("RGB", (160, 120), "red").save(cls.cover)
        for path, inputs, codecs in (
            (cls.audio, ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"], []),
            (cls.video, ["-f", "lavfi", "-i", "testsrc2=size=160x120:rate=30",
                         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"],
             ["-c:v", "libx264", "-c:a", "aac"]),
        ):
            subprocess.run([cls.ffmpeg, "-y", *inputs, "-t", "4", *codecs,
                            "-metadata", "title=PRIVATE_TITLE", "-metadata", "comment=PRIVATE_COMMENT",
                            "-metadata:s", "title=PRIVATE_STREAM", str(path)],
                           check=True, capture_output=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="media-cloak-test-")
        self.addCleanup(temp.cleanup)
        self.output = Path(temp.name)

    def probe(self, path):
        if not self.ffprobe:
            self.skipTest("ffprobe required: set FFPROBE_EXE or add ffprobe to PATH")
        result = subprocess.run([self.ffprobe, "-v", "error", "-show_format", "-show_streams",
                                 "-of", "json", str(path)], check=True, capture_output=True,
                                text=True, timeout=30)
        return json.loads(result.stdout)

    def assert_transformed(self, source, output, kind):
        self.assertNotEqual(hashlib.sha256(source.read_bytes()).digest(),
                            hashlib.sha256(output.read_bytes()).digest())
        before = media_cloak._probe(source, kind)[0]
        after = media_cloak._probe(output, kind)[0]
        self.assertLess(abs(after - before) / before, 0.05)
        self.assertFalse(list(self.output.glob("_tmp_*")))
        subprocess.run([self.ffmpeg, "-v", "error", "-i", str(output), "-f", "null", "-"],
                       check=True, capture_output=True, timeout=30)

class MediaCloakTest(MediaFixtures):
    def test_audio_each_level(self):
        for level in media_cloak.LEVELS:
            with self.subTest(level=level):
                output = self.output / f"{level}.m4a"
                media_cloak.cloak_audio(self.audio, output, level)
                self.assert_transformed(self.audio, output, "Audio")

    def test_video_each_level_with_and_without_cover(self):
        for level in media_cloak.LEVELS:
            for cover in (None, self.cover):
                with self.subTest(level=level, cover=cover):
                    output = self.output / f"{level}-{bool(cover)}.mp4"
                    media_cloak.cloak_video(self.video, output, level, cover)
                    self.assert_transformed(self.video, output, "Video")
                    info = self.probe(output)
                    self.assertEqual([s["codec_name"] for s in info["streams"]], ["h264", "aac"])
                    if cover:
                        frame = subprocess.run([self.ffmpeg, "-v", "error", "-i", str(output),
                                                "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                                               check=True, capture_output=True, timeout=30).stdout
                        self.assertGreater(sum(frame[0::3]), sum(frame[2::3]) * 3)

    def test_metadata_removed_at_each_level(self):
        for kind, source, engine, extension in (("Audio", self.audio, media_cloak.cloak_audio, "m4a"),
                                                 ("Video", self.video, media_cloak.cloak_video, "mp4")):
            self.assertIn("PRIVATE_TITLE", json.dumps(self.probe(source)))
            for level in media_cloak.LEVELS:
                with self.subTest(kind=kind, level=level):
                    output = self.output / f"clean-{kind}-{level}.{extension}"
                    engine(source, output, level)
                    info = self.probe(output)
                    self.assertNotIn("PRIVATE_", json.dumps(info))
                    # Container structure (brands, language, vendor id) is not user metadata.
                    for obj in [info["format"], *info["streams"]]:
                        self.assertTrue(set(obj.get("tags", {})) <= {
                            "major_brand", "minor_version", "compatible_brands", "language", "vendor_id"}, obj)

    def test_video_without_audio_and_odd_dimensions(self):
        source = self.output / "silent.mov"
        subprocess.run([self.ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc=size=161x121:rate=24",
                        "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv444p", str(source)],
                       check=True, capture_output=True, timeout=30)
        output = self.output / "silent.mp4"
        media_cloak.cloak_video(source, output)
        self.assert_transformed(source, output, "Video")
        self.assertEqual(len(self.probe(output)["streams"]), 1)

    def test_phone_rotation_is_preserved_visually(self):
        source = self.output / "rotated.mp4"
        subprocess.run([self.ffmpeg, "-y", "-display_rotation:v:0", "90", "-i", str(self.video),
                        "-c", "copy", str(source)],
                       check=True, capture_output=True, timeout=30)
        output = self.output / "upright.mp4"
        media_cloak.cloak_video(source, output)
        video = self.probe(output)["streams"][0]
        self.assertEqual((video["width"], video["height"]), (120, 160))
        self.assert_transformed(source, output, "Video")

    def test_invalid_file_and_level_leave_output_untouched(self):
        source = self.output / "invalid"
        source.write_bytes(b"not media")
        output = self.output / "output.mp4"
        output.write_bytes(b"existing output")
        for engine in (media_cloak.cloak_audio, media_cloak.cloak_video):
            with self.subTest(engine=engine.__name__):
                with self.assertRaises(media_cloak.InvalidMedia):
                    engine(source, output)
                with self.assertRaises((KeyError, ValueError)):
                    engine(source, output, "invalid")
                self.assertEqual(output.read_bytes(), b"existing output")
        self.assertFalse(list(self.output.glob("_tmp_*")))

    def test_timeout_cleans_partial_output_and_logs_stderr(self):
        real_run = subprocess.run
        def timeout_encode(command, **kwargs):
            self.assertEqual(kwargs["timeout"], 300)
            self.assertFalse(kwargs.get("shell", False))
            if "-y" in command:
                Path(command[-1]).write_bytes(b"partial")
                raise subprocess.TimeoutExpired(command, 300, stderr="timeout diagnostic")
            return real_run(command, **kwargs)
        for engine, source in ((media_cloak.cloak_audio, self.audio), (media_cloak.cloak_video, self.video)):
            output = self.output / "timeout.mp4"
            with patch.object(media_cloak.subprocess, "run", side_effect=timeout_encode):
                with self.assertLogs(media_cloak.logger, level="ERROR") as logs:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        engine(source, output)
            self.assertIn("timeout diagnostic", " ".join(logs.output))
            self.assertFalse(output.exists())
            self.assertFalse(list(self.output.glob("_tmp_*")))


class MediaCloakAPITest(MediaFixtures):
    def setUp(self):
        super().setUp()
        for change in (
            patch.object(main.app.app, "middleware_stack", None),
            patch.multiple(main, PROCESSED_DIR=str(self.output), DB_PATH=str(self.output / "auth.db"),
                           STATS=copy.deepcopy(main.STATS), CUSTOMER_STATS={}),
        ):
            change.start()
            self.addCleanup(change.stop)
        main.init_db()
        self.client = TestClient(main.app)
        authorize_owner(self, self.client, self.output)

    def test_api_audio_and_video_levels_and_legacy_response(self):
        for kind, source in (("audio", self.audio), ("video", self.video)):
            for level in media_cloak.LEVELS:
                with self.subTest(kind=kind, level=level):
                    data = {"cloak_level" if kind == "video" else "level": level}
                    response = self.client.post(f"/api/camouflage/{kind}", data=data,
                                                files={"file": (source.name, source.read_bytes())})
                    self.assertEqual(response.status_code, 200, response.text)
                    result = response.json()
                    self.assertEqual(result["status"], "success")
                    output = self.output / result["filename"]
                    self.assertEqual(result["new_hash"], hashlib.sha256(output.read_bytes()).hexdigest())
                    download = self.client.get(result["download_url"])
                    self.assertEqual(download.content, output.read_bytes())
                    if kind == "video":
                        self.assertTrue({"duration", "duration_seconds", "new_sha256", "cover"} <= result.keys())
                        self.assertEqual(result["level"], "normal")
        stats = self.client.get("/api/stats").json()
        self.assertEqual(stats["audio_cloaked_today"], 3)
        self.assertEqual(stats["video_cloaked_today"], 3)

    def test_api_invalid_inputs(self):
        for kind, name in (("audio", "invalid.wav"), ("video", "invalid.mp4")):
            for data, filename, content in (({}, name, b"invalid"),
                                             ({"cloak_level" if kind == "video" else "level": "invalid"}, name, b"invalid"),
                                             ({}, "file.txt", b"invalid")):
                response = self.client.post(f"/api/camouflage/{kind}", data=data,
                                            files={"file": (filename, content)})
                self.assertIn(response.status_code, (400, 422), response.text)
        self.assertFalse(list(self.output.glob("cads_*")))
        self.assertFalse(list(self.output.glob("_tmp_*")))

    def test_api_audio_timeout(self):
        with patch.object(media_cloak, "cloak_audio", side_effect=subprocess.TimeoutExpired("ffmpeg", 300)):
            response = self.client.post("/api/camouflage/audio",
                                        files={"file": ("sound.wav", self.audio.read_bytes())})
        self.assertEqual(response.status_code, 504, response.text)
        self.assertFalse(list(self.output.glob("cads_*")))

    def test_new_video_motor_is_last_and_defaults_to_normal(self):
        real_engine = media_cloak.cloak_video
        def final_stage(source, output, level):
            self.assertEqual(source, output)
            self.assertTrue(Path(source).is_file())
            self.assertEqual(level, "normal")
            real_engine(source, output, level)
        with patch.object(media_cloak, "cloak_video", side_effect=final_stage) as engine:
            response = self.client.post("/api/camouflage/video",
                                        files={"file": ("clip.mp4", self.video.read_bytes())})
        self.assertEqual(response.status_code, 200, response.text)
        engine.assert_called_once()

    def test_supported_audio_and_video_containers(self):
        for extension, codec in (("mp3", "libmp3lame"), ("aac", "aac"), ("ogg", "libvorbis")):
            source = self.output / ("source." + extension)
            subprocess.run([self.ffmpeg, "-y", "-i", str(self.audio), "-c:a", codec, str(source)],
                           check=True, capture_output=True, timeout=30)
            response = self.client.post("/api/camouflage/audio", files={"file": (source.name, source.read_bytes())})
            self.assertEqual(response.status_code, 200, response.text)
        for extension, codec in (("mov", "libx264"), ("webm", "libvpx")):
            source = self.output / ("source." + extension)
            subprocess.run([self.ffmpeg, "-y", "-i", str(self.video), "-an", "-c:v", codec, str(source)],
                           check=True, capture_output=True, timeout=30)
            response = self.client.post("/api/camouflage/video", files={"file": (source.name, source.read_bytes())})
            self.assertEqual(response.status_code, 200, response.text)
            output = self.output / response.json()["filename"]
            self.assertEqual(self.probe(output)["streams"][0]["codec_name"], "h264")

    def test_upload_limit_is_enforced_in_chunks(self):
        class Upload:
            def __init__(self, size):
                self.remaining = size
            async def read(self, size):
                chunk = b"x" * min(size, self.remaining)
                self.remaining -= len(chunk)
                return chunk
        for kind, size, name in (("audio", 50, "source.wav"), ("video", 300, "source.mp4")):
            async def too_large(upload, path, limit):
                self.assertEqual(limit, size * 1024 * 1024)
                # Exercise the real reader at a small boundary without a 300 MB fixture.
                await reader(Upload(1025), path, 1024)
            reader = main._save_cloak_upload
            with patch.object(main, "_save_cloak_upload", side_effect=too_large):
                response = self.client.post(f"/api/camouflage/{kind}", files={"file": (name, b"x")})
            self.assertEqual(response.status_code, 413, response.text)
        exact = self.output / "exact"
        asyncio.run(reader(Upload(1024), exact, 1024))
        self.assertEqual(exact.stat().st_size, 1024)
        self.assertFalse(list(self.output.glob("_tmp_*")))

    def test_video_final_timeout_removes_output_and_does_not_count(self):
        before = copy.deepcopy(main.STATS)
        with patch.object(media_cloak, "cloak_video", side_effect=subprocess.TimeoutExpired("ffmpeg", 300)):
            response = self.client.post("/api/camouflage/video", files={"file": ("source.mp4", self.video.read_bytes())})
        self.assertEqual(response.status_code, 504, response.text)
        self.assertEqual(main.STATS, before)
        self.assertFalse(list(self.output.glob("cads_*")))
        self.assertFalse(list(self.output.glob("_tmp_*")))

    def test_customer_downloads_and_stats_are_isolated(self):
        customers = []
        for number in (1, 2):
            customer_id = accounts.create_customer(f"Customer {number}", f"{number}@example.test",
                                                   "test-only-customer-password", status="approved")
            client = TestClient(main.app)
            client.cookies.set(accounts.COOKIE, accounts.new_session(customer_id))
            customers.append(client)
        for kind, source in (("audio", self.audio), ("video", self.video)):
            response = customers[0].post(f"/api/camouflage/{kind}", files={"file": (source.name, source.read_bytes())})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(customers[0].get(response.json()["download_url"]).status_code, 200)
            self.assertEqual(customers[1].get(response.json()["download_url"]).status_code, 404)
            self.assertEqual(customers[0].get("/api/stats").json()[f"{kind}_cloaked_today"], 1)
            self.assertEqual(customers[1].get("/api/stats").json()[f"{kind}_cloaked_today"], 0)
