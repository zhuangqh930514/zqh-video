import os
import random
import threading
from typing import List
from urllib.parse import urlencode

import numpy as np
import requests
from loguru import logger
from moviepy.audio.io.AudioFileClip import AudioFileClip
from moviepy.video.VideoClip import ImageClip
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
    params = {"query": search_term, "per_page": 80, "orientation": video_orientation}
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
        "per_page": 80,
        "order": "relevant",
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
                h = int(video["height"])
                if w >= video_width and h >= video_height:
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


def search_material_items(
    search_terms: List[str],
    source: str = "pexels",
    video_aspect: VideoAspect = VideoAspect.portrait,
    minimum_duration: int = 3,
    image_first: bool = False,
    image_count: int = 4,
    per_term_limit: int = 6,
) -> List[MaterialInfo]:
    """Search a small, ordered material set for one scene."""
    source_map = {
        "pexels": search_videos_pexels,
        "pixabay": search_videos_pixabay,
        "coverr": search_videos_coverr,
        "videvo": search_videos_videvo,
    }
    seen_urls = set()
    term_buckets: List[List[MaterialInfo]] = []

    for search_term in search_terms:
        term_items: List[MaterialInfo] = []

        if image_first and source in ("pexels", "pixabay"):
            if source == "pexels":
                term_items.extend(search_images_pexels(search_term, video_aspect, count=image_count))
            else:
                term_items.extend(search_images_pixabay(search_term, count=image_count))

        if source == "ai_generated":
            term_items.extend(
                generate_videos_pollinations(
                    search_term=search_term,
                    video_aspect=video_aspect,
                    clip_duration=minimum_duration,
                )
            )
        else:
            search_fn = source_map.get(source, search_videos_pexels)
            term_items.extend(
                search_fn(
                    search_term=search_term,
                    minimum_duration=minimum_duration,
                    video_aspect=video_aspect,
                )
            )

        if not term_items and " " in search_term:
            simple_term = " ".join(search_term.split()[:2])
            if simple_term != search_term:
                logger.info(f"scene material fallback query: '{simple_term}'")
                term_items = search_material_items(
                    [simple_term],
                    source=source,
                    video_aspect=video_aspect,
                    minimum_duration=minimum_duration,
                    image_first=image_first,
                    image_count=image_count,
                    per_term_limit=per_term_limit,
                )

        if not term_items and " " in search_term:
            for word in search_term.split():
                if len(word) <= 2:
                    continue
                logger.info(f"scene material broad fallback query: '{word}'")
                term_items = search_material_items(
                    [word],
                    source=source,
                    video_aspect=video_aspect,
                    minimum_duration=minimum_duration,
                    image_first=image_first,
                    image_count=image_count,
                    per_term_limit=per_term_limit,
                )
                if term_items:
                    break

        unique_term_items = []
        for item in term_items:
            if item.url and item.url not in seen_urls:
                unique_term_items.append(item)
                seen_urls.add(item.url)
            if len(unique_term_items) >= per_term_limit:
                break
        if unique_term_items:
            term_buckets.append(unique_term_items)

    # Interleave candidates from each search term so one strong/broad term
    # does not dominate the whole scene and make the video visually repetitive.
    items: List[MaterialInfo] = []
    bucket_index = 0
    while True:
        added = False
        for bucket in term_buckets:
            if bucket_index < len(bucket):
                items.append(bucket[bucket_index])
                added = True
        if not added:
            break
        bucket_index += 1

    return items


def download_material_item(
    item: MaterialInfo,
    task_id: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
    max_clip_duration: int = 5,
    name_prefix: str = "material",
) -> str:
    material_dir = utils.task_dir(task_id)
    os.makedirs(material_dir, exist_ok=True)

    if item.provider.endswith("_image"):
        aspect = VideoAspect(video_aspect)
        video_width, video_height = aspect.to_resolution()
        img_path = os.path.join(
            material_dir, f"{name_prefix}-{utils.md5(item.url)}.jpg"
        )
        if not os.path.exists(img_path):
            response = requests.get(
                item.url,
                proxies=config.proxy,
                verify=_verify_ssl(),
                timeout=(30, 120),
            )
            response.raise_for_status()
            with open(img_path, "wb") as f:
                f.write(response.content)
        return create_ken_burns_clip(
            img_path,
            duration=min(max_clip_duration, 5.0),
            video_width=video_width,
            video_height=video_height,
        )

    return save_video(video_url=item.url, save_dir=material_dir)


def download_videos(
    task_id: str,
    search_terms: List[str],
    source: str = "pexels",
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_contact_mode: VideoConcatMode = VideoConcatMode.sequential,
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
        sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING,
                             progress=41, message=f"AI 正在生成视频素材 ({len(search_terms)} 个主题)…")
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

            # Fallback: if no results, try simplified query (first 1-2 words)
            if not video_items and " " in search_term:
                simple_term = " ".join(search_term.split()[:2])
                if simple_term != search_term:
                    logger.info(f"retrying with simplified term: '{simple_term}'")
                    video_items = search_fn(
                        search_term=simple_term,
                        minimum_duration=max_clip_duration,
                        video_aspect=video_aspect,
                    )
                    logger.info(f"found {len(video_items)} videos for simplified '{simple_term}'")

            # Fallback: if still no results, try single broadest word
            if not video_items and " " in search_term:
                words = search_term.split()
                for word in words:
                    if len(word) > 2:
                        logger.info(f"retrying with single word: '{word}'")
                        video_items = search_fn(
                            search_term=word,
                            minimum_duration=max_clip_duration,
                            video_aspect=video_aspect,
                        )
                        if video_items:
                            logger.info(f"found {len(video_items)} videos for single word '{word}'")
                            break

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
    min_material_count = min(
        12,
        max(6, int(audio_duration / max(max_clip_duration, 1)) + 2),
    )
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
                    message=f"正在下载视频素材 ({idx+1}/{total_items})…",
                )

                if total_duration > audio_duration and len(video_paths) >= min_material_count:
                    logger.info(
                        f"total duration of downloaded videos: {total_duration} seconds, skip downloading more"
                    )
                    break
        except Exception as e:
            logger.error(f"failed to download video: {utils.to_json(item)} => {str(e)}")
    logger.success(f"downloaded {len(video_paths)} videos")
    return video_paths


# ── Image search + Ken Burns effect ──

def search_images_pexels(search_term: str, video_aspect: VideoAspect = VideoAspect.portrait, count: int = 10) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)
    orientation = "portrait" if aspect.value == VideoAspect.portrait.value else "landscape"
    api_key = get_api_key("pexels_api_keys")
    headers = {
        "Authorization": api_key,
        "User-Agent": "Mozilla/5.0",
    }
    params = {"query": search_term, "per_page": max(count, 20), "orientation": orientation}
    query_url = f"https://api.pexels.com/v1/search?{urlencode(params)}"
    logger.info(f"searching images pexels: {query_url}")
    try:
        r = requests.get(query_url, headers=headers, proxies=config.proxy,
                         verify=_verify_ssl(), timeout=(30, 60))
        r.raise_for_status()
        response = r.json()
        items = []
        for photo in response.get("photos", []):
            src = photo["src"].get("large") or photo["src"].get("original") or photo["src"].get("large2x", "")
            if src:
                item = MaterialInfo()
                item.provider = "pexels_image"
                item.url = src
                item.duration = 5
                items.append(item)
                if len(items) >= count:
                    break
        return items
    except Exception as e:
        logger.error(f"pexels image search failed: {e}")
    return []


def search_images_pixabay(search_term: str, count: int = 10) -> List[MaterialInfo]:
    api_key = get_api_key("pixabay_api_keys")
    params = {"q": search_term, "image_type": "photo", "per_page": max(count, 30), "key": api_key,
              "order": "relevant"}
    query_url = f"https://pixabay.com/api/?{urlencode(params)}"
    logger.info(f"searching images pixabay: {query_url}")
    try:
        r = requests.get(query_url, proxies=config.proxy, verify=_verify_ssl(), timeout=(30, 60))
        r.raise_for_status()
        response = r.json()
        items = []
        for hit in response.get("hits", []):
            url = hit.get("largeImageURL") or hit.get("webformatURL", "")
            if url:
                item = MaterialInfo()
                item.provider = "pixabay_image"
                item.url = url
                item.duration = 5
                items.append(item)
                if len(items) >= count:
                    break
        return items
    except Exception as e:
        logger.error(f"pixabay image search failed: {e}")
    return []


def create_ken_burns_clip(
    image_path: str,
    duration: float = 5.0,
    video_width: int = 1080,
    video_height: int = 1920,
    fps: int = 24,
) -> str:
    """
    Convert a static image into a video clip with Ken Burns effect (slow zoom/pan).
    Returns the path to the generated video file.
    """
    output_path = f"{image_path}.kb.mp4"
    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        return output_path

    try:
        from PIL import Image
        img = Image.open(image_path)
        img_w, img_h = img.size

        target_ratio = video_width / video_height
        img_ratio = img_w / img_h

        if img_ratio > target_ratio:
            new_h = video_height
            new_w = int(img_w * (video_height / img_h))
        else:
            new_w = video_width
            new_h = int(img_h * (video_width / img_w))

        style = random.choice(["zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down"])
        zoom_factor = random.uniform(1.08, 1.20)

        clip = ImageClip(image_path).with_fps(fps).with_duration(duration)
        clip = clip.resized(new_size=(new_w, new_h))

        def frame_func(t):
            progress = t / duration if duration > 0 else 0

            if style == "zoom_in":
                s = 1.0 + (zoom_factor - 1.0) * progress
            elif style == "zoom_out":
                s = zoom_factor - (zoom_factor - 1.0) * progress
            else:
                s = 1.0 + (zoom_factor - 1.0) * 0.5

            sw = int(new_w * s)
            sh = int(new_h * s)

            if style == "pan_left":
                ox = int((sw - video_width) * (1.0 - progress))
                oy = (sh - video_height) // 2
            elif style == "pan_right":
                ox = int((sw - video_width) * progress)
                oy = (sh - video_height) // 2
            elif style == "pan_up":
                ox = (sw - video_width) // 2
                oy = int((sh - video_height) * (1.0 - progress))
            elif style == "pan_down":
                ox = (sw - video_width) // 2
                oy = int((sh - video_height) * progress)
            else:
                ox = (sw - video_width) // 2
                oy = (sh - video_height) // 2

            ox = max(0, ox)
            oy = max(0, oy)

            return clip.resized(new_size=(sw, sh)).get_frame(t)[oy:oy + video_height, ox:ox + video_width]

        result = clip.with_updated_frame_function(frame_func)
        result = result.with_fps(fps).with_duration(duration)

        result.write_videofile(
            output_path, fps=fps, codec="libx264",
            audio=False, logger=None, threads=2,
        )
        result.close()
        clip.close()
        return output_path
    except Exception as e:
        logger.warning(f"Ken Burns failed for {os.path.basename(image_path)}: {e}")
        return ""


def download_materials(
    task_id: str,
    search_terms: List[str],
    source: str = "pexels",
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_contact_mode: VideoConcatMode = VideoConcatMode.sequential,
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
    image_first: bool = False,
) -> List[str]:
    """
    Download video/image materials. If image_first is True, search images first
    (with Ken Burns effect) and use videos only as supplement.
    """
    aspect = VideoAspect(video_aspect)
    video_width, video_height = aspect.to_resolution()

    if source in ("pexels", "pixabay") and image_first:
        logger.info(f"\n\n## searching images (primary) + videos (supplement) from {source}")
        sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING,
                             progress=41, message="正在搜索匹配图片…")

        # ── Phase 1: Search images ──
        image_items = []
        for search_term in search_terms:
            if source == "pexels":
                imgs = search_images_pexels(search_term, video_aspect, count=8)
            else:
                imgs = search_images_pixabay(search_term, count=8)
            for item in imgs:
                if item.url not in {i.url for i in image_items}:
                    image_items.append(item)
            # Fallback for image search too
            if not imgs and " " in search_term:
                simple = " ".join(search_term.split()[:2])
                if simple != search_term:
                    imgs = search_images_pexels(simple, video_aspect, count=8) if source == "pexels" else search_images_pixabay(simple, count=8)
                    for item in imgs:
                        if item.url not in {i.url for i in image_items}:
                            image_items.append(item)

        logger.info(f"found {len(image_items)} images for {len(search_terms)} terms")

        # ── Phase 2: Search videos as supplement (fewer per term) ──
        sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING,
                             progress=43, message="正在搜索补充视频素材…")
        video_items = []
        search_fn = search_videos_pexels if source == "pexels" else search_videos_pixabay
        for search_term in search_terms:
            vids = search_fn(search_term=search_term, minimum_duration=3,
                             video_aspect=video_aspect)
            for item in vids:
                if item.url not in {i.url for i in video_items}:
                    video_items.append(item)
            logger.info(f"found {len(vids)} videos for '{search_term}'")

        # ── Phase 3: Interleave: image, image, video, image, image, video... ──
        valid_items = []
        seen_urls = set()
        img_idx, vid_idx = 0, 0
        while img_idx < len(image_items) or vid_idx < len(video_items):
            for _ in range(3):
                if img_idx < len(image_items):
                    item = image_items[img_idx]
                    if item.url not in seen_urls:
                        valid_items.append(item)
                        seen_urls.add(item.url)
                    img_idx += 1
            if vid_idx < len(video_items):
                item = video_items[vid_idx]
                if item.url not in seen_urls:
                    valid_items.append(item)
                    seen_urls.add(item.url)
                vid_idx += 1

        logger.info(f"total materials: {len(valid_items)} ({len(image_items)} images + {len(video_items)} videos)")

        # ── Phase 4: Download images and convert to video clips ──
        sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING,
                             progress=45, message="正在下载图片并生成动态效果…")
        material_dir = utils.task_dir(task_id)
        os.makedirs(material_dir, exist_ok=True)

        final_paths = []
        total_items = max(len(valid_items), 1)
        total_duration = 0.0
        min_material_count = min(
            14,
            max(8, int(audio_duration / max(max_clip_duration, 1)) + 3),
        )

        for idx, item in enumerate(valid_items):
            try:
                if item.provider.endswith("_image"):
                    # Download image and convert to video via Ken Burns
                    img_path = os.path.join(material_dir, f"img_{idx}.jpg")
                    if not os.path.exists(img_path):
                        r = requests.get(item.url, proxies=config.proxy,
                                         verify=_verify_ssl(), timeout=(30, 120))
                        r.raise_for_status()
                        with open(img_path, "wb") as f:
                            f.write(r.content)
                    kb_path = create_ken_burns_clip(
                        img_path, duration=min(max_clip_duration, 5.0),
                        video_width=video_width, video_height=video_height,
                    )
                    if kb_path and os.path.exists(kb_path):
                        final_paths.append(kb_path)
                        total_duration += max_clip_duration
                else:
                    # Download video normally
                    saved = save_video(video_url=item.url, save_dir=material_dir)
                    if saved:
                        final_paths.append(saved)
                        total_duration += min(max_clip_duration, item.duration)

                dl_progress = 45 + int((idx + 1) / total_items * 5)
                sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING,
                                     progress=min(dl_progress, 50),
                                     message=f"正在准备素材 ({idx+1}/{total_items})…")

                if total_duration > audio_duration and len(final_paths) >= min_material_count:
                    logger.info(f"total duration {total_duration}s >= audio {audio_duration}s, stop")
                    break
            except Exception as e:
                logger.error(f"failed to process material: {item.url} => {e}")

        logger.success(f"prepared {len(final_paths)} material clips")
        return final_paths

    # ── Original video-only path ──
    return download_videos(
        task_id, search_terms, source, video_aspect,
        video_contact_mode, audio_duration, max_clip_duration,
    )


if __name__ == "__main__":
    download_videos(
        "test123", ["Money Exchange Medium"], audio_duration=100, source="pixabay"
    )
