import math
import os.path
import re
from os import path
from typing import Any

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoConcatMode, VideoParams
from app.services import llm, material, subtitle, video, voice, upload_post, douyin
from app.services import state as sm
from app.utils import utils


def generate_script(task_id, params):
    logger.info("\n\n## generating video script")
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, message="AI 正在生成视频文案…")
    video_script = params.video_script.strip()
    if not video_script:
        video_script = llm.generate_script(
            video_subject=params.video_subject,
            language=params.video_language,
            paragraph_number=params.paragraph_number,
            script_style=getattr(params, "script_style", "douyin"),
            target_duration=getattr(params, "target_duration", 60),
        )
    else:
        logger.debug(f"video script: \n{video_script}")

    if not video_script:
        error = "AI 生成文案失败，请检查 LLM 配置和 API Key"
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED, error=error, message="文案生成失败")
        logger.error(error)
        return None

    return video_script


def generate_terms(task_id, params, video_script):
    logger.info("\n\n## generating video terms")
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, message="AI 正在生成视频关键词…")
    video_terms = params.video_terms
    if not video_terms:
        video_terms = llm.generate_terms(
            video_subject=params.video_subject, video_script=video_script, amount=12
        )
    else:
        if isinstance(video_terms, str):
            video_terms = [
                term.strip() for term in re.split(r"[,，]", video_terms) if term.strip()
            ]
        elif isinstance(video_terms, list):
            video_terms = [
                term.strip() for term in video_terms if isinstance(term, str) and term.strip()
            ]
        else:
            raise ValueError("video_terms must be a string or a list of strings.")

        logger.debug(f"video terms: {utils.to_json(video_terms)}")

    if not video_terms:
        error = "AI 生成关键词失败，请检查 LLM 配置和 API Key"
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED, error=error, message="关键词生成失败")
        logger.error(error)
        return None

    if not isinstance(video_terms, list) or not all(
        isinstance(term, str) for term in video_terms
    ):
        error = f"invalid video terms response: {video_terms}"
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED, error=error)
        logger.error(error)
        return None

    return video_terms


def split_script_scenes(video_script: str) -> list[str]:
    scenes = [p.strip() for p in re.split(r"\n\s*\n", video_script or "") if p.strip()]
    if scenes:
        return scenes
    return [video_script.strip()] if video_script and video_script.strip() else []


def allocate_scene_timeline(scenes: list[str], audio_duration: float) -> list[dict[str, Any]]:
    total_weight = sum(max(len(scene), 1) for scene in scenes) or 1
    cursor = 0.0
    scene_plan = []
    for index, scene_text in enumerate(scenes, start=1):
        if index == len(scenes):
            duration = max(audio_duration - cursor, 1.0)
        else:
            duration = max(audio_duration * (max(len(scene_text), 1) / total_weight), 1.0)
        scene_plan.append(
            {
                "index": index,
                "script": scene_text,
                "start": round(cursor, 2),
                "end": round(cursor + duration, 2),
                "duration": round(duration, 2),
                "terms": [],
                "materials": [],
            }
        )
        cursor += duration
    return scene_plan


def build_scene_plan(task_id, params, video_script, video_terms, audio_duration):
    scenes = split_script_scenes(video_script)
    scene_plan = allocate_scene_timeline(scenes, audio_duration)
    if not scene_plan:
        return []

    if getattr(params, "video_source", "") == "local":
        return scene_plan

    if not getattr(params, "enable_scene_matching", True):
        for scene in scene_plan:
            scene["terms"] = video_terms[:2] if isinstance(video_terms, list) else []
        return scene_plan

    sm.state.update_task(
        task_id,
        state=const.TASK_STATE_PROCESSING,
        progress=35,
        message="AI 正在生成分镜关键词…",
    )
    fallback_terms = video_terms if isinstance(video_terms, list) else []
    for scene in scene_plan:
        scene_terms = llm.generate_terms(
            video_subject=params.video_subject,
            video_script=scene["script"],
            amount=5,
        )
        if not scene_terms:
            scene_terms = fallback_terms[:3]
        scene["terms"] = scene_terms
    return scene_plan


def save_script_data(task_id, video_script, video_terms, params, scene_plan=None):
    script_file = path.join(utils.task_dir(task_id), "script.json")
    script_data = {
        "script": video_script,
        "search_terms": video_terms,
        "scenes": scene_plan or [],
        "params": params,
    }

    with open(script_file, "w", encoding="utf-8") as f:
        f.write(utils.to_json(script_data))


def generate_audio(task_id, params, video_script):
    '''
    Generate audio for the video script.
    If a custom audio file is provided, it will be used directly.
    There will be no subtitle maker object returned in this case.
    Otherwise, TTS will be used to generate the audio.
    Returns:
        - audio_file: path to the generated or provided audio file
        - audio_duration: duration of the audio in seconds
        - sub_maker: subtitle maker object if TTS is used, None otherwise
    '''
    logger.info("\n\n## generating audio")
    # /audio 和 /subtitle 请求模型不包含 custom_audio_file，
    # 这里统一做兼容读取，避免直调接口时抛属性错误。
    custom_audio_file = getattr(params, "custom_audio_file", None)
    if not custom_audio_file or not os.path.exists(custom_audio_file):
        if custom_audio_file:
            logger.warning(
                f"custom audio file not found: {custom_audio_file}, using TTS to generate audio."
            )
        else:
            logger.info("no custom audio file provided, using TTS to generate audio.")
        audio_file = path.join(utils.task_dir(task_id), "audio.mp3")
        voice_name = getattr(params, "voice_name", "") or ""
        if not voice_name:
            error = "语音合成失败：未选择配音声音"
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED, error=error, message="语音合成失败")
            logger.error(error)
            return None, None, None
        parsed_voice_name = voice.parse_voice_name(voice_name)
        sub_maker = voice.tts(
            text=video_script,
            voice_name=parsed_voice_name,
            voice_rate=params.voice_rate,
            voice_file=audio_file,
        )
        if sub_maker is None:
            error = (
                f"语音合成失败：配音 '{voice_name}' 不可用，请检查网络或更换配音"
            )
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED, error=error, message="语音合成失败")
            logger.error(error)
            logger.error(
                """Troubleshooting:
1. check if the language of the voice matches the language of the video script.
2. check if the network is available. If you are in China, it is recommended to use a VPN and enable the global traffic mode.
            """.strip()
            )
            return None, None, None
        audio_duration = math.ceil(voice.get_audio_duration(sub_maker))
        if audio_duration == 0:
            error = f"failed to get audio duration for voice '{voice_name}'."
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED, error=error)
            logger.error(error)
            return None, None, None
        return audio_file, audio_duration, sub_maker
    else:
        logger.info(f"using custom audio file: {custom_audio_file}")
        audio_duration = voice.get_audio_duration(custom_audio_file)
        if audio_duration == 0:
            error = f"failed to get audio duration from custom audio file: {custom_audio_file}"
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED, error=error)
            logger.error(error)
            return None, None, None
        return custom_audio_file, audio_duration, None

def generate_subtitle(task_id, params, video_script, sub_maker, audio_file):
    '''
    Generate subtitle for the video script.
    If subtitle generation is disabled or no subtitle maker is provided, it will return an empty string.
    Otherwise, it will generate the subtitle using the specified provider.
    Returns:
        - subtitle_path: path to the generated subtitle file
    '''
    logger.info("\n\n## generating subtitle")
    if not params.subtitle_enabled or sub_maker is None:
        return ""

    subtitle_path = path.join(utils.task_dir(task_id), "subtitle.srt")
    subtitle_provider = config.app.get("subtitle_provider", "edge").strip().lower()
    logger.info(f"\n\n## generating subtitle, provider: {subtitle_provider}")

    subtitle_fallback = False
    if subtitle_provider == "edge":
        voice.create_subtitle(
            text=video_script, sub_maker=sub_maker, subtitle_file=subtitle_path
        )
        if not os.path.exists(subtitle_path):
            subtitle_fallback = True
            logger.warning("subtitle file not found, fallback to whisper")

    if subtitle_provider == "whisper" or subtitle_fallback:
        subtitle.create(audio_file=audio_file, subtitle_file=subtitle_path)
        logger.info("\n\n## correcting subtitle")
        subtitle.correct(subtitle_file=subtitle_path, video_script=video_script)

    subtitle_lines = subtitle.file_to_subtitles(subtitle_path)
    if not subtitle_lines:
        logger.warning(f"subtitle file is invalid: {subtitle_path}")
        return ""

    return subtitle_path


def _actual_material_source(video_source: str) -> tuple[str, bool]:
    if video_source == "pexels_img":
        return "pexels", True
    if video_source == "pixabay_img":
        return "pixabay", True
    return video_source, False


def flatten_material_paths(materials):
    if isinstance(materials, list) and materials and isinstance(materials[0], dict):
        paths = []
        for scene in materials:
            paths.extend(scene.get("materials") or [])
        return paths
    return materials or []


def get_scene_materials(task_id, params, scene_plan, audio_duration):
    if params.video_source == "local":
        materials = video.preprocess_video(
            materials=params.video_materials, clip_duration=params.video_clip_duration
        )
        if not materials:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error("no valid local materials found.")
            return None
        local_paths = [material_info.url for material_info in materials]
        for index, scene in enumerate(scene_plan):
            scene["materials"] = [local_paths[index % len(local_paths)]]
        return scene_plan

    source, image_first = _actual_material_source(params.video_source)
    total = max(len(scene_plan), 1)
    for index, scene in enumerate(scene_plan, start=1):
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_PROCESSING,
            progress=40 + int(index / total * 10),
            message=f"正在为分镜 {index}/{total} 搜索素材…",
        )
        items = material.search_material_items(
            search_terms=scene.get("terms") or [],
            source=source,
            video_aspect=params.video_aspect,
            minimum_duration=2,
            image_first=image_first,
            image_count=6,
            per_term_limit=5,
        )
        material_paths = []
        collected_duration = 0.0
        target_duration = scene.get("duration", params.video_clip_duration)
        min_material_count = min(
            6,
            max(3, math.ceil(target_duration / max(params.video_clip_duration, 1)) + 1),
        )
        for item_idx, item in enumerate(items):
            try:
                clip_path = material.download_material_item(
                    item=item,
                    task_id=task_id,
                    video_aspect=params.video_aspect,
                    max_clip_duration=params.video_clip_duration,
                    name_prefix=f"scene-{index}-{item_idx}",
                )
                if clip_path:
                    material_paths.append(clip_path)
                    collected_duration += min(params.video_clip_duration, item.duration or params.video_clip_duration)
                if collected_duration >= target_duration and len(material_paths) >= min_material_count:
                    break
            except Exception as exc:
                logger.error(f"failed to download scene material: {exc}")

        if not material_paths:
            logger.warning(f"scene {index} has no matched materials, falling back to global terms")
            fallback_paths = material.download_materials(
                task_id=task_id,
                search_terms=scene.get("terms") or [],
                source=source,
                video_aspect=params.video_aspect,
                video_contact_mode=params.video_concat_mode,
                audio_duration=scene.get("duration", params.video_clip_duration),
                max_clip_duration=params.video_clip_duration,
                image_first=image_first,
            )
            material_paths = fallback_paths or []

        scene["materials"] = material_paths

    if not any(scene.get("materials") for scene in scene_plan):
        return None
    return scene_plan


def get_video_materials(task_id, params, video_terms, audio_duration, scene_plan=None):
    if scene_plan:
        return get_scene_materials(task_id, params, scene_plan, audio_duration)

    if params.video_source == "local":
        logger.info("\n\n## preprocess local materials")
        materials = video.preprocess_video(
            materials=params.video_materials, clip_duration=params.video_clip_duration
        )
        if not materials:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(
                "no valid materials found, please check the materials and try again."
            )
            return None
        return [material_info.url for material_info in materials]
    else:
        logger.info(f"\n\n## downloading videos from {params.video_source}")
        # Image-first mode: use images with Ken Burns as primary, videos as supplement
        if params.video_source in ("pexels_img", "pixabay_img"):
            actual_source = "pexels" if params.video_source == "pexels_img" else "pixabay"
            downloaded_videos = material.download_materials(
                task_id=task_id,
                search_terms=video_terms,
                source=actual_source,
                video_aspect=params.video_aspect,
                video_contact_mode=params.video_concat_mode,
                audio_duration=audio_duration * params.video_count,
                max_clip_duration=params.video_clip_duration,
                image_first=True,
            )
        else:
            downloaded_videos = material.download_videos(
                task_id=task_id,
                search_terms=video_terms,
                source=params.video_source,
                video_aspect=params.video_aspect,
                video_contact_mode=params.video_concat_mode,
                audio_duration=audio_duration * params.video_count,
                max_clip_duration=params.video_clip_duration,
            )
        if not downloaded_videos:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(
                "failed to download videos, maybe the network is not available. if you are in China, please use a VPN."
            )
            return None
        return downloaded_videos


def generate_final_videos(
    task_id, params, downloaded_videos, audio_file, subtitle_path
):
    final_video_paths = []
    combined_video_paths = []
    video_concat_mode = (
        params.video_concat_mode if params.video_count == 1 else VideoConcatMode.random
    )
    video_transition_mode = params.video_transition_mode

    _progress = 50
    for i in range(params.video_count):
        index = i + 1
        combined_video_path = path.join(
            utils.task_dir(task_id), f"combined-{index}.mp4"
        )
        logger.info(f"\n\n## combining video: {index} => {combined_video_path}")
        if downloaded_videos and isinstance(downloaded_videos[0], dict):
            video.combine_scene_videos(
                combined_video_path=combined_video_path,
                scenes=downloaded_videos,
                audio_file=audio_file,
                video_aspect=params.video_aspect,
                video_transition_mode=video_transition_mode,
                max_clip_duration=params.video_clip_duration,
                threads=params.n_threads,
            )
        else:
            video.combine_videos(
                combined_video_path=combined_video_path,
                video_paths=downloaded_videos,
                audio_file=audio_file,
                video_aspect=params.video_aspect,
                video_concat_mode=video_concat_mode,
                video_transition_mode=video_transition_mode,
                max_clip_duration=params.video_clip_duration,
                threads=params.n_threads,
            )

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress, message="正在拼接视频片段…")

        final_video_path = path.join(utils.task_dir(task_id), f"final-{index}.mp4")

        logger.info(f"\n\n## generating video: {index} => {final_video_path}")
        video.generate_video(
            video_path=combined_video_path,
            audio_path=audio_file,
            subtitle_path=subtitle_path,
            output_file=final_video_path,
            params=params,
        )

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress, message="正在合成音视频与字幕…")

        final_video_paths.append(final_video_path)
        combined_video_paths.append(combined_video_path)

    return final_video_paths, combined_video_paths


def _start_impl(task_id, params: VideoParams, stop_at: str = "video"):
    logger.info(f"start task: {task_id}, stop_at: {stop_at}")
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=5, message="正在生成视频文案…")

    # 1. Generate script
    video_script = generate_script(task_id, params)
    if not video_script or (
        isinstance(video_script, str) and video_script.startswith("Error: ")
    ):
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_FAILED,
            error="AI 生成文案失败，请检查 LLM 配置和 API Key" if not video_script else video_script,
            message="文案生成失败",
        )
        return

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=10, message="正在生成视频搜索关键词…")

    if stop_at == "script":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, script=video_script
        )
        return {"script": video_script}

    # 2. Generate terms
    video_terms = ""
    if params.video_source != "local":
        video_terms = generate_terms(task_id, params, video_script)
        if not video_terms:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED, message="关键词生成失败")
            return

    save_script_data(task_id, video_script, video_terms, params)

    if stop_at == "terms":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, terms=video_terms
        )
        return {"script": video_script, "terms": video_terms}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=20, message="正在合成语音…")

    # 3. Generate audio
    audio_file, audio_duration, sub_maker = generate_audio(
        task_id, params, video_script
    )
    if not audio_file:
        task_state = sm.state.get_task(task_id) or {}
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_FAILED,
            error=task_state.get("error") or "TTS 语音合成失败，请检查语音配置和网络连接",
            message="语音合成失败",
        )
        return

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=30, message="正在生成字幕…")

    if stop_at == "audio":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            audio_file=audio_file,
        )
        return {"audio_file": audio_file, "audio_duration": audio_duration}

    # 4. Generate subtitle
    subtitle_path = generate_subtitle(
        task_id, params, video_script, sub_maker, audio_file
    )

    if stop_at == "subtitle":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            subtitle_path=subtitle_path,
        )
        return {"subtitle_path": subtitle_path}

    scene_plan = []
    if getattr(params, "enable_scene_matching", True):
        scene_plan = build_scene_plan(
            task_id, params, video_script, video_terms, audio_duration
        )
        save_script_data(task_id, video_script, video_terms, params, scene_plan)

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=40, message="正在搜索并下载视频素材…")

    # 5. Get video materials
    downloaded_videos = get_video_materials(
        task_id, params, video_terms, audio_duration, scene_plan=scene_plan
    )
    if not downloaded_videos:
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_FAILED,
            error="素材下载失败：未能获取到足够的视频素材，请检查网络连接或 API Key 是否有效",
            message="素材下载失败",
        )
        return

    if stop_at == "materials":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            materials=downloaded_videos,
            scenes=downloaded_videos if isinstance(downloaded_videos, list) else [],
            script=video_script,
            terms=video_terms,
            audio_file=audio_file,
            audio_duration=audio_duration,
            subtitle_path=subtitle_path,
        )
        return {"materials": downloaded_videos, "scenes": downloaded_videos}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=50, message="正在合成最终视频…")

    # 仅完整视频生成流程才需要处理视频拼接模式；
    # 这样可以避免 /subtitle 和 /audio 这类请求访问不存在的字段。
    if type(params.video_concat_mode) is str:
        params.video_concat_mode = VideoConcatMode(params.video_concat_mode)

    # 6. Generate final videos
    final_video_paths, combined_video_paths = generate_final_videos(
        task_id, params, downloaded_videos, audio_file, subtitle_path
    )

    if not final_video_paths:
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_FAILED,
            error="视频合成失败：请检查 ffmpeg 是否正确安装",
            message="视频合成失败",
        )
        return

    logger.success(
        f"task {task_id} finished, generated {len(final_video_paths)} videos."
    )

    # 7. Cleanup intermediate material files (save disk space)
    if params.video_source not in ("local",):
        for file_path in flatten_material_paths(downloaded_videos):
            if os.path.exists(file_path) and file_path not in final_video_paths:
                try:
                    os.remove(file_path)
                    logger.debug(f"cleaned up material: {os.path.basename(file_path)}")
                except Exception:
                    pass
        # Also clean up downloaded images (.jpg) that were used for Ken Burns
        task_dir = utils.task_dir(task_id)
        for f in os.listdir(task_dir):
            if f.startswith("img_") and (f.endswith(".jpg") or f.endswith(".png")):
                fp = os.path.join(task_dir, f)
                try:
                    os.remove(fp)
                    logger.debug(f"cleaned up source image: {f}")
                except Exception:
                    pass
        for f in os.listdir(task_dir):
            if f.endswith(".kb.mp4"):
                fp = os.path.join(task_dir, f)
                if fp not in final_video_paths:
                    try:
                        os.remove(fp)
                    except Exception:
                        pass
    for file_path in combined_video_paths:
        if os.path.exists(file_path) and file_path not in final_video_paths:
            try:
                os.remove(file_path)
                logger.debug(f"cleaned up combined: {os.path.basename(file_path)}")
            except Exception:
                pass

    # 8. Cross-post to TikTok/Instagram (if enabled)
    cross_post_results = []
    if upload_post.upload_post_service.is_configured() and upload_post.upload_post_service.auto_upload:
        logger.info("\n\n## cross-posting videos to TikTok/Instagram")
        for video_path in final_video_paths:
            result = upload_post.cross_post_video(
                video_path=video_path,
                title=params.video_subject or "Check out this video! #shorts #viral"
            )
            cross_post_results.append(result)
            if result.get('success'):
                logger.info(f"✅ Cross-posted: {video_path}")
            else:
                logger.warning(f"⚠️ Failed to cross-post: {video_path} - {result.get('error', 'Unknown error')}")

    # 9. Publish to Douyin (if enabled)
    douyin_publish_results = []
    if douyin.douyin_service.is_configured() and douyin.douyin_service.is_authorized() and douyin.douyin_service.auto_publish:
        logger.info("\n\n## publishing videos to Douyin")
        for video_path in final_video_paths:
            result = douyin.publish_to_douyin(
                video_path=video_path,
                title=params.video_subject or "每天一个小知识",
            )
            douyin_publish_results.append(result)
            if result.get("success"):
                logger.info(f"✅ Published to Douyin: {video_path}")
            else:
                logger.warning(f"⚠️ Failed to publish to Douyin: {video_path} - {result.get('error', 'Unknown error')}")

    kwargs = {
        "videos": final_video_paths,
        "combined_videos": combined_video_paths,
        "script": video_script,
        "terms": video_terms,
        "audio_file": audio_file,
        "audio_duration": audio_duration,
        "subtitle_path": subtitle_path,
        "materials": flatten_material_paths(downloaded_videos),
        "scenes": downloaded_videos if isinstance(downloaded_videos, list) and downloaded_videos and isinstance(downloaded_videos[0], dict) else [],
        "cross_post_results": cross_post_results if cross_post_results else None,
        "douyin_publish_results": douyin_publish_results if douyin_publish_results else None,
    }
    sm.state.update_task(
        task_id, state=const.TASK_STATE_COMPLETE, progress=100, message="视频制作完成！", **kwargs
    )
    return kwargs


def start(task_id, params: VideoParams, stop_at: str = "video"):
    try:
        return _start_impl(task_id, params, stop_at)
    except Exception as exc:
        logger.exception(f"task {task_id} failed unexpectedly: {exc}")
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_FAILED,
            error=f"程序异常: {str(exc)}",
            message="程序异常退出",
        )
        return None


if __name__ == "__main__":
    task_id = "task_id"
    params = VideoParams(
        video_subject="金钱的作用",
        voice_name="zh-CN-XiaoyiNeural-Female",
        voice_rate=1.0,
    )
    start(task_id, params, stop_at="video")
