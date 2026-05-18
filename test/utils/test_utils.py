import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.utils import utils


class TestUtils(unittest.TestCase):
    def test_task_dir_is_safe_for_concurrent_creation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch.object(utils, "root_dir", return_value=tmp_dir):
                errors = []

                def create_task_dir():
                    try:
                        utils.task_dir("same-task")
                    except Exception as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=create_task_dir) for _ in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

                self.assertEqual(errors, [])
                self.assertTrue(Path(tmp_dir, "storage", "tasks", "same-task").is_dir())


if __name__ == "__main__":
    unittest.main()
