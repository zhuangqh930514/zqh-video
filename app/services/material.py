import os
import random
import threading
from typing import List
from urllib.parse import urlencode

import requests
from loguru import logger
from moviepy.video.io.VideoFileClip import VideoFileClip

from app.models import const

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode
from app.services import state as sm
from app.utils import utils

# Thread-safe counter for API key rotation
_api_key_counter = 0
_api_key_lock = threading.Lock()


def get_api_key(cfg_key: str):
    api_keys = config.app.get(cfg_key)
    if not api_keys:
        raise ValueError(
            f"\n\n##### {cfg_key} is not set #####\n\nPlease set it in the config.toml file: {config.config_file}\n\n"
            f"{utils.to_json(config.app)}"
        )

    # if only one key is provided, return it
    if isinstance(api_keys, str):
        return api_keys

    global _api_key_counter
    with _api_key_lock:
        key = api_keys[_api_key_counter % len(api_keys)]
        _api_key_counter += 1
        return key


def _verify_ssl() -> bool:
    return bool(config.app.get("request_verify_ssl", True))


def _is_valid_video_file(video_path: str) -> bool:
    clip = None
    try:
        clip = VideoFileClip(video_path)
        return bool(clip.duration > 0 and clip.fps > 0)
    except Exception as e:
        logger.warning(f"invalid video file: {video_path} => {str(e)}")
        return False
    finally:
        if clip is not None:
            try:
                clip.close()
            except Exception:
                pass


def _remove_file_safely(file_path: str):
    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        logger.debug(f"failed to remove file {file_path}: {str(e)}")


def search_videos_pexels(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)
    video_orientation = aspect.name
    video_width, video_height = aspect.to_resolution()
    api_key = get_api_key("pexels_api_keys")
    headers = {
        "Authorization": api_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    }
    # Build URL
    params = {"query": search_term, "per_page": 20, "orientation": video_orientation}
    query_url = f"https://api.pexels.com/videos/search?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url,
            headers=headers,
            proxies=config.proxy,
            verify=_verify_ssl(),
            timeout=(30, 60),
        )
        r.raise_for_status()
        response = r.json()
        video_items = []
        if "videos" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items
        videos = response["videos"]
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["video_files"]
            # loop through each url to determine the best quality
            for video in video_files:
                w = int(video["width"])
                h = int(video["height"])
                if w == video_width and h == video_height:
                    item = MaterialInfo()
                    item.provider = "pexels"
                    item.url = video["link"]
                    item.duration = duration
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def search_videos_pixabay(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)

    video_width, video_height = aspect.to_resolution()

    api_key = get_api_key("pixabay_api_keys")
    # Build URL
    params = {
        "q": search_term,
        "video_type": "all",  # Accepted values: "all", "film", "animation"
        "per_page": 50,
        "key": api_key,
    }
    query_url = f"https://pixabay.com/api/videos/?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url, proxies=config.proxy, verify=_verify_ssl(), timeout=(30, 60)
        )
        r.raise_for_status()
        response = r.json()
        video_items = []
        if "hits" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items
        videos = response["hits"]
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["videos"]
            # loop through each url to determine the best quality
            for video_type in video_files:
                video = video_files[video_type]
                w = int(video["width"])
                # h = int(video["height"])
                if w >= video_width:
                    item = MaterialInfo()
                    item.provider = "pixabay"
                    item.url = video["url"]
                    item.duration = duration
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def search_videos_coverr(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    """
    Search stock videos via Coverr API (free tier: 50 requests/hour).
    Requires coverr_api_key in config.
    API docs: https://coverr.co/developers
    """
    api_key = config.app.get("coverr_api_key", "")
    if not api_key:
        logger.warning("coverr_api_key not configured, skipping Coverr search.")
        return []

    aspect = VideoAspect(video_aspect)
    video_width, video_height = aspect.to_resolution()
    is_vertical = video_aspect.value == VideoAspect.portrait.value

    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    params = {
        "query": search_term,
        "page_size": 20,
    }
    query_url = "https://api.coverr.co/videos"

    try:
        r = requests.get(
            query_url, headers=headers, params=params,
            proxies=config.proxy, verify=_verify_ssl(), timeout=(30, 60),
        )
        if r.status_code != 200:
            logger.error(f"Coverr API error: {r.status_code} {r.text[:200]}")
            return []

        response = r.json()
        hits = response.get("hits", [])
        video_items = []
        for hit in hits:
            try:
                # try to match orientation
                hit_is_vertical = hit.get("is_vertical", False)
                if is_vertical != hit_is_vertical:
                    continue

                # Coverr provides multiple video files (mp4, webm) at different resolutions.
                # We grab the mp4 with the best matching width.
                video_files = hit.get("video_files", [])
                best_url = None
                best_width = 0
                for vf in video_files:
                    if vf.get("type") != "video/mp4":
                        continue
                    w = int(vf.get("width", 0))
                    url = vf.get("url", "")
                    if not url:
                        continue
                    # prefer the width closest to our target (but at least 720)
                    if w >= 720 and (best_url is None or abs(w - video_width) < abs(best_width - video_width)):
                        best_url = url
                        best_width = w
                if not best_url:
                    continue

                duration = int(hit.get("duration", 0))
                if duration < minimum_duration:
                    continue

                item = MaterialInfo()
                item.provider = "coverr"
                item.url = best_url
                item.duration = duration
                video_items.append(item)
            except Exception as e:
                logger.debug(f"Coverr parse hit error: {e}")
                continue

        logger.info(f"Coverr: found {len(video_items)} videos for '{search_term}'")
        return video_items

    except Exception as e:
        logger.error(f"Coverr search failed: {str(e)}")
        return []


def search_videos_videvo(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    """
    Search free stock videos via Videvo API.
    Requires videvo_api_key in config.
    Free tier: https://www.videvo.net/api/
    """
    api_key = config.app.get("videvo_api_key", "")
    if not api_key:
        logger.warning("videvo_api_key not configured, skipping Videvo search.")
        return []

    aspect = VideoAspect(video_aspect)
    video_width, video_height = aspect.to_resolution()

    query_url = f"https://api.videvo.net/api/v2/videos/"
    params = {
        "api_key": api_key,
        "q": search_term,
        "page_size": 20,
        "sort": "relevant",
    }

    try:
        r = requests.get(
            query_url, params=params,
            proxies=config.proxy, verify=_verify_ssl(), timeout=(30, 60),
        )
        if r.status_code != 200:
            logger.error(f"Videvo API error: {r.status_code} {r.text[:200]}")
            return []

        response = r.json()
        total = response.get("total_count", 0)
        if total == 0:
            return []

        video_items = []
        # Videvo returns videos in the "videos" list where each item has "files"
        for video in response.get("videos", []):
            try:
                # Only take free clips
                if not video.get("free", False):
                    continue

                duration = float(video.get("duration", 0))
                if duration < minimum_duration:
                    continue

                # Find the best mp4 file matching our dimensions
                files = video.get("files", [])
                best_url = None
                best_width = 0
                for f in files:
                    if f.get("mime_type") != "video/mp4":
                        continue
                    w = int(f.get("width", 0))
                    url = f.get("url", "")
                    if not url:
                        continue
                    if w >= 640 and (best_url is None or abs(w - video_width) < abs(best_width - video_width)):
                        best_url = url
                        best_width = w

                if not best_url:
                    continue

                item = MaterialInfo()
                item.provider = "videvo"
                item.url = best_url
                item.duration = int(duration)
                video_items.append(item)
            except Exception:
                continue

        logger.info(f"Videvo: found {len(video_items)} free videos for '{search_term}'")
        return video_items

    except Exception as e:
        logger.error(f"Videvo search failed: {str(e)}")
        return []


def generate_videos_pollinations(
    search_term: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
    clip_duration: int = 5,
) -> List[MaterialInfo]:
    """
    Generate short AI video clips from text prompts using Pollinations.AI.
    Free, no API key required.
    API: GET https://video.pollinations.ai/prompt/{prompt}
    """
    aspect = VideoAspect(video_aspect)
    video_width, video_height = aspect.to_resolution()
    aspect_ratio = video_aspect.value  # "16:9" or "9:16"

    items = []
    # Generate 2 clips per search term for variety
    for variant in ["", " cinematic", " close up"]:
        prompt = f"{search_term}{variant}"
        try:
            url = f"https://video.pollinations.ai/prompt/{requests.utils.quote(prompt)}"
            params = {
                "model": "ltx-2",
                "duration": clip_duration,
                "aspectRatio": aspect_ratio,
                "width": video_width,
                "height": video_height,
                "nologo": "true",
            }
            query_url = f"{url}?{urlencode(params)}"
            logger.info(f"Pollinations generating video: {prompt[:50]}...")

            # Pollinations returns MP4 directly — just save the URL as a material
            # The actual download happens later in save_video()
            item = MaterialInfo()
            item.provider = "pollinations"
            item.url = query_url
            item.duration = clip_duration
            items.append(item)
        except Exception as e:
            logger.error(f"Pollinations generate failed for '{search_term}': {str(e)}")
            continue

    return items


def save_video(video_url: str, save_dir: str = "") -> str:
    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")

    os.makedirs(save_dir, exist_ok=True)

    url_without_query = video_url.split("?")[0]
    url_hash = utils.md5(url_without_query)
    video_id = f"vid-{url_hash}"
    video_path = f"{save_dir}/{video_id}.mp4"

    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        if _is_valid_video_file(video_path):
            logger.info(f"video already exists: {video_path}")
            return video_path
        _remove_file_safely(video_path)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }

    temp_video_path = f"{video_path}.download"
    _remove_file_safely(temp_video_path)
    try:
        with requests.get(
            video_url,
            headers=headers,
            proxies=config.proxy,
            verify=_verify_ssl(),
            timeout=(30, 240),
            stream=True,
        ) as response:
            response.raise_for_status()
            with open(temp_video_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        os.replace(temp_video_path, video_path)
    except Exception as e:
        _remove_file_safely(temp_video_path)
        _remove_file_safely(video_path)
        logger.error(f"failed to download video: {video_url} => {str(e)}")
        return ""

    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        if _is_valid_video_file(video_path):
            return video_path
        _remove_file_safely(video_path)

    return ""


def download_videos(
    task_id: str,
    search_terms: List[str],
    source: str = "pexels",
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_contact_mode: VideoConcatMode = VideoConcatMode.random,
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
) -> List[str]:
    video_contact_mode = VideoConcatMode(video_contact_mode)
    valid_video_items = []
    valid_video_urls = []
    found_duration = 0.0

    # Route to the correct search/generate function based on source
    source_map = {
        "pexels": search_videos_pexels,
        "pixabay": search_videos_pixabay,
        "coverr": search_videos_coverr,
        "videvo": search_videos_videvo,
    }

    if source == "ai_generated":
        # AI generation: generate clips one at a time, no search phase needed
        logger.info(f"\n\n## generating AI videos from {len(search_terms)} prompts via Pollinations")
        for search_term in search_terms:
            items = generate_videos_pollinations(
                search_term=search_term,
                video_aspect=video_aspect,
                clip_duration=max_clip_duration,
            )
            for item in items:
                if item.url not in valid_video_urls:
                    valid_video_items.append(item)
                    valid_video_urls.append(item.url)
                    found_duration += item.duration
    else:
        search_fn = source_map.get(source, search_videos_pexels)
        for search_term in search_terms:
            video_items = search_fn(
                search_term=search_term,
                minimum_duration=max_clip_duration,
                video_aspect=video_aspect,
            )
            logger.info(f"found {len(video_items)} videos for '{search_term}'")

            for item in video_items:
                if item.url not in valid_video_urls:
                    valid_video_items.append(item)
                    valid_video_urls.append(item.url)
                    found_duration += item.duration

        logger.info(
            f"found total videos: {len(valid_video_items)}, required duration: {audio_duration} seconds, found duration: {found_duration} seconds"
        )

    video_paths = []
    material_directory = config.app.get("material_directory", "").strip()
    if material_directory == "task":
        material_directory = utils.task_dir(task_id)
    elif material_directory and not os.path.isdir(material_directory):
        material_directory = ""

    if source != "ai_generated" and video_contact_mode.value == VideoConcatMode.random.value:
        random.shuffle(valid_video_items)

    total_duration = 0.0
    total_items = max(len(valid_video_items), 1)
    for idx, item in enumerate(valid_video_items):
        try:
            logger.info(f"downloading video: {item.url}")
            saved_video_path = save_video(
                video_url=item.url, save_dir=material_directory
            )
            if saved_video_path:
                logger.info(f"video saved: {saved_video_path}")
                video_paths.append(saved_video_path)
                seconds = min(max_clip_duration, item.duration)
                total_duration += seconds

                # 每下载完一个视频更新一次进度 (42~48)
                dl_progress = 42 + int((idx + 1) / total_items * 6)
                sm.state.update_task(
                    task_id,
                    state=const.TASK_STATE_PROCESSING,
                    progress=min(dl_progress, 48),
                )

                if total_duration > audio_duration:
                    logger.info(
                        f"total duration of downloaded videos: {total_duration} seconds, skip downloading more"
                    )
                    break
        except Exception as e:
            logger.error(f"failed to download video: {utils.to_json(item)} => {str(e)}")
    logger.success(f"downloaded {len(video_paths)} videos")
    return video_paths


if __name__ == "__main__":
    download_videos(
        "test123", ["Money Exchange Medium"], audio_duration=100, source="pixabay"
    )
