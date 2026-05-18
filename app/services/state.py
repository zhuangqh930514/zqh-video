import ast
import copy
import json
import os
import threading
import time
from abc import ABC, abstractmethod

from loguru import logger

from app.config import config
from app.models import const
from app.utils import utils


# Base class for state management
class BaseState(ABC):
    @abstractmethod
    def update_task(self, task_id: str, state: int, progress: int = 0, **kwargs):
        pass

    @abstractmethod
    def get_task(self, task_id: str):
        pass

    @abstractmethod
    def get_all_tasks(self, page: int, page_size: int):
        pass


# Memory state management
class MemoryState(BaseState):
    def __init__(self):
        self._tasks = {}
        self._lock = threading.RLock()

    def get_all_tasks(self, page: int, page_size: int):
        with self._lock:
            start = (page - 1) * page_size
            end = start + page_size
            tasks = [copy.deepcopy(task) for task in self._tasks.values()]
            total = len(tasks)
            return tasks[start:end], total

    def update_task(
        self,
        task_id: str,
        state: int = const.TASK_STATE_PROCESSING,
        progress: int = 0,
        **kwargs,
    ):
        progress = int(progress)
        if progress > 100:
            progress = 100

        with self._lock:
            self._tasks[task_id] = {
                "task_id": task_id,
                "created_at": self._tasks.get(task_id, {}).get("created_at", time.time()),
                "state": state,
                "progress": progress,
                **kwargs,
            }

    def get_task(self, task_id: str):
        with self._lock:
            task = self._tasks.get(task_id, None)
            return copy.deepcopy(task) if task is not None else None

    def delete_task(self, task_id: str):
        with self._lock:
            if task_id in self._tasks:
                del self._tasks[task_id]


# Redis state management
class RedisState(BaseState):
    def __init__(self, host="localhost", port=6379, db=0, password=None):
        import redis

        self._redis = redis.StrictRedis(host=host, port=port, db=db, password=password)

    def get_all_tasks(self, page: int, page_size: int):
        start = (page - 1) * page_size
        end = start + page_size
        tasks = []
        cursor = 0
        total = 0
        while True:
            cursor, keys = self._redis.scan(cursor, count=page_size)
            total += len(keys)
            if total > start:
                for key in keys[max(0, start - total):end - total]:
                    task_data = self._redis.hgetall(key)
                    task = {
                        k.decode("utf-8"): self._convert_to_original_type(v) for k, v in task_data.items()
                    }
                    tasks.append(task)
                    if len(tasks) >= page_size:
                        break
            if cursor == 0 or len(tasks) >= page_size:
                break
        return tasks, total

    def update_task(
        self,
        task_id: str,
        state: int = const.TASK_STATE_PROCESSING,
        progress: int = 0,
        **kwargs,
    ):
        progress = int(progress)
        if progress > 100:
            progress = 100

        fields = {
            "task_id": task_id,
            "state": state,
            "progress": progress,
            **kwargs,
        }

        for field, value in fields.items():
            self._redis.hset(task_id, field, str(value))

    def get_task(self, task_id: str):
        task_data = self._redis.hgetall(task_id)
        if not task_data:
            return None

        task = {
            key.decode("utf-8"): self._convert_to_original_type(value)
            for key, value in task_data.items()
        }
        return task

    def delete_task(self, task_id: str):
        self._redis.delete(task_id)

    @staticmethod
    def _convert_to_original_type(value):
        """
        Convert the value from byte string to its original data type.
        You can extend this method to handle other data types as needed.
        """
        value_str = value.decode("utf-8")

        try:
            # try to convert byte string array to list
            return ast.literal_eval(value_str)
        except (ValueError, SyntaxError):
            pass

        if value_str.isdigit():
            return int(value_str)
        # Add more conversions here if needed
        return value_str


# File-based state — persists across page refreshes
class FileState(BaseState):
    def __init__(self):
        self._cache = {}
        self._lock = threading.RLock()

    def _state_file(self, task_id: str) -> str:
        return os.path.join(utils.storage_dir("tasks"), task_id, "state.json")

    def _load_task_from_disk(self, task_id: str):
        state_file = self._state_file(task_id)
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._cache[task_id] = data
        return data

    def _refresh_cache_from_disk(self):
        tasks_dir = utils.storage_dir("tasks")
        if not os.path.isdir(tasks_dir):
            return

        for task_id in os.listdir(tasks_dir):
            task_dir = os.path.join(tasks_dir, task_id)
            state_file = os.path.join(task_dir, "state.json")
            if not os.path.isdir(task_dir) or not os.path.isfile(state_file):
                continue
            if task_id in self._cache:
                continue
            try:
                self._load_task_from_disk(task_id)
            except Exception as e:
                logger.warning(f"failed to load task state: {state_file}, error: {e}")

    def update_task(
        self,
        task_id: str,
        state: int = const.TASK_STATE_PROCESSING,
        progress: int = 0,
        **kwargs,
    ):
        progress = int(progress)
        if progress > 100:
            progress = 100

        with self._lock:
            data = copy.deepcopy(self._cache.get(task_id, {"task_id": task_id}))
            data.setdefault("created_at", time.time())
            data["state"] = state
            data["progress"] = progress
            data.update(kwargs)
            self._cache[task_id] = data

            state_file = self._state_file(task_id)
            temp_state_file = f"{state_file}.tmp"
            try:
                os.makedirs(os.path.dirname(state_file), exist_ok=True)
                with open(temp_state_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, default=str)
                os.replace(temp_state_file, state_file)
            except Exception as e:
                logger.warning(f"failed to persist task state: {state_file}, error: {e}")
                try:
                    if os.path.exists(temp_state_file):
                        os.remove(temp_state_file)
                except Exception:
                    pass

    def get_task(self, task_id: str):
        with self._lock:
            cached = self._cache.get(task_id)
            if cached is not None:
                return copy.deepcopy(cached)
            try:
                return copy.deepcopy(self._load_task_from_disk(task_id))
            except Exception:
                return None

    def get_all_tasks(self, page: int, page_size: int):
        with self._lock:
            self._refresh_cache_from_disk()
            tasks = [copy.deepcopy(task) for task in self._cache.values()]
            tasks.sort(
                key=lambda task: task.get("created_at", task.get("task_id", "")),
                reverse=True,
            )
            start = (page - 1) * page_size
            end = start + page_size
            total = len(tasks)
            return tasks[start:end], total

    def delete_task(self, task_id: str):
        with self._lock:
            self._cache.pop(task_id, None)
            state_file = self._state_file(task_id)
            try:
                if os.path.exists(state_file):
                    os.remove(state_file)
            except Exception as e:
                logger.warning(f"failed to delete task state: {state_file}, error: {e}")


# Global state
_enable_redis = config.app.get("enable_redis", False)
_redis_host = config.app.get("redis_host", "localhost")
_redis_port = config.app.get("redis_port", 6379)
_redis_db = config.app.get("redis_db", 0)
_redis_password = config.app.get("redis_password", None)

if _enable_redis:
    state = RedisState(
        host=_redis_host, port=_redis_port, db=_redis_db, password=_redis_password
    )
else:
    state = FileState()
