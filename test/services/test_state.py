import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models import const
from app.services import state as state_service


class TestStateService(unittest.TestCase):
    def test_memory_state_returns_task_copy(self):
        state = state_service.MemoryState()
        state.update_task("task-1", videos=["/tmp/a.mp4"])

        task = state.get_task("task-1")
        task["videos"].append("http://example.com/a.mp4")

        self.assertEqual(state.get_task("task-1")["videos"], ["/tmp/a.mp4"])

    def test_file_state_get_all_tasks_loads_disk_state(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tasks_dir = Path(tmp_dir) / "tasks"
            task_dir = tasks_dir / "task-1"
            task_dir.mkdir(parents=True)
            (task_dir / "state.json").write_text(
                json.dumps(
                    {
                        "task_id": "task-1",
                        "state": const.TASK_STATE_COMPLETE,
                        "progress": 100,
                    }
                ),
                encoding="utf-8",
            )

            def _storage_dir(sub_dir="", create=False):
                path = Path(tmp_dir)
                if sub_dir:
                    path = path / sub_dir
                if create:
                    path.mkdir(parents=True, exist_ok=True)
                return str(path)

            with patch.object(state_service.utils, "storage_dir", _storage_dir):
                state = state_service.FileState()
                tasks, total = state.get_all_tasks(page=1, page_size=10)

            self.assertEqual(total, 1)
            self.assertEqual(tasks[0]["task_id"], "task-1")


if __name__ == "__main__":
    unittest.main()
