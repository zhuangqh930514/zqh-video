import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import material


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        yield b"video-"
        yield b"bytes"


class TestMaterialService(unittest.TestCase):
    def test_api_key_rotation_starts_with_first_key(self):
        with patch.object(material.config, "app", {"pexels_api_keys": ["k1", "k2"]}):
            material._api_key_counter = 0

            self.assertEqual(material.get_api_key("pexels_api_keys"), "k1")
            self.assertEqual(material.get_api_key("pexels_api_keys"), "k2")

    def test_save_video_streams_to_temp_file_then_promotes(self):
        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            material.requests, "get", return_value=_FakeResponse()
        ) as get_mock, patch.object(material, "_is_valid_video_file", return_value=True):
            video_path = material.save_video(
                "https://example.com/video.mp4?token=abc",
                save_dir=tmp_dir,
            )

            self.assertTrue(video_path)
            self.assertEqual(Path(video_path).read_bytes(), b"video-bytes")
            self.assertFalse(os.path.exists(f"{video_path}.download"))
            self.assertTrue(get_mock.call_args.kwargs["stream"])


if __name__ == "__main__":
    unittest.main()
