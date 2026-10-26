import base64
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from server import product_video as studio

app = FastAPI()
app.include_router(studio.router)
client = TestClient(app)


def photo():
    output = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(output, "PNG")
    return output.getvalue()


class ProductVideoTest(unittest.TestCase):
    def submit(self, content=None, **fields):
        data = {"product_name": "Fone preto", "scene": "Estúdio neutro com câmera lenta.",
                "api_key": "test-key", "provider": "gemini_paid", **fields}
        return client.post("/api/studio/generate", data=data,
                           files={"file": ("produto.png", photo() if content is None else content, "image/png")})

    def test_missing_key_does_not_call_google(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(studio, "request_video") as generate:
            self.assertEqual(self.submit(api_key="").status_code, 503)
            generate.assert_not_called()

    def test_bad_image_and_format_do_not_call_google(self):
        with patch.object(studio, "request_video") as generate:
            self.assertEqual(self.submit(content=b"not a photo").status_code, 400)
            self.assertEqual(self.submit(aspect_ratio="1:1").status_code, 400)
            generate.assert_not_called()

    def test_other_sites_cannot_start_paid_generation(self):
        response = client.post("/api/studio/generate", headers={"Origin": "https://unrelated.example"},
                               data={"product_name": "Fone", "scene": "Câmera lenta em estúdio.", "api_key": "test-key"},
                               files={"file": ("produto.png", photo(), "image/png")})
        self.assertEqual(response.status_code, 403)

    def test_success_saves_unique_video_and_never_returns_key(self):
        video = b"\x00\x00\x00\x18ftypisomtest"
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(studio, "PROCESSED_DIR", Path(directory)), \
                patch.object(studio, "request_video", return_value=video) as generate:
            first = self.submit()
            second = self.submit()
            self.assertEqual(first.status_code, 200)
            data = first.json()
            self.assertNotEqual(data["filename"], second.json()["filename"])
            self.assertEqual((Path(directory) / data["filename"]).read_bytes(), video)
            self.assertNotIn("test-key", first.text)
            self.assertEqual(generate.call_args.args[0], "gemini_paid")
            self.assertEqual(generate.call_args.args[3], "9:16")

    def test_google_rest_schema_and_secret_header(self):
        video = b"\x00\x00\x00\x18ftypisomtest"
        payload = {"status": "completed", "steps": [{"type": "model_output", "content": [
            {"type": "video", "mime_type": "video/mp4", "data": base64.b64encode(video).decode()}
        ]}]}
        with patch.object(studio.urllib.request, "urlopen") as open_url:
            open_url.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            self.assertEqual(studio.request_video("gemini_paid", "image", "prompt", "9:16", "test-key"), video)
            request = open_url.call_args.args[0]
            self.assertNotIn("test-key", request.full_url)
            self.assertEqual(request.get_header("X-goog-api-key"), "test-key")
            body = json.loads(request.data)
            self.assertEqual(body["model"], studio.PROVIDERS["gemini_paid"]["model"])
            self.assertEqual(body["response_format"]["delivery"], "inline")
            self.assertFalse(body["store"])

    def test_blocked_generation_is_not_reported_as_success(self):
        with patch.object(studio.urllib.request, "urlopen") as open_url:
            open_url.return_value.__enter__.return_value.read.return_value = b'{"steps": [], "status":"failed"}'
            with self.assertRaises(studio.HTTPException) as error:
                studio.request_video("gemini_paid", "image", "prompt", "9:16", "test-key")
            self.assertEqual(error.exception.status_code, 502)

    def test_nonvideo_provider_does_not_make_a_request(self):
        with patch.object(studio.urllib.request, 'urlopen') as request:
            self.assertEqual(self.submit(provider='gemini_free').status_code, 400)
            request.assert_not_called()

    def test_invalid_video_is_not_saved(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(studio, 'PROCESSED_DIR', Path(directory)), \
                patch.object(studio, 'request_video', return_value=b'<html>error</html>'):
            self.assertEqual(self.submit().status_code, 502)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_config_reports_each_provider_and_capabilities(self):
        with patch.dict(os.environ, {'DEAPI_KEY': 'test-key'}, clear=True):
            config = client.get('/api/studio/config').json()
        self.assertTrue(config['providers']['deapi_ltx']['configured'])
        self.assertFalse(config['providers']['gemini_paid']['configured'])
        self.assertFalse(config['providers']['fal_svd']['supports_scene'])
        self.assertEqual(config['default'], 'deapi_ltx')

    def test_deapi_uses_model_specs_multipart_and_job_polling(self):
        image = io.BytesIO()
        Image.effect_noise((128, 128), 64).convert('RGB').save(image, 'JPEG')
        responses = [
            {'data': [{'slug': studio.PROVIDERS['deapi_ltx']['model'], 'inference_types': ['img2video'],
                       'info': {'limits': {'resolution_step': 32, 'min_width': 256, 'max_width': 1536,
                                            'min_height': 256, 'max_height': 1536, 'min_frames': 1, 'max_frames': 121},
                                'defaults': {'fps': 30, 'frames': 120, 'steps': 4},
                                'features': {'supports_steps': True}}}]},
            {'data': {'request_id': 'job-123'}},
            {'data': {'status': 'processing'}},
            {'data': {'status': 'done', 'result_url': 'https://cdn.example/video.mp4'}},
        ]
        with patch.object(studio.urllib.request, 'urlopen') as request, \
                patch.object(studio, 'download_video', return_value=b'valid-video') as download, \
                patch.object(studio.time, 'sleep'):
            request.return_value.__enter__.return_value.read.side_effect = [json.dumps(r).encode() for r in responses]
            self.assertEqual(studio.request_video_deapi(base64.b64encode(image.getvalue()).decode(),
                                                       'product scene', '9:16', 'secret-key'), b'valid-video')
            submitted = request.call_args_list[1].args[0]
            self.assertEqual(submitted.full_url, 'https://api.deapi.ai/api/v2/videos/animations')
            self.assertIn(b'name="first_frame_image"', submitted.data)
            self.assertIn(b'name="width"\r\n\r\n576', submitted.data)
            self.assertIn(b'name="height"\r\n\r\n1024', submitted.data)
            self.assertIn(b'product scene', submitted.data)
            self.assertNotIn('secret-key', submitted.full_url)
            self.assertTrue(request.call_args.args[0].full_url.endswith('/api/v2/jobs/job-123'))
            download.assert_called_once_with('https://cdn.example/video.mp4')

    def test_deapi_unavailable_model_does_not_submit_generation(self):
        with patch.object(studio.urllib.request, 'urlopen') as request:
            request.return_value.__enter__.return_value.read.return_value = b'{"data":[]}'
            with self.assertRaises(studio.HTTPException) as error:
                studio.request_video_deapi('image', 'prompt', '9:16', 'key')
            self.assertEqual(error.exception.status_code, 422)
            self.assertEqual(request.call_count, 1)

    def test_deapi_access_denied_is_actionable_and_does_not_leak_key(self):
        error = urllib.error.HTTPError('https://api.deapi.ai/api/v2/models', 403, 'Forbidden', {}, None)
        with patch.object(studio.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaises(studio.HTTPException) as caught:
                studio.request_video_deapi('image', 'prompt', '9:16', 'secret-key')
            self.assertEqual(caught.exception.status_code, 502)
            self.assertIn('permissoes', caught.exception.detail)
            self.assertNotIn('secret-key', caught.exception.detail)

    def test_generation_lock_prevents_duplicate_paid_calls(self):
        studio.GENERATION_LOCK.acquire()
        try:
            with patch.object(studio, 'request_video') as generate:
                self.assertEqual(self.submit().status_code, 409)
                generate.assert_not_called()
        finally:
            studio.GENERATION_LOCK.release()

    def test_download_refuses_private_urls(self):
        with patch.object(studio.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaises(studio.HTTPException):
                studio.download_video('https://127.0.0.1/private')

    def test_fal_uses_documented_endpoint_and_parameter(self):
        responses = [{'request_id': 'job-123'}, {'status': 'COMPLETED'},
                     {'video': {'url': 'https://cdn.example/video.mp4'}}]
        with patch.object(studio.urllib.request, 'urlopen') as request, \
                patch.object(studio.time, 'sleep'), patch.object(studio, 'download_video', return_value=b'video'):
            request.return_value.__enter__.return_value.read.side_effect = [json.dumps(r).encode() for r in responses]
            self.assertEqual(studio.request_video_fal('image', 'prompt', '9:16', 'key'), b'video')
            self.assertEqual(request.call_args_list[0].args[0].full_url, 'https://queue.fal.run/fal-ai/stable-video')
            self.assertIn('cond_aug', json.loads(request.call_args_list[0].args[0].data))


if __name__ == "__main__":
    unittest.main()
