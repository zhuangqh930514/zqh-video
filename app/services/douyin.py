"""
Douyin Open Platform integration for publishing videos via official API.

Docs: https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/video-management/douyin/create-video/video-create
"""
import os
import time
from typing import Any, Optional
from urllib.parse import urlencode

import requests
from loguru import logger

from app.config import config


class DouyinService:
    API_BASE = "https://open.douyin.com"
    DEFAULT_SCOPE = "video.create.bind"
    TOKEN_REFRESH_BUFFER_SECONDS = 86400

    def __init__(self):
        self._reload_config()

    def _reload_config(self) -> None:
        self.client_key = config.app.get("douyin_client_key", "")
        self.client_secret = config.app.get("douyin_client_secret", "")
        self.redirect_uri = config.app.get("douyin_redirect_uri", "")
        self.enabled = config.app.get("douyin_enabled", False)
        self.auto_publish = config.app.get("douyin_auto_publish", False)
        self.scope = config.app.get("douyin_scope", self.DEFAULT_SCOPE)

    def is_configured(self) -> bool:
        self._reload_config()
        return bool(
            self.enabled
            and self.client_key
            and self.client_secret
            and self.redirect_uri
        )

    def is_authorized(self) -> bool:
        return bool(
            config.app.get("douyin_open_id")
            and config.app.get("douyin_access_token")
        )

    def get_auth_url(self, state: str = "") -> str:
        params = {
            "client_key": self.client_key,
            "response_type": "code",
            "scope": self.scope,
            "redirect_uri": self.redirect_uri,
        }
        if state:
            params["state"] = state
        return f"{self.API_BASE}/platform/oauth/connect/?{urlencode(params)}"

    def get_status(self) -> dict:
        expires_at = self._token_expires_at()
        return {
            "configured": self.is_configured(),
            "authorized": self.is_authorized(),
            "open_id": config.app.get("douyin_open_id", ""),
            "expires_at": expires_at,
            "auth_url": self.get_auth_url() if self.is_configured() and not self.is_authorized() else "",
        }

    def exchange_code(self, code: str) -> dict:
        if not self.is_configured():
            return {"success": False, "error": "Douyin is not configured"}

        try:
            response = requests.post(
                f"{self.API_BASE}/oauth/access_token/",
                data={
                    "client_key": self.client_key,
                    "client_secret": self.client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") or {}
            error = self._extract_error(data, payload)
            if error:
                return {"success": False, "error": error, "raw": payload}

            self._save_tokens(data)
            logger.info("Douyin OAuth completed successfully")
            return {"success": True, "open_id": data.get("open_id", ""), "raw": payload}
        except requests.exceptions.RequestException as exc:
            logger.error(f"Douyin OAuth exchange failed: {exc}")
            return {"success": False, "error": str(exc)}

    def refresh_access_token(self) -> dict:
        refresh_token = config.app.get("douyin_refresh_token", "")
        if not refresh_token:
            return {"success": False, "error": "Missing refresh_token, please authorize again"}

        try:
            response = requests.post(
                f"{self.API_BASE}/oauth/refresh_token/",
                data={
                    "client_key": self.client_key,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") or {}
            error = self._extract_error(data, payload)
            if error:
                return {"success": False, "error": error, "raw": payload}

            self._save_tokens(data)
            logger.info("Douyin access_token refreshed")
            return {"success": True, "raw": payload}
        except requests.exceptions.RequestException as exc:
            logger.error(f"Douyin token refresh failed: {exc}")
            return {"success": False, "error": str(exc)}

    def ensure_valid_token(self) -> str:
        access_token = config.app.get("douyin_access_token", "")
        if not access_token:
            raise ValueError("Douyin is not authorized")

        expires_at = self._token_expires_at()
        if expires_at and expires_at - time.time() <= self.TOKEN_REFRESH_BUFFER_SECONDS:
            result = self.refresh_access_token()
            if not result.get("success"):
                raise ValueError(result.get("error", "Failed to refresh Douyin token"))
            access_token = config.app.get("douyin_access_token", "")
        return access_token

    def upload_video(self, video_path: str, access_token: Optional[str] = None, open_id: Optional[str] = None) -> dict:
        access_token = access_token or self.ensure_valid_token()
        open_id = open_id or config.app.get("douyin_open_id", "")
        if not open_id:
            return {"success": False, "error": "Missing open_id, please authorize again"}

        if not os.path.exists(video_path):
            return {"success": False, "error": f"Video file not found: {video_path}"}

        logger.info(f"Uploading video to Douyin: {video_path}")
        try:
            with open(video_path, "rb") as video_file:
                response = requests.post(
                    f"{self.API_BASE}/api/douyin/v1/video/upload_video/",
                    params={"open_id": open_id},
                    headers={"access-token": access_token},
                    files={"video": video_file},
                    timeout=600,
                )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") or {}
            error = self._extract_error(data, payload)
            if error:
                return {"success": False, "error": error, "raw": payload}

            video_id = data.get("video_id") or (data.get("video") or {}).get("video_id")
            if not video_id:
                return {"success": False, "error": "Douyin upload succeeded but video_id is missing", "raw": payload}

            return {"success": True, "video_id": video_id, "raw": payload}
        except requests.exceptions.RequestException as exc:
            logger.error(f"Douyin upload failed: {exc}")
            return {"success": False, "error": str(exc)}

    def create_video(
        self,
        video_id: str,
        text: str,
        access_token: Optional[str] = None,
        open_id: Optional[str] = None,
        private_status: int = 0,
    ) -> dict:
        access_token = access_token or self.ensure_valid_token()
        open_id = open_id or config.app.get("douyin_open_id", "")

        logger.info("Creating Douyin video from uploaded file...")
        try:
            response = requests.post(
                f"{self.API_BASE}/api/douyin/v1/video/create_video/",
                params={"open_id": open_id},
                headers={
                    "access-token": access_token,
                    "Content-Type": "application/json",
                },
                json={
                    "video_id": video_id,
                    "text": text[:2200],
                    "private_status": private_status,
                },
                timeout=60,
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") or {}
            error = self._extract_error(data, payload)
            if error:
                return {"success": False, "error": error, "raw": payload}

            item_id = data.get("item_id") or data.get("video_id")
            return {
                "success": True,
                "item_id": item_id,
                "video_id": video_id,
                "raw": payload,
            }
        except requests.exceptions.RequestException as exc:
            logger.error(f"Douyin create_video failed: {exc}")
            return {"success": False, "error": str(exc)}

    def publish_video(
        self,
        video_path: str,
        title: str,
        private_status: Optional[int] = None,
    ) -> dict:
        if not self.is_configured():
            return {"success": False, "error": "Douyin is not configured"}
        if not self.is_authorized():
            return {"success": False, "error": "Douyin is not authorized", "auth_url": self.get_auth_url()}

        upload_result = self.upload_video(video_path)
        if not upload_result.get("success"):
            return upload_result

        if private_status is None:
            private_status = int(config.app.get("douyin_private_status", 0))

        create_result = self.create_video(
            video_id=upload_result["video_id"],
            text=title,
            private_status=private_status,
        )
        if create_result.get("success"):
            logger.info(f"Douyin publish succeeded: item_id={create_result.get('item_id')}")
        return create_result

    def clear_authorization(self) -> None:
        for key in (
            "douyin_access_token",
            "douyin_refresh_token",
            "douyin_open_id",
            "douyin_expires_in",
            "douyin_token_updated_at",
        ):
            config.app.pop(key, None)
        config.save_config()

    def _save_tokens(self, data: dict[str, Any]) -> None:
        if data.get("access_token"):
            config.app["douyin_access_token"] = data["access_token"]
        if data.get("refresh_token"):
            config.app["douyin_refresh_token"] = data["refresh_token"]
        if data.get("open_id"):
            config.app["douyin_open_id"] = data["open_id"]
        if data.get("expires_in") is not None:
            config.app["douyin_expires_in"] = int(data["expires_in"])
        config.app["douyin_token_updated_at"] = int(time.time())
        config.save_config()

    def _token_expires_at(self) -> int:
        updated_at = int(config.app.get("douyin_token_updated_at", 0) or 0)
        expires_in = int(config.app.get("douyin_expires_in", 0) or 0)
        if not updated_at or not expires_in:
            return 0
        return updated_at + expires_in

    @staticmethod
    def _extract_error(data: dict[str, Any], payload: dict[str, Any]) -> str:
        error_code = data.get("error_code")
        if error_code not in (None, 0, "0"):
            description = data.get("description") or data.get("error_msg") or payload.get("message")
            return description or f"Douyin API error_code={error_code}"
        if payload.get("message") and str(payload.get("message")).lower() not in {"success", "ok"}:
            return str(payload.get("message"))
        return ""


douyin_service = DouyinService()


def publish_to_douyin(video_path: str, title: str) -> dict:
    return douyin_service.publish_video(video_path, title)
