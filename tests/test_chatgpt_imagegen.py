import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "chatgpt_imagegen", ROOT / ".claude" / "skills" / "chatgpt-imagegen" / "generate.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def png_bytes(width=3, height=2):
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" +
            width.to_bytes(4, "big") + height.to_bytes(4, "big"))


def jpeg_bytes(width=5, height=4):
    return (b"\xff\xd8" + b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08" +
            height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x03")


def webp_bytes(width=7, height=6):
    data = bytearray(b"RIFF" + (22).to_bytes(4, "little") + b"WEBP" +
                     b"VP8X" + (10).to_bytes(4, "little") + b"\x00\x00\x00\x00")
    data += (width - 1).to_bytes(3, "little")
    data += (height - 1).to_bytes(3, "little")
    return bytes(data)


class ImageHelpersTest(unittest.TestCase):
    def test_local_timezone(self):
        offset, name = MODULE.local_timezone()
        self.assertIsInstance(offset, int)
        self.assertGreaterEqual(offset, -14 * 60)
        self.assertLessEqual(offset, 14 * 60)
        self.assertTrue(name)

    def test_auth_file_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.json"
            path.write_text('{"tokens": {"access_token": "fake-token"}}', encoding="utf-8")
            self.assertEqual(MODULE.load_access_token(False, str(path)), "fake-token")

    def test_sniff_image_mime(self):
        self.assertEqual(MODULE.sniff_image_mime(png_bytes()), "image/png")
        self.assertEqual(MODULE.sniff_image_mime(jpeg_bytes()), "image/jpeg")
        self.assertEqual(MODULE.sniff_image_mime(webp_bytes()), "image/webp")
        self.assertEqual(MODULE.sniff_image_mime(b"not an image"), "")

    def test_image_dimensions(self):
        self.assertEqual(MODULE.image_dimensions(png_bytes(3, 2), "image/png"), (3, 2))
        self.assertEqual(MODULE.image_dimensions(jpeg_bytes(5, 4), "image/jpeg"), (5, 4))
        self.assertEqual(MODULE.image_dimensions(webp_bytes(7, 6), "image/webp"), (7, 6))
        self.assertEqual(MODULE.image_dimensions(b"", "image/png"), (1, 1))

    def test_load_reference_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ref.png"
            path.write_bytes(png_bytes(11, 9))
            loaded = MODULE.load_reference_images([str(path)])
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["mime_type"], "image/png")
        self.assertEqual((loaded[0]["width"], loaded[0]["height"]), (11, 9))
        self.assertEqual(loaded[0]["size"], len(png_bytes(11, 9)))


class MessagePayloadTest(unittest.TestCase):
    def image(self, file_id, name="ref.png", width=3, height=2):
        return {
            "file_id": file_id,
            "file_name": name,
            "mime_type": "image/png",
            "size": 123,
            "width": width,
            "height": height,
        }

    def test_text_message_is_unchanged(self):
        message = MODULE.build_user_message("hello", [])
        self.assertEqual(message["content"], {"content_type": "text", "parts": ["hello"]})
        self.assertNotIn("attachments", message["metadata"])

    def test_multimodal_message_uses_asset_pointer_and_attachments(self):
        message = MODULE.build_user_message(
            "make it blue", [self.image("file-abc"), self.image("file_xyz")])
        content = message["content"]
        self.assertEqual(content["content_type"], "multimodal_text")
        self.assertEqual(content["parts"][0]["asset_pointer"], "file-service://file-abc")
        self.assertEqual(content["parts"][0]["content_type"], "image_asset_pointer")
        self.assertEqual(content["parts"][1]["asset_pointer"], "sediment://file_xyz")
        self.assertEqual(content["parts"][-1], "make it blue")
        attachments = message["metadata"]["attachments"]
        self.assertEqual(attachments[0]["id"], "file-abc")
        self.assertEqual(attachments[0]["mime_type"], "image/png")


class FakeResponse:
    def __init__(self, status_code=200, lines=None, body=b"{}"):
        self.status_code = status_code
        self._lines = lines or []
        self.content = body

    def iter_lines(self):
        yield from self._lines

    def close(self):
        pass


class FakeSession:
    def __init__(self, response=None):
        self.response = response or FakeResponse()
        self.calls = []

    def put(self, url, **kwargs):
        self.calls.append(("put", url, kwargs))
        return self.response

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return self.response

    def patch(self, url, **kwargs):
        self.calls.append(("patch", url, kwargs))
        return self.response

    def request(self, method, url, **kwargs):
        if method.upper() == "PUT":
            return self.put(url, **kwargs)
        if method.upper() == "PATCH":
            return self.patch(url, **kwargs)
        return self.post(url, **kwargs)


class UploadHelpersTest(unittest.TestCase):
    def test_direct_azure_upload_headers(self):
        session = FakeSession(FakeResponse(201))
        client = MODULE.Client.__new__(MODULE.Client)
        client.session = session
        image = {"file_name": "ref.png", "mime_type": "image/png", "data": b"abc"}
        client._upload_blob("https://blob.example/upload?sig=1", image, None)
        kind, _, kwargs = session.calls[0]
        self.assertEqual(kind, "put")
        self.assertEqual(kwargs["data"], b"abc")
        self.assertEqual(kwargs["headers"]["x-ms-blob-type"], "BlockBlob")
        self.assertEqual(kwargs["headers"]["Content-Type"], "image/png")

    def test_aws_upload_does_not_send_azure_headers(self):
        session = FakeSession(FakeResponse(200))
        client = MODULE.Client.__new__(MODULE.Client)
        client.session = session
        image = {"file_name": "ref.png", "mime_type": "image/png", "data": b"abc"}
        client._upload_blob("https://bucket.example/upload?X-Amz-Algorithm=AWS4", image, None)
        _, _, kwargs = session.calls[0]
        self.assertNotIn("x-ms-blob-type", kwargs["headers"])

    def test_confirm_accepts_completion_events(self):
        response = FakeResponse(200, [
            b"event: file.processing.file_ready\n",
            b'data: {"file_id":"file-abc"}\n',
            b"event: file.processing.completed\n",
            b"data: {}\n",
        ])
        client = MODULE.Client.__new__(MODULE.Client)
        client.session = FakeSession(response)
        client.conv_headers = lambda *args, **kwargs: {}
        ok, detail = client._confirm_upload(
            {"file_id": "file-abc", "file_name": "ref.png", "mime_type": "image/png"}, {})
        self.assertTrue(ok)
        self.assertEqual(detail, "")

    def test_confirm_rejects_error_event(self):
        response = FakeResponse(200, [
            b"event: file.processing.failed\n",
            b'data: {"event":"file.processing.failed"}\n',
        ])
        client = MODULE.Client.__new__(MODULE.Client)
        client.session = FakeSession(response)
        client.conv_headers = lambda *args, **kwargs: {}
        ok, detail = client._confirm_upload(
            {"file_id": "file-abc", "file_name": "ref.png", "mime_type": "image/png"}, {})
        self.assertFalse(ok)
        self.assertEqual(detail, "file.processing.failed")

    def test_archive_conversation(self):
        session = FakeSession(FakeResponse(200))
        client = MODULE.Client.__new__(MODULE.Client)
        client.session = session
        client.conv_headers = lambda *args, **kwargs: {}
        self.assertTrue(client.archive_conversation("conv-1", {}))
        kind, url, kwargs = session.calls[0]
        self.assertEqual(kind, "patch")
        self.assertTrue(url.endswith("/backend-api/conversation/conv-1"))
        self.assertEqual(kwargs["json"], {"is_archived": True})

    def test_archive_failure_is_non_fatal(self):
        client = MODULE.Client.__new__(MODULE.Client)
        client.session = FakeSession(FakeResponse(500))
        client.conv_headers = lambda *args, **kwargs: {}
        self.assertFalse(client.archive_conversation("conv-1", {}))


if __name__ == "__main__":
    unittest.main()
