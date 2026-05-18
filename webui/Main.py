import json
import os
import platform
import subprocess
import sys
import threading
import time
from uuid import uuid4

import streamlit as st
from loguru import logger

# Add the root directory of the project to the system path to allow importing modules from the project
root_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if root_dir not in sys.path:
    sys.path.append(root_dir)
    print("******** sys.path ********")
    print(sys.path)
    print("")

from app.config import config
from app.models import const
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services import llm, voice
from app.services import state as sm
from app.services import task as tm
from app.utils import utils

# 用于在 async 模式下跨渲染周期追踪上一次日志文件长度，避免重复刷新
_prev_log_len = 0

st.set_page_config(
    page_title="zqh video",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="auto",
    menu_items={
        "Report a bug": "https://github.com/harry0703/MoneyPrinterTurbo/issues",
        "About": "# MoneyPrinterTurbo\nSimply provide a topic or keyword for a video, and it will "
        "automatically generate the video copy, video materials, video subtitles, "
        "and video background music before synthesizing a high-definition short "
        "video.\n\nhttps://github.com/harry0703/MoneyPrinterTurbo",
    },
)


streamlit_style = """
<style>
h1 {
    padding-top: 0 !important;
}
/* 限制视频播放器高度，避免页面过长需要滚动 */
div[data-testid="stVideo"] video {
    max-height: 380px;
    width: 100%;
}
/* 视频封面图占位也跟随限制 */
div[data-testid="stVideo"] {
    max-height: 380px;
    overflow: hidden;
}
</style>
"""
st.markdown(streamlit_style, unsafe_allow_html=True)

# 定义资源目录
font_dir = os.path.join(root_dir, "resource", "fonts")
song_dir = os.path.join(root_dir, "resource", "songs")
i18n_dir = os.path.join(root_dir, "webui", "i18n")
config_file = os.path.join(root_dir, "webui", ".streamlit", "webui.toml")
system_locale = utils.get_system_locale()


if "video_subject" not in st.session_state:
    st.session_state["video_subject"] = ""
if "video_script" not in st.session_state:
    st.session_state["video_script"] = ""
if "video_terms" not in st.session_state:
    st.session_state["video_terms"] = ""
if "ui_language" not in st.session_state:
    st.session_state["ui_language"] = config.ui.get("language", system_locale)
if "local_video_materials" not in st.session_state:
    # 记住用户最近一次已经落盘的本地素材，避免仅修改文案后二次生成时丢失素材列表。
    st.session_state["local_video_materials"] = []
if "latest_task_id" not in st.session_state:
    st.session_state["latest_task_id"] = None
if "suggested_topics" not in st.session_state:
    st.session_state["suggested_topics"] = None
if "batch_task_ids" not in st.session_state:
    st.session_state["batch_task_ids"] = []
if "pending_batch_count" not in st.session_state:
    st.session_state["pending_batch_count"] = 0
if "pending_batch_category" not in st.session_state:
    st.session_state["pending_batch_category"] = ""

# 加载语言文件
locales = utils.load_locales(i18n_dir)

# 创建一个顶部栏，包含标题和语言选择
title_col, lang_col = st.columns([3, 1])

with title_col:
    st.title(f"zqh video v{config.project_version}")

with lang_col:
    display_languages = []
    selected_index = 0
    for i, code in enumerate(locales.keys()):
        display_languages.append(f"{code} - {locales[code].get('Language')}")
        if code == st.session_state.get("ui_language", ""):
            selected_index = i

    selected_language = st.selectbox(
        "Language / 语言",
        options=display_languages,
        index=selected_index,
        key="top_language_selector",
        label_visibility="collapsed",
    )
    if selected_language:
        code = selected_language.split(" - ")[0].strip()
        st.session_state["ui_language"] = code
        config.ui["language"] = code

support_locales = [
    "zh-CN",
    "zh-HK",
    "zh-TW",
    "de-DE",
    "en-US",
    "fr-FR",
    "vi-VN",
    "th-TH",
    "tr-TR",
]


def get_all_fonts():
    fonts = []
    for root, dirs, files in os.walk(font_dir):
        for file in files:
            if file.endswith(".ttf") or file.endswith(".ttc"):
                fonts.append(file)
    fonts.sort()
    return fonts


def get_all_songs():
    songs = []
    for root, dirs, files in os.walk(song_dir):
        for file in files:
            if file.endswith(".mp3"):
                songs.append(file)
    return songs


def open_task_folder(task_id):
    try:
        sys = platform.system()
        path = os.path.join(root_dir, "storage", "tasks", task_id)
        if os.path.exists(path):
            if sys == "Windows":
                os.startfile(path)
            if sys == "Darwin":
                subprocess.run(["open", path], check=False)
    except Exception as e:
        logger.error(e)


def _cleanup_log_handler():
    hid = st.session_state.get("task_log_handler_id")
    if hid:
        try:
            logger.remove(hid)
        except Exception:
            pass
        st.session_state["task_log_handler_id"] = None


def scroll_to_bottom():
    js = """
    <script>
        console.log("scroll_to_bottom");
        function scroll(dummy_var_to_force_repeat_execution){
            var sections = parent.document.querySelectorAll('section.main');
            console.log(sections);
            for(let index = 0; index<sections.length; index++) {
                sections[index].scrollTop = sections[index].scrollHeight;
            }
        }
        scroll(1);
    </script>
    """
    st.components.v1.html(js, height=0, width=0)


def init_log():
    logger.remove()
    _lvl = "DEBUG"

    def format_record(record):
        # 获取日志记录中的文件全路径
        file_path = record["file"].path
        # 将绝对路径转换为相对于项目根目录的路径
        relative_path = os.path.relpath(file_path, root_dir)
        # 更新记录中的文件路径
        record["file"].path = f"./{relative_path}"
        # 返回修改后的格式字符串
        # 您可以根据需要调整这里的格式
        record["message"] = record["message"].replace(root_dir, ".")

        _format = (
            "<green>{time:%Y-%m-%d %H:%M:%S}</> | "
            + "<level>{level}</> | "
            + '"{file.path}:{line}":<blue> {function}</> '
            + "- <level>{message}</>"
            + "\n"
        )
        return _format

    logger.add(
        sys.stdout,
        level=_lvl,
        format=format_record,
        colorize=True,
    )


init_log()

locales = utils.load_locales(i18n_dir)


def tr(key):
    loc = locales.get(st.session_state["ui_language"], {})
    return loc.get("Translation", {}).get(key, key)


# ========== Task History Helper ==========
TASK_STATE_ICONS = {-1: "❌", 1: "✅", 4: "⏳"}


def get_task_list():
    """
    扫描 storage/tasks/ 目录，读取 state.json 获取状态，
    返回按时间倒序排列的任务列表。
    """
    tasks_dir = utils.storage_dir("tasks")
    task_list = []
    if os.path.exists(tasks_dir):
        for task_id in os.listdir(tasks_dir):
            task_dir = os.path.join(tasks_dir, task_id)
            if not os.path.isdir(task_dir):
                continue

            # 读取持久化的 state
            state_info = {}
            state_file = os.path.join(task_dir, "state.json")
            if os.path.exists(state_file):
                try:
                    with open(state_file, "r", encoding="utf-8") as f:
                        state_info = json.loads(f.read())
                except Exception:
                    pass

            task_state = state_info.get("state")
            task_progress = state_info.get("progress", 0)

            # 已完成的必须有视频文件才算有效任务
            video_files = sorted([
                os.path.join(task_dir, f)
                for f in os.listdir(task_dir)
                if f.startswith("final-") and f.endswith(".mp4")
            ])
            if task_state != const.TASK_STATE_PROCESSING and not video_files:
                continue

            # 读取文案摘要
            script_subject = ""
            script_file = os.path.join(task_dir, "script.json")
            if os.path.exists(script_file):
                try:
                    with open(script_file, "r", encoding="utf-8") as f:
                        script_data = json.loads(f.read())
                        script_content = script_data.get("script", "")
                        script_subject = script_content[:80]
                except Exception:
                    pass

            task_list.append({
                "task_id": task_id,
                "subject": script_subject,
                "videos": video_files,
                "state": task_state,
                "progress": task_progress,
                "error": state_info.get("error", ""),
                "created_time": os.path.getctime(task_dir),
            })

    task_list.sort(key=lambda x: x["created_time"], reverse=True)
    return task_list


def get_historical_subjects():
    """
    从 storage/tasks/ 目录中提取所有已完成任务的历史视频主题，
    用于在生成新主题时排除重复内容。
    Returns:
        list of video_subject strings
    """
    tasks_dir = utils.storage_dir("tasks")
    subjects = []
    if os.path.exists(tasks_dir):
        for task_id in os.listdir(tasks_dir):
            task_dir = os.path.join(tasks_dir, task_id)
            if not os.path.isdir(task_dir):
                continue
            script_file = os.path.join(task_dir, "script.json")
            if os.path.exists(script_file):
                try:
                    with open(script_file, "r", encoding="utf-8") as f:
                        script_data = json.loads(f.read())
                        params = script_data.get("params", {})
                        subject = params.get("video_subject", "")
                        if subject:
                            subjects.append(subject)
                except Exception:
                    pass
    return subjects


def launch_batch(batch_count, topic_category, params):
    """Start a batch after all UI controls have populated params."""
    config.save_config()
    if (
        not params.video_source
        or params.video_source
        not in ["pexels", "pixabay", "coverr", "videvo", "ai_generated", "local"]
    ):
        st.error(tr("Please Select a Valid Video Source"))
        return
    if params.video_source == "pexels" and not config.app.get("pexels_api_keys", ""):
        st.error(tr("Please Enter the Pexels API Key"))
        return
    if params.video_source == "pixabay" and not config.app.get("pixabay_api_keys", ""):
        st.error(tr("Please Enter the Pixabay API Key"))
        return
    if params.video_source == "coverr" and not config.app.get("coverr_api_key", ""):
        st.error(tr("Please Enter the Coverr API Key"))
        return
    if params.video_source == "videvo" and not config.app.get("videvo_api_key", ""):
        st.error(tr("Please Enter the Videvo API Key"))
        return
    if not params.custom_audio_file and not params.voice_name:
        st.error(tr("No voices available for the selected TTS server. Please select another server."))
        return

    with st.spinner(tr("Generating batch topics...") + f" ({batch_count})"):
        historical = get_historical_subjects()
        topics = llm.generate_topics(
            category=topic_category, count=batch_count, exclude_subjects=historical
        )
    if not topics or ("Error: " in topics[0] if topics else True):
        st.error(topics[0] if topics else tr("Batch generation failed"))
        return

    batch_task_ids = []
    batch_params_list = []
    for topic in topics:
        task_id = str(uuid4())
        batch_task_ids.append(task_id)
        batch_params = VideoParams(
            **{
                **params.model_dump(),
                "video_subject": topic,
                "video_script": "",
                "video_terms": None,
            }
        )
        batch_params_list.append(batch_params)

    st.session_state["batch_task_ids"] = batch_task_ids
    st.session_state["batch_total"] = len(batch_task_ids)
    st.session_state["task_running"] = True
    st.session_state["task_start_time"] = time.time()
    st.session_state["current_task_id"] = batch_task_ids[0]

    task_thread = utils.run_in_background(
        run_batch, task_ids=batch_task_ids, params_list=batch_params_list
    )
    st.session_state["task_thread_id"] = task_thread.ident
    st.session_state["current_task_log"] = os.path.join(
        utils.task_dir(batch_task_ids[0]), "task.log"
    )
    st.toast(f"🚀 {tr('Batch started')}: {batch_count} {tr('videos')}")
    scroll_to_bottom()
    st.rerun()


def run_batch(task_ids, params_list):
    """Process a batch of video tasks sequentially in background."""
    total = len(task_ids)
    for i, (task_id, params) in enumerate(zip(task_ids, params_list)):
        task_log_dir = utils.task_dir(task_id)
        task_log_file = os.path.join(task_log_dir, "task.log")
        handler_id = logger.add(task_log_file, level="DEBUG", rotation="10 MB")
        logger.info(f"Batch [{i+1}/{total}] {params.video_subject}")
        try:
            tm.start(task_id=task_id, params=params)
            task_state = sm.state.get_task(task_id) or {}
            if task_state.get("state") == const.TASK_STATE_FAILED:
                logger.error(
                    f"Batch task {task_id} failed: "
                    f"{task_state.get('error') or 'unknown error'}"
                )
        except Exception as e:
            logger.error(f"Batch task {task_id} failed: {e}")
        try:
            logger.remove(handler_id)
        except Exception:
            pass


def task_status_text(state, progress):
    """返回任务状态的中文描述"""
    if state == const.TASK_STATE_PROCESSING:
        return f"{progress}%"
    if state == const.TASK_STATE_COMPLETE:
        return tr("Completed")
    if state == const.TASK_STATE_FAILED:
        return tr("Failed")
    return ""


# ========== Sidebar — Task History ==========
with st.sidebar:
    st.header("🎬 " + tr("Task History"))
    if st.button("↻ " + tr("Refresh Task List")):
        st.rerun()

    tasks = get_task_list()

    # 自动选中最新任务（刚生成的或用户指定的）
    latest_id = st.session_state.get("latest_task_id")
    if latest_id:
        for i, t in enumerate(tasks):
            if t["task_id"] == latest_id:
                st.session_state["task_selector_index"] = i
                break

    if tasks:
        task_options = []
        for t in tasks:
            icon = TASK_STATE_ICONS.get(t["state"], "🕐")
            short_id = t["task_id"][:8]
            status_str = task_status_text(t["state"], t["progress"])
            subject = (
                t["subject"][:30] + ("…" if len(t["subject"]) > 30 else "")
                if t["subject"]
                else tr("No subject")
            )
            label = f"{icon} {short_id} — {subject}"
            if status_str:
                label += f" ({status_str})"
            task_options.append(label)

        # 使用一个固定 key + session_state 实现默认选中最新任务
        default_idx = st.session_state.get("task_selector_index", 0)
        selected_task_index = st.selectbox(
            tr("Select Task"),
            options=range(len(task_options)),
            format_func=lambda i: task_options[i],
            index=default_idx,
            key="task_selector",
            label_visibility="collapsed",
        )
        # 清除「最新」标记，让用户自由选择
        if st.session_state.get("latest_task_id"):
            st.session_state["latest_task_id"] = None

        st.session_state["selected_task"] = tasks[selected_task_index]
        selected_task = tasks[selected_task_index]
        st.caption(f"{tr('Videos')}: {len(selected_task['videos'])}")

        # 如果任务是进行中状态，显示进度提示
        if selected_task.get("state") == const.TASK_STATE_PROCESSING:
            st.info(f"⏳ {tr('Processing')}… {selected_task.get('progress', 0)}%")
        elif selected_task.get("state") == const.TASK_STATE_FAILED:
            error_msg = selected_task.get("error")
            st.error(f"❌ {tr('Failed')}" + (f": {error_msg}" if error_msg else ""))

        # --- Delete task ---
        delete_key = "confirm_delete_task"
        if st.button("🗑️ " + tr("Delete Task"), key="delete_task_btn"):
            st.session_state[delete_key] = selected_task["task_id"]

        if st.session_state.get(delete_key) == selected_task["task_id"]:
            col_yes, col_no = st.columns(2)
            with col_yes:
                if st.button("✅ " + tr("Confirm"), key="confirm_del"):
                    try:
                        import shutil
                        task_dir = os.path.join(utils.storage_dir("tasks"), selected_task["task_id"])
                        if os.path.isdir(task_dir):
                            shutil.rmtree(task_dir)
                            st.session_state.pop(delete_key, None)
                            st.session_state["selected_task"] = None
                            st.rerun()
                    except Exception as e:
                        st.error(str(e))
            with col_no:
                if st.button("❌ " + tr("Cancel"), key="cancel_del"):
                    st.session_state.pop(delete_key, None)
                    st.rerun()
    else:
        st.info(tr("No generated videos found"))
        st.session_state["selected_task"] = None


# 创建基础设置折叠框
if not config.app.get("hide_config", False):
    with st.expander(tr("Basic Settings"), expanded=False):
        config_panels = st.columns(3)
        left_config_panel = config_panels[0]
        middle_config_panel = config_panels[1]
        right_config_panel = config_panels[2]

        # 左侧面板 - 日志设置
        with left_config_panel:
            # 是否隐藏配置面板
            hide_config = st.checkbox(
                tr("Hide Basic Settings"), value=config.app.get("hide_config", False)
            )
            config.app["hide_config"] = hide_config

            # 是否禁用日志显示
            hide_log = st.checkbox(
                tr("Hide Log"), value=config.ui.get("hide_log", False)
            )
            config.ui["hide_log"] = hide_log

        # 中间面板 - LLM 设置

        with middle_config_panel:
            st.write(tr("LLM Settings"))
            llm_providers = [
                "OpenAI",
                "Moonshot",
                "Azure",
                "Qwen",
                "DeepSeek",
                "ModelScope",
                "Gemini",
                "Ollama",
                "G4f",
                "OneAPI",
                "Cloudflare",
                "ERNIE",
                "Pollinations",
            ]
            saved_llm_provider = config.app.get("llm_provider", "OpenAI").lower()
            saved_llm_provider_index = 0
            for i, provider in enumerate(llm_providers):
                if provider.lower() == saved_llm_provider:
                    saved_llm_provider_index = i
                    break

            llm_provider = st.selectbox(
                tr("LLM Provider"),
                options=llm_providers,
                index=saved_llm_provider_index,
            )
            llm_helper = st.container()
            llm_provider = llm_provider.lower()
            config.app["llm_provider"] = llm_provider

            llm_api_key = config.app.get(f"{llm_provider}_api_key", "")
            llm_secret_key = config.app.get(
                f"{llm_provider}_secret_key", ""
            )  # only for baidu ernie
            llm_base_url = config.app.get(f"{llm_provider}_base_url", "")
            llm_model_name = config.app.get(f"{llm_provider}_model_name", "")
            llm_account_id = config.app.get(f"{llm_provider}_account_id", "")

            tips = ""
            if llm_provider == "ollama":
                if not llm_model_name:
                    llm_model_name = "qwen:7b"
                if not llm_base_url:
                    llm_base_url = "http://localhost:11434/v1"

                with llm_helper:
                    tips = """
                            ##### Ollama配置说明
                            - **API Key**: 随便填写，比如 123
                            - **Base Url**: 一般为 http://localhost:11434/v1
                                - 如果 `MoneyPrinterTurbo` 和 `Ollama` **不在同一台机器上**，需要填写 `Ollama` 机器的IP地址
                                - 如果 `MoneyPrinterTurbo` 是 `Docker` 部署，建议填写 `http://host.docker.internal:11434/v1`
                            - **Model Name**: 使用 `ollama list` 查看，比如 `qwen:7b`
                            """

            if llm_provider == "openai":
                if not llm_model_name:
                    llm_model_name = "gpt-3.5-turbo"
                with llm_helper:
                    tips = """
                            ##### OpenAI 配置说明
                            > 需要VPN开启全局流量模式
                            - **API Key**: [点击到官网申请](https://platform.openai.com/api-keys)
                            - **Base Url**: 官方 OpenAI 可留空；如果使用 OpenAI 兼容供应商（例如 OpenRouter），请填写对应的兼容接口地址
                            - **Model Name**: 填写**有权限**的模型；如果使用兼容供应商，请填写该平台支持的模型 ID
                            """

            if llm_provider == "moonshot":
                if not llm_model_name:
                    llm_model_name = "moonshot-v1-8k"
                with llm_helper:
                    tips = """
                            ##### Moonshot 配置说明
                            - **API Key**: [点击到官网申请](https://platform.moonshot.cn/console/api-keys)
                            - **Base Url**: 固定为 https://api.moonshot.cn/v1
                            - **Model Name**: 比如 moonshot-v1-8k，[点击查看模型列表](https://platform.moonshot.cn/docs/intro#%E6%A8%A1%E5%9E%8B%E5%88%97%E8%A1%A8)
                            """
            if llm_provider == "oneapi":
                if not llm_model_name:
                    llm_model_name = (
                        "claude-3-5-sonnet-20240620"  # 默认模型，可以根据需要调整
                    )
                with llm_helper:
                    tips = """
                        ##### OneAPI 配置说明
                        - **API Key**: 填写您的 OneAPI 密钥
                        - **Base Url**: 填写 OneAPI 的基础 URL
                        - **Model Name**: 填写您要使用的模型名称，例如 claude-3-5-sonnet-20240620
                        """

            if llm_provider == "qwen":
                if not llm_model_name:
                    llm_model_name = "qwen-max"
                with llm_helper:
                    tips = """
                            ##### 通义千问Qwen 配置说明
                            - **API Key**: [点击到官网申请](https://dashscope.console.aliyun.com/apiKey)
                            - **Base Url**: 留空
                            - **Model Name**: 比如 qwen-max，[点击查看模型列表](https://help.aliyun.com/zh/dashscope/developer-reference/model-introduction#3ef6d0bcf91wy)
                            """

            if llm_provider == "g4f":
                if not llm_model_name:
                    llm_model_name = "gpt-3.5-turbo"
                with llm_helper:
                    tips = """
                            ##### gpt4free 配置说明
                            > [GitHub开源项目](https://github.com/xtekky/gpt4free)，可以免费使用GPT模型，但是**稳定性较差**
                            - **API Key**: 随便填写，比如 123
                            - **Base Url**: 留空
                            - **Model Name**: 比如 gpt-3.5-turbo，[点击查看模型列表](https://github.com/xtekky/gpt4free/blob/main/g4f/models.py#L308)
                            """
            if llm_provider == "azure":
                with llm_helper:
                    tips = """
                            ##### Azure 配置说明
                            > [点击查看如何部署模型](https://learn.microsoft.com/zh-cn/azure/ai-services/openai/how-to/create-resource)
                            - **API Key**: [点击到Azure后台创建](https://portal.azure.com/#view/Microsoft_Azure_ProjectOxford/CognitiveServicesHub/~/OpenAI)
                            - **Base Url**: 留空
                            - **Model Name**: 填写你实际的部署名
                            """

            if llm_provider == "gemini":
                if not llm_model_name:
                    llm_model_name = "gemini-1.0-pro"

                with llm_helper:
                    tips = """
                            ##### Gemini 配置说明
                            > 需要VPN开启全局流量模式
                            - **API Key**: [点击到官网申请](https://ai.google.dev/)
                            - **Base Url**: 留空
                            - **Model Name**: 比如 gemini-1.0-pro
                            """

            if llm_provider == "deepseek":
                if not llm_model_name:
                    llm_model_name = "deepseek-chat"
                if not llm_base_url:
                    llm_base_url = "https://api.deepseek.com"
                with llm_helper:
                    tips = """
                            ##### DeepSeek 配置说明
                            - **API Key**: [点击到官网申请](https://platform.deepseek.com/api_keys)
                            - **Base Url**: 固定为 https://api.deepseek.com
                            - **Model Name**: 固定为 deepseek-chat
                            """

            if llm_provider == "modelscope":
                if not llm_model_name:
                    llm_model_name = "Qwen/Qwen3-32B"
                if not llm_base_url:
                    llm_base_url = "https://api-inference.modelscope.cn/v1/"
                with llm_helper:
                    tips = """
                            ##### ModelScope 配置说明
                            - **API Key**: [点击到官网申请](https://modelscope.cn/docs/model-service/API-Inference/intro)
                            - **Base Url**: 固定为 https://api-inference.modelscope.cn/v1/
                            - **Model Name**: 比如 Qwen/Qwen3-32B，[点击查看模型列表](https://modelscope.cn/models?filter=inference_type&page=1)
                            """

            if llm_provider == "ernie":
                with llm_helper:
                    tips = """
                            ##### 百度文心一言 配置说明
                            - **API Key**: [点击到官网申请](https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application)
                            - **Secret Key**: [点击到官网申请](https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application)
                            - **Base Url**: 填写 **请求地址** [点击查看文档](https://cloud.baidu.com/doc/WENXINWORKSHOP/s/jlil56u11#%E8%AF%B7%E6%B1%82%E8%AF%B4%E6%98%8E)
                            """

            if llm_provider == "pollinations":
                if not llm_model_name:
                    llm_model_name = "default"
                with llm_helper:
                    tips = """
                            ##### Pollinations AI Configuration
                            - **API Key**: Optional - Leave empty for public access
                            - **Base Url**: Default is https://text.pollinations.ai/openai
                            - **Model Name**: Use 'openai-fast' or specify a model name
                            """

            if tips and config.ui["language"] == "zh":
                st.warning(
                    "中国用户建议使用 **DeepSeek** 或 **Moonshot** 作为大模型提供商\n- 国内可直接访问，不需要VPN \n- 注册就送额度，基本够用"
                )
                st.info(tips)

            st_llm_api_key = st.text_input(
                tr("API Key"), value=llm_api_key, type="password"
            )
            st_llm_base_url = st.text_input(tr("Base Url"), value=llm_base_url)
            st_llm_model_name = ""
            if llm_provider != "ernie":
                st_llm_model_name = st.text_input(
                    tr("Model Name"),
                    value=llm_model_name,
                    key=f"{llm_provider}_model_name_input",
                )
                if st_llm_model_name:
                    config.app[f"{llm_provider}_model_name"] = st_llm_model_name
            else:
                st_llm_model_name = None

            if st_llm_api_key:
                config.app[f"{llm_provider}_api_key"] = st_llm_api_key
            if st_llm_base_url:
                config.app[f"{llm_provider}_base_url"] = st_llm_base_url
            if st_llm_model_name:
                config.app[f"{llm_provider}_model_name"] = st_llm_model_name
            if llm_provider == "ernie":
                st_llm_secret_key = st.text_input(
                    tr("Secret Key"), value=llm_secret_key, type="password"
                )
                config.app[f"{llm_provider}_secret_key"] = st_llm_secret_key

            if llm_provider == "cloudflare":
                st_llm_account_id = st.text_input(
                    tr("Account ID"), value=llm_account_id
                )
                if st_llm_account_id:
                    config.app[f"{llm_provider}_account_id"] = st_llm_account_id

        # 右侧面板 - API 密钥设置
        with right_config_panel:

            def get_keys_from_config(cfg_key):
                api_keys = config.app.get(cfg_key, [])
                if isinstance(api_keys, str):
                    api_keys = [api_keys]
                api_key = ", ".join(api_keys)
                return api_key

            def save_keys_to_config(cfg_key, value):
                value = value.replace(" ", "")
                if value:
                    config.app[cfg_key] = value.split(",")

            st.write(tr("Video Source Settings"))

            pexels_api_key = get_keys_from_config("pexels_api_keys")
            pexels_api_key = st.text_input(
                tr("Pexels API Key"), value=pexels_api_key, type="password"
            )
            save_keys_to_config("pexels_api_keys", pexels_api_key)

            pixabay_api_key = get_keys_from_config("pixabay_api_keys")
            pixabay_api_key = st.text_input(
                tr("Pixabay API Key"), value=pixabay_api_key, type="password"
            )
            save_keys_to_config("pixabay_api_keys", pixabay_api_key)

            coverr_api_key = config.app.get("coverr_api_key", "")
            coverr_api_key = st.text_input(
                tr("Coverr API Key"), value=coverr_api_key, type="password"
            )
            config.app["coverr_api_key"] = coverr_api_key

            videvo_api_key = config.app.get("videvo_api_key", "")
            videvo_api_key = st.text_input(
                tr("Videvo API Key"), value=videvo_api_key, type="password"
            )
            config.app["videvo_api_key"] = videvo_api_key

llm_provider = config.app.get("llm_provider", "").lower()
panel = st.columns(3)
left_panel = panel[0]
middle_panel = panel[1]
right_panel = panel[2]

params = VideoParams(video_subject="")
uploaded_files = []

with left_panel:
    with st.container(border=True):
        st.write(tr("Video Script Settings"))
        # ── 智能推荐主题 ──
        TOPIC_CATEGORIES = [
            tr("Astronomy"), tr("Technology"), tr("Health"), tr("Finance"),
            tr("Motivation"), tr("Nature"), tr("History"), tr("Psychology"),
            tr("Education"), tr("Life Skills"), tr("Business"), tr("AI"),
        ]
        topic_category = st.selectbox(
            tr("Topic Category"),
            options=TOPIC_CATEGORIES,
            key="topic_category_sel",
        )

        col_gen, col_ref = st.columns([1, 1])
        with col_gen:
            gen_topics = st.button("🤖 " + tr("Generate Topic Ideas"), use_container_width=True)
        with col_ref:
            refresh_topics = st.button("🔄 " + tr("Refresh"), use_container_width=True)

        if gen_topics or refresh_topics:
            with st.spinner(tr("Generating topic ideas...")):
                historical = get_historical_subjects()
                generated = llm.generate_topics(category=topic_category, count=20, exclude_subjects=historical)
                if generated and "Error: " not in generated[0]:
                    st.session_state["suggested_topics"] = generated
                    st.rerun()
                else:
                    st.error(generated[0] if generated else tr("Generation failed"))

        if st.session_state.get("suggested_topics"):
            st.caption(tr("Click a topic to auto-fill"))
            topics = st.session_state["suggested_topics"]
            # 每行 4 个按钮展示
            for row_start in range(0, len(topics), 4):
                cols = st.columns(4)
                for ci in range(4):
                    idx = row_start + ci
                    if idx < len(topics):
                        topic = topics[idx]
                        btn_key = f"topic_btn_{topic_category}_{idx}"
                        if cols[ci].button(topic, key=btn_key, use_container_width=True):
                            st.session_state["video_subject"] = topic
                            st.rerun()

        params.video_subject = st.text_input(
            tr("Video Subject"),
            value=st.session_state["video_subject"],
            key="video_subject_input",
        ).strip()

        video_languages = [
            (tr("Auto Detect"), ""),
        ]
        for code in support_locales:
            video_languages.append((code, code))

        selected_index = st.selectbox(
            tr("Script Language"),
            index=0,
            options=range(
                len(video_languages)
            ),  # Use the index as the internal option value
            format_func=lambda x: video_languages[x][
                0
            ],  # The label is displayed to the user
        )
        params.video_language = video_languages[selected_index][1]

        saved_paragraph_number = config.ui.get("paragraph_number", 3)
        params.paragraph_number = st.slider(
            tr("Number of Paragraphs"),
            min_value=1,
            max_value=8,
            value=saved_paragraph_number,
            help=tr("More paragraphs = longer video script (approx 30-40 seconds per paragraph)"),
        )
        config.ui["paragraph_number"] = params.paragraph_number

        if st.button(
            tr("Generate Video Script and Keywords"), key="auto_generate_script"
        ):
            with st.spinner(tr("Generating Video Script and Keywords")):
                script = llm.generate_script(
                    video_subject=params.video_subject,
                    language=params.video_language,
                    paragraph_number=params.paragraph_number,
                )
                terms = llm.generate_terms(params.video_subject, script)
                if "Error: " in script:
                    st.error(tr(script))
                elif "Error: " in terms:
                    st.error(tr(terms))
                else:
                    st.session_state["video_script"] = script
                    st.session_state["video_terms"] = ", ".join(terms)
        params.video_script = st.text_area(
            tr("Video Script"), value=st.session_state["video_script"], height=280
        )
        if st.button(tr("Generate Video Keywords"), key="auto_generate_terms"):
            if not params.video_script:
                st.error(tr("Please Enter the Video Subject"))
                st.stop()

            with st.spinner(tr("Generating Video Keywords")):
                terms = llm.generate_terms(params.video_subject, params.video_script)
                if "Error: " in terms:
                    st.error(tr(terms))
                else:
                    st.session_state["video_terms"] = ", ".join(terms)

        params.video_terms = st.text_area(
            tr("Video Keywords"), value=st.session_state["video_terms"]
        )

        # ── 批量生成 ──
        st.divider()
        st.caption("🚀 " + tr("Batch Generation: use current settings to generate multiple videos with random topics"))
        col_b3, col_b5, col_b10 = st.columns(3)
        with col_b3:
            batch_3 = st.button("⚡ " + tr("Make 3 Videos"), use_container_width=True, key="batch_3")
        with col_b5:
            batch_5 = st.button("⚡ " + tr("Make 5 Videos"), use_container_width=True, key="batch_5")
        with col_b10:
            batch_10 = st.button("⚡ " + tr("Make 10 Videos"), use_container_width=True, key="batch_10")
        batch_clicked = batch_3 or batch_5 or batch_10
        batch_count = 3 if batch_3 else (5 if batch_5 else (10 if batch_10 else 0))

        if batch_clicked:
            st.session_state["pending_batch_count"] = batch_count
            st.session_state["pending_batch_category"] = topic_category

with middle_panel:
    with st.container(border=True):
        st.write(tr("Video Settings"))
        video_concat_modes = [
            (tr("Sequential"), "sequential"),
            (tr("Random"), "random"),
        ]
        video_sources = [
            (tr("Pexels"), "pexels"),
            (tr("Pixabay"), "pixabay"),
            (tr("Coverr"), "coverr"),
            (tr("Videvo"), "videvo"),
            (tr("AI Generated"), "ai_generated"),
            (tr("Local file"), "local"),
            (tr("TikTok"), "douyin"),
            (tr("Bilibili"), "bilibili"),
            (tr("Xiaohongshu"), "xiaohongshu"),
        ]

        saved_video_source_name = config.app.get("video_source", "pexels")
        saved_video_source_values = [v[1] for v in video_sources]
        saved_video_source_index = (
            saved_video_source_values.index(saved_video_source_name)
            if saved_video_source_name in saved_video_source_values
            else 0
        )

        selected_index = st.selectbox(
            tr("Video Source"),
            options=range(len(video_sources)),
            format_func=lambda x: video_sources[x][0],
            index=saved_video_source_index,
        )
        params.video_source = video_sources[selected_index][1]
        config.app["video_source"] = params.video_source

        if params.video_source == "local":
            # Streamlit 的文件类型校验对扩展名大小写敏感，这里同时放行大小写两种形式。
            local_file_types = ["mp4", "mov", "avi", "flv", "mkv", "jpg", "jpeg", "png"]
            uploaded_files = st.file_uploader(
                "Upload Local Files",
                type=local_file_types + [file_type.upper() for file_type in local_file_types],
                accept_multiple_files=True,
            )

        selected_index = st.selectbox(
            tr("Video Concat Mode"),
            index=1,
            options=range(
                len(video_concat_modes)
            ),  # Use the index as the internal option value
            format_func=lambda x: video_concat_modes[x][
                0
            ],  # The label is displayed to the user
        )
        params.video_concat_mode = VideoConcatMode(
            video_concat_modes[selected_index][1]
        )

        # 视频转场模式
        video_transition_modes = [
            (tr("None"), VideoTransitionMode.none.value),
            (tr("Shuffle"), VideoTransitionMode.shuffle.value),
            (tr("FadeIn"), VideoTransitionMode.fade_in.value),
            (tr("FadeOut"), VideoTransitionMode.fade_out.value),
            (tr("SlideIn"), VideoTransitionMode.slide_in.value),
            (tr("SlideOut"), VideoTransitionMode.slide_out.value),
        ]
        selected_index = st.selectbox(
            tr("Video Transition Mode"),
            options=range(len(video_transition_modes)),
            format_func=lambda x: video_transition_modes[x][0],
            index=0,
        )
        params.video_transition_mode = VideoTransitionMode(
            video_transition_modes[selected_index][1]
        )

        video_aspect_ratios = [
            (tr("Portrait"), VideoAspect.portrait.value),
            (tr("Landscape"), VideoAspect.landscape.value),
        ]
        selected_index = st.selectbox(
            tr("Video Ratio"),
            options=range(
                len(video_aspect_ratios)
            ),  # Use the index as the internal option value
            format_func=lambda x: video_aspect_ratios[x][
                0
            ],  # The label is displayed to the user
        )
        params.video_aspect = VideoAspect(video_aspect_ratios[selected_index][1])

        params.video_clip_duration = st.selectbox(
            tr("Clip Duration"), options=[2, 3, 4, 5, 6, 7, 8, 9, 10], index=1
        )
        params.video_count = st.selectbox(
            tr("Number of Videos Generated Simultaneously"),
            options=[1, 2, 3, 4, 5],
            index=0,
        )
    with st.container(border=True):
        st.write(tr("Audio Settings"))

        # 添加TTS服务器选择下拉框
        tts_servers = [
            ("azure-tts-v1", "Azure TTS V1"),
            ("azure-tts-v2", "Azure TTS V2"),
            ("siliconflow", "SiliconFlow TTS"),
            ("gemini-tts", "Google Gemini TTS"),
        ]

        # 获取保存的TTS服务器，默认为v1
        saved_tts_server = config.ui.get("tts_server", "azure-tts-v1")
        saved_tts_server_index = 0
        for i, (server_value, _) in enumerate(tts_servers):
            if server_value == saved_tts_server:
                saved_tts_server_index = i
                break

        selected_tts_server_index = st.selectbox(
            tr("TTS Servers"),
            options=range(len(tts_servers)),
            format_func=lambda x: tts_servers[x][1],
            index=saved_tts_server_index,
        )

        selected_tts_server = tts_servers[selected_tts_server_index][0]
        config.ui["tts_server"] = selected_tts_server

        # 根据选择的TTS服务器获取声音列表
        filtered_voices = []

        if selected_tts_server == "siliconflow":
            # 获取硅基流动的声音列表
            filtered_voices = voice.get_siliconflow_voices()
        elif selected_tts_server == "gemini-tts":
            # 获取Gemini TTS的声音列表
            filtered_voices = voice.get_gemini_voices()
        else:
            # 获取Azure的声音列表
            all_voices = voice.get_all_azure_voices(filter_locals=None)

            # 根据选择的TTS服务器筛选声音
            for v in all_voices:
                if selected_tts_server == "azure-tts-v2":
                    # V2版本的声音名称中包含"v2"
                    if "V2" in v:
                        filtered_voices.append(v)
                else:
                    # V1版本的声音名称中不包含"v2"
                    if "V2" not in v:
                        filtered_voices.append(v)

        friendly_names = {
            v: v.replace("Female", tr("Female"))
            .replace("Male", tr("Male"))
            .replace("Neural", "")
            for v in filtered_voices
        }

        saved_voice_name = config.ui.get("voice_name", "")
        saved_voice_name_index = 0

        # 检查保存的声音是否在当前筛选的声音列表中
        if saved_voice_name in friendly_names:
            saved_voice_name_index = list(friendly_names.keys()).index(saved_voice_name)
        else:
            # 如果不在，则根据当前UI语言选择一个默认声音
            for i, v in enumerate(filtered_voices):
                if v.lower().startswith(st.session_state["ui_language"].lower()):
                    saved_voice_name_index = i
                    break

        # 如果没有找到匹配的声音，使用第一个声音
        if saved_voice_name_index >= len(friendly_names) and friendly_names:
            saved_voice_name_index = 0

        # 确保有声音可选
        if friendly_names:
            selected_friendly_name = st.selectbox(
                tr("Speech Synthesis"),
                options=list(friendly_names.values()),
                index=min(saved_voice_name_index, len(friendly_names) - 1)
                if friendly_names
                else 0,
            )

            voice_name = list(friendly_names.keys())[
                list(friendly_names.values()).index(selected_friendly_name)
            ]
            params.voice_name = voice_name
            config.ui["voice_name"] = voice_name
        else:
            # 如果没有声音可选，显示提示信息
            st.warning(
                tr(
                    "No voices available for the selected TTS server. Please select another server."
                )
            )
            params.voice_name = ""
            config.ui["voice_name"] = ""

        # 只有在有声音可选时才显示试听按钮
        if friendly_names and st.button(tr("Play Voice")):
            play_content = params.video_subject
            if not play_content:
                play_content = params.video_script
            if not play_content:
                play_content = tr("Voice Example")
            with st.spinner(tr("Synthesizing Voice")):
                temp_dir = utils.storage_dir("temp", create=True)
                audio_file = os.path.join(temp_dir, f"tmp-voice-{str(uuid4())}.mp3")
                sub_maker = voice.tts(
                    text=play_content,
                    voice_name=voice_name,
                    voice_rate=params.voice_rate,
                    voice_file=audio_file,
                    voice_volume=params.voice_volume,
                )
                # if the voice file generation failed, try again with a default content.
                if not sub_maker:
                    play_content = "This is a example voice. if you hear this, the voice synthesis failed with the original content."
                    sub_maker = voice.tts(
                        text=play_content,
                        voice_name=voice_name,
                        voice_rate=params.voice_rate,
                        voice_file=audio_file,
                        voice_volume=params.voice_volume,
                    )

                if sub_maker and os.path.exists(audio_file):
                    st.audio(audio_file, format="audio/mp3")
                    if os.path.exists(audio_file):
                        os.remove(audio_file)

        # 当选择V2版本或者声音是V2声音时，显示服务区域和API key输入框
        if selected_tts_server == "azure-tts-v2" or (
            voice_name and voice.is_azure_v2_voice(voice_name)
        ):
            saved_azure_speech_region = config.azure.get("speech_region", "")
            saved_azure_speech_key = config.azure.get("speech_key", "")
            azure_speech_region = st.text_input(
                tr("Speech Region"),
                value=saved_azure_speech_region,
                key="azure_speech_region_input",
            )
            azure_speech_key = st.text_input(
                tr("Speech Key"),
                value=saved_azure_speech_key,
                type="password",
                key="azure_speech_key_input",
            )
            config.azure["speech_region"] = azure_speech_region
            config.azure["speech_key"] = azure_speech_key

        # 当选择硅基流动时，显示API key输入框和说明信息
        if selected_tts_server == "siliconflow" or (
            voice_name and voice.is_siliconflow_voice(voice_name)
        ):
            saved_siliconflow_api_key = config.siliconflow.get("api_key", "")

            siliconflow_api_key = st.text_input(
                tr("SiliconFlow API Key"),
                value=saved_siliconflow_api_key,
                type="password",
                key="siliconflow_api_key_input",
            )

            # 显示硅基流动的说明信息
            st.info(
                tr("SiliconFlow TTS Settings")
                + ":\n"
                + "- "
                + tr("Speed: Range [0.25, 4.0], default is 1.0")
                + "\n"
                + "- "
                + tr("Volume: Uses Speech Volume setting, default 1.0 maps to gain 0")
            )

            config.siliconflow["api_key"] = siliconflow_api_key

        params.voice_volume = st.selectbox(
            tr("Speech Volume"),
            options=[0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 3.0, 4.0, 5.0],
            index=2,
        )

        params.voice_rate = st.selectbox(
            tr("Speech Rate"),
            options=[0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 1.8, 2.0],
            index=2,
        )

        bgm_options = [
            (tr("No Background Music"), ""),
            (tr("Random Background Music"), "random"),
            (tr("Custom Background Music"), "custom"),
        ]
        selected_index = st.selectbox(
            tr("Background Music"),
            index=1,
            options=range(
                len(bgm_options)
            ),  # Use the index as the internal option value
            format_func=lambda x: bgm_options[x][
                0
            ],  # The label is displayed to the user
        )
        # Get the selected background music type
        params.bgm_type = bgm_options[selected_index][1]

        # Show or hide components based on the selection
        if params.bgm_type == "custom":
            custom_bgm_file = st.text_input(
                tr("Custom Background Music File"), key="custom_bgm_file_input"
            )
            if custom_bgm_file and os.path.exists(custom_bgm_file):
                params.bgm_file = custom_bgm_file
                # st.write(f":red[已选择自定义背景音乐]：**{custom_bgm_file}**")
        params.bgm_volume = st.selectbox(
            tr("Background Music Volume"),
            options=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
            index=2,
        )

with right_panel:
    with st.container(border=True):
        st.write(tr("Subtitle Settings"))
        params.subtitle_enabled = st.checkbox(tr("Enable Subtitles"), value=True)
        font_names = get_all_fonts()
        saved_font_name = config.ui.get("font_name", "MicrosoftYaHeiBold.ttc")
        saved_font_name_index = 0
        if saved_font_name in font_names:
            saved_font_name_index = font_names.index(saved_font_name)
        params.font_name = st.selectbox(
            tr("Font"), font_names, index=saved_font_name_index
        )
        config.ui["font_name"] = params.font_name

        subtitle_positions = [
            (tr("Top"), "top"),
            (tr("Center"), "center"),
            (tr("Bottom"), "bottom"),
            (tr("Custom"), "custom"),
        ]
        saved_subtitle_position = config.ui.get("subtitle_position", "bottom")
        saved_position_index = 2
        for i, (_, pos_value) in enumerate(subtitle_positions):
            if pos_value == saved_subtitle_position:
                saved_position_index = i
                break
        selected_index = st.selectbox(
            tr("Position"),
            index=saved_position_index,
            options=range(len(subtitle_positions)),
            format_func=lambda x: subtitle_positions[x][0],
        )
        params.subtitle_position = subtitle_positions[selected_index][1]
        config.ui["subtitle_position"] = params.subtitle_position

        if params.subtitle_position == "custom":
            saved_custom_position = config.ui.get("custom_position", 70.0)
            custom_position = st.text_input(
                tr("Custom Position (% from top)"),
                value=str(saved_custom_position),
                key="custom_position_input",
            )
            try:
                params.custom_position = float(custom_position)
                if params.custom_position < 0 or params.custom_position > 100:
                    st.error(tr("Please enter a value between 0 and 100"))
                else:
                    config.ui["custom_position"] = params.custom_position
            except ValueError:
                st.error(tr("Please enter a valid number"))

        font_cols = st.columns([0.3, 0.7])
        with font_cols[0]:
            saved_text_fore_color = config.ui.get("text_fore_color", "#FFFFFF")
            params.text_fore_color = st.color_picker(
                tr("Font Color"), saved_text_fore_color
            )
            config.ui["text_fore_color"] = params.text_fore_color

        with font_cols[1]:
            saved_font_size = config.ui.get("font_size", 60)
            params.font_size = st.slider(tr("Font Size"), 30, 100, saved_font_size)
            config.ui["font_size"] = params.font_size

        stroke_cols = st.columns([0.3, 0.7])
        with stroke_cols[0]:
            saved_stroke_color = config.ui.get("stroke_color", "#000000")
            params.stroke_color = st.color_picker(
                tr("Stroke Color"), saved_stroke_color
            )
            config.ui["stroke_color"] = params.stroke_color
        with stroke_cols[1]:
            saved_stroke_width = config.ui.get("stroke_width", 1.5)
            params.stroke_width = st.slider(
                tr("Stroke Width"), 0.0, 10.0, saved_stroke_width
            )
            config.ui["stroke_width"] = params.stroke_width
    with st.expander(tr("Click to show API Key management"), expanded=False):
        st.subheader(tr("Manage Pexels and Pixabay API Keys"))

        col1, col2 = st.tabs(["Pexels API Keys", "Pixabay API Keys"])

        with col1:
            st.subheader("Pexels API Keys")
            if config.app["pexels_api_keys"]:
                st.write(tr("Current Keys:"))
                for key in config.app["pexels_api_keys"]:
                    st.code(key)
            else:
                st.info(tr("No Pexels API Keys currently"))

            new_key = st.text_input(tr("Add Pexels API Key"), key="pexels_new_key")
            if st.button(tr("Add Pexels API Key")):
                if new_key and new_key not in config.app["pexels_api_keys"]:
                    config.app["pexels_api_keys"].append(new_key)
                    config.save_config()
                    st.success(tr("Pexels API Key added successfully"))
                elif new_key in config.app["pexels_api_keys"]:
                    st.warning(tr("This API Key already exists"))
                else:
                    st.error(tr("Please enter a valid API Key"))

            if config.app["pexels_api_keys"]:
                delete_key = st.selectbox(
                    tr("Select Pexels API Key to delete"), config.app["pexels_api_keys"], key="pexels_delete_key"
                )
                if st.button(tr("Delete Selected Pexels API Key")):
                    config.app["pexels_api_keys"].remove(delete_key)
                    config.save_config()
                    st.success(tr("Pexels API Key deleted successfully"))

        with col2:
            st.subheader("Pixabay API Keys")

            if config.app["pixabay_api_keys"]:
                st.write(tr("Current Keys:"))
                for key in config.app["pixabay_api_keys"]:
                    st.code(key)
            else:
                st.info(tr("No Pixabay API Keys currently"))

            new_key = st.text_input(tr("Add Pixabay API Key"), key="pixabay_new_key")
            if st.button(tr("Add Pixabay API Key")):
                if new_key and new_key not in config.app["pixabay_api_keys"]:
                    config.app["pixabay_api_keys"].append(new_key)
                    config.save_config()
                    st.success(tr("Pixabay API Key added successfully"))
                elif new_key in config.app["pixabay_api_keys"]:
                    st.warning(tr("This API Key already exists"))
                else:
                    st.error(tr("Please enter a valid API Key"))

            if config.app["pixabay_api_keys"]:
                delete_key = st.selectbox(
                    tr("Select Pixabay API Key to delete"), config.app["pixabay_api_keys"], key="pixabay_delete_key"
                )
                if st.button(tr("Delete Selected Pixabay API Key")):
                    config.app["pixabay_api_keys"].remove(delete_key)
                    config.save_config()
                    st.success(tr("Pixabay API Key deleted successfully"))

    pending_batch_count = st.session_state.get("pending_batch_count", 0)
    if pending_batch_count:
        pending_batch_category = (
            st.session_state.get("pending_batch_category") or topic_category
        )
        st.session_state["pending_batch_count"] = 0
        st.session_state["pending_batch_category"] = ""
        launch_batch(pending_batch_count, pending_batch_category, params)

    start_button = st.button(tr("Generate Video"), use_container_width=True, type="primary")
    if start_button:
        config.save_config()
        task_id = str(uuid4())
        if not params.video_subject and not params.video_script:
            st.error(tr("Video Script and Subject Cannot Both Be Empty"))
            scroll_to_bottom()
            st.stop()

        if params.video_source not in ["pexels", "pixabay", "coverr", "videvo", "ai_generated", "local"]:
            st.error(tr("Please Select a Valid Video Source"))
            scroll_to_bottom()
            st.stop()

        if params.video_source == "pexels" and not config.app.get("pexels_api_keys", ""):
            st.error(tr("Please Enter the Pexels API Key"))
            scroll_to_bottom()
            st.stop()

        if params.video_source == "pixabay" and not config.app.get("pixabay_api_keys", ""):
            st.error(tr("Please Enter the Pixabay API Key"))
            scroll_to_bottom()
            st.stop()

        if params.video_source == "coverr" and not config.app.get("coverr_api_key", ""):
            st.error(tr("Please Enter the Coverr API Key"))
            scroll_to_bottom()
            st.stop()

        if params.video_source == "videvo" and not config.app.get("videvo_api_key", ""):
            st.error(tr("Please Enter the Videvo API Key"))
            scroll_to_bottom()
            st.stop()

        if not params.custom_audio_file and not params.voice_name:
            st.error(tr("No voices available for the selected TTS server. Please select another server."))
            scroll_to_bottom()
            st.stop()

        if uploaded_files:
            local_videos_dir = utils.storage_dir("local_videos", create=True)
            # 每次重新上传时都以本次选择的素材为准，避免旧素材不断重复追加。
            params.video_materials = []
            persisted_local_materials = []
            for file in uploaded_files:
                file_path = os.path.join(local_videos_dir, f"{file.file_id}_{file.name}")
                with open(file_path, "wb") as f:
                    f.write(file.getbuffer())
                    m = MaterialInfo()
                    m.provider = "local"
                    m.url = file_path
                    params.video_materials.append(m)
                    persisted_local_materials.append(
                        {
                            "provider": m.provider,
                            "url": m.url,
                            "duration": m.duration,
                        }
                    )
            # 将已上传并保存到本地的视频素材写入会话，供后续只改文案时直接复用。
            st.session_state["local_video_materials"] = persisted_local_materials
        elif params.video_source == "local" and st.session_state["local_video_materials"]:
            # 当用户没有重新上传文件时，复用最近一次已经保存到磁盘的本地素材列表。
            params.video_materials = []
            for material in st.session_state["local_video_materials"]:
                m = MaterialInfo()
                m.provider = material.get("provider", "local")
                m.url = material.get("url", "")
                m.duration = material.get("duration", 0)
                if m.url:
                    params.video_materials.append(m)

        # ── Async task launch ──
        task_log_dir = utils.task_dir(task_id)
        task_log_file = os.path.join(task_log_dir, "task.log")
        # 清理上个任务的日志 handler
        old_handler = st.session_state.get("task_log_handler_id")
        if old_handler:
            try:
                logger.remove(old_handler)
            except Exception:
                pass
        # enqueue=True 确保后台线程的日志也能正确写入文件
        new_handler = logger.add(task_log_file, level="DEBUG", rotation="10 MB", enqueue=True)
        st.session_state["task_log_handler_id"] = new_handler

        st.session_state["task_running"] = True
        st.session_state["current_task_id"] = task_id
        st.session_state["current_task_log"] = task_log_file
        st.session_state["latest_task_id"] = task_id

        st.toast(tr("Generating Video"))
        logger.info(tr("Start Generating Video"))
        logger.info(utils.to_json(params))

        # 后台线程执行，不阻塞 UI
        task_thread = utils.run_in_background(tm.start, task_id=task_id, params=params)
        st.session_state["task_thread_id"] = task_thread.ident
        st.session_state["task_start_time"] = time.time()

        scroll_to_bottom()
        st.rerun()


# ════════════════════════════════════════════════════════
# 实时执行日志（从文件读取，每次渲染都展示）
# ════════════════════════════════════════════════════════
_current_log_file = st.session_state.get("current_task_log", "")
if _current_log_file and os.path.exists(_current_log_file) and not config.ui.get("hide_log", False):
    try:
        with open(_current_log_file, "r", encoding="utf-8") as _f:
            _log_content = _f.read()
        if _log_content.strip():
            st.code(_log_content.strip(), line_wrap=False)
    except Exception:
        pass

# ════════════════════════════════════════════════════════
# Async task polling — 每次渲染都会检查
# ════════════════════════════════════════════════════════
if st.session_state.get("task_running"):
    batch_ids = st.session_state.get("batch_task_ids", [])
    if batch_ids:
        # ── Batch mode ──
        completed = 0
        failed = 0
        current_tid = None
        for tid in batch_ids:
            sd = sm.state.get_task(tid)
            if sd and sd.get("state") == const.TASK_STATE_COMPLETE:
                completed += 1
            elif sd and sd.get("state") == const.TASK_STATE_FAILED:
                failed += 1
            elif not current_tid:
                current_tid = tid

        total = len(batch_ids)
        done = completed + failed

        # Update current task info for log display
        if current_tid:
            if st.session_state.get("current_task_id") != current_tid:
                st.session_state["current_task_id"] = current_tid
                st.session_state["current_task_log"] = os.path.join(
                    utils.task_dir(current_tid), "task.log"
                )

        # Show batch progress bar
        if done < total:
            current_sd = sm.state.get_task(current_tid) if current_tid else {}
            cur_progress = current_sd.get("progress", 0) if current_sd else 0
            batch_progress = (done * 100 + cur_progress) / total
            progress_text = f"📦 {tr('Batch')} [{done}/{total}] | {tr('Processing')}… {cur_progress}% | ID: {(current_tid or '')[:8]}"
            st.progress(min(batch_progress, 100) / 100, text=progress_text)

        # Check if batch is complete
        if done >= total:
            st.session_state["task_running"] = False
            st.session_state["batch_task_ids"] = []
            st.balloons()
            success_count = completed
            if success_count > 0:
                st.success(tr("Batch generation completed") + f" — {success_count}/{total} {tr('videos succeed')}")
            if failed > 0:
                st.warning(f"{failed}/{total} {tr('videos failed')}")

            # Show all generated videos
            all_videos = []
            for tid in batch_ids:
                sd = sm.state.get_task(tid)
                if sd and sd.get("state") == const.TASK_STATE_COMPLETE:
                    vids = sd.get("videos", [])
                    for v in vids:
                        all_videos.append((tid, v))
            if all_videos:
                st.subheader("🎬 " + tr("Generated Videos"))
                cols = st.columns(min(len(all_videos), 3))
                for i, (tid, url) in enumerate(all_videos):
                    with cols[i % 3]:
                        st.caption(f"{tid[:8]} — {os.path.basename(url)}")
                        st.video(url)
            _cleanup_log_handler()

        # Batch thread liveness check
        if done < total:
            thread_id = st.session_state.get("task_thread_id")
            thread_dead = False
            if thread_id:
                alive = any(t.ident == thread_id and t.is_alive()
                            for t in threading.enumerate())
                if not alive:
                    thread_dead = True
                    st.session_state["task_running"] = False
                    st.session_state["batch_task_ids"] = []
                    # Mark remaining tasks as failed
                    for tid in batch_ids:
                        sd = sm.state.get_task(tid)
                        if sd and sd.get("state") not in [const.TASK_STATE_COMPLETE, const.TASK_STATE_FAILED]:
                            sm.state.update_task(tid, state=const.TASK_STATE_FAILED)
                    _cleanup_log_handler()
                    st.error(f"⛔ {tr('Batch generation failed')} — {tr('thread died')}")

            # Timeout
            if not thread_dead:
                start_time = st.session_state.get("task_start_time", time.time())
                elapsed = time.time() - start_time
                if elapsed > 3600:
                    thread_dead = True
                    st.session_state["task_running"] = False
                    st.session_state["batch_task_ids"] = []
                    for tid in batch_ids:
                        sd = sm.state.get_task(tid)
                        if sd and sd.get("state") not in [const.TASK_STATE_COMPLETE, const.TASK_STATE_FAILED]:
                            sm.state.update_task(tid, state=const.TASK_STATE_FAILED)
                    _cleanup_log_handler()
                    st.error(f"⏰ {tr('Batch generation failed')} — {tr('timeout')}")

            # Cancel button
            if not thread_dead:
                if st.button("⏹️ " + tr("Cancel")):
                    st.session_state["task_running"] = False
                    st.session_state["batch_task_ids"] = []
                    for tid in batch_ids:
                        sd = sm.state.get_task(tid)
                        if sd and sd.get("state") not in [const.TASK_STATE_COMPLETE, const.TASK_STATE_FAILED]:
                            sm.state.update_task(tid, state=const.TASK_STATE_FAILED)
                    _cleanup_log_handler()
                    st.rerun()

            if not thread_dead and not current_tid:
                time.sleep(2)
            if not thread_dead:
                time.sleep(3)
                st.rerun()
    else:
        # ── Single task mode ──
        task_id = st.session_state["current_task_id"]
        task_state_data = sm.state.get_task(task_id)

        if task_state_data:
            state = task_state_data.get("state")
            progress = task_state_data.get("progress", 0)

            progress_text = f"{tr('Processing')}… {progress}% | {tr('Task')}: {task_id[:8]}"
            st.progress(min(progress, 100) / 100, text=progress_text)

            if state == const.TASK_STATE_COMPLETE:
                st.session_state["task_running"] = False
                st.balloons()
                st.success(tr("Video Generation Completed"))

                video_files = task_state_data.get("videos", [])
                if video_files:
                    cols = st.columns(min(len(video_files), 3))
                    for i, url in enumerate(video_files):
                        with cols[i % 3]:
                            st.caption(os.path.basename(url))
                            st.video(url)
                open_task_folder(task_id)
                _cleanup_log_handler()

            elif state == const.TASK_STATE_FAILED:
                st.session_state["task_running"] = False
                error_msg = task_state_data.get("error")
                st.error(
                    tr("Video Generation Failed")
                    + (f": {error_msg}" if error_msg else "")
                )
                _cleanup_log_handler()

            else:
                thread_id = st.session_state.get("task_thread_id")
                thread_dead = False
                if thread_id:
                    alive = any(t.ident == thread_id and t.is_alive()
                                for t in threading.enumerate())
                    if not alive:
                        thread_dead = True
                        st.session_state["task_running"] = False
                        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
                        _cleanup_log_handler()
                        error_msg = task_state_data.get("error")
                        detail = error_msg or f"{tr('Processing')} {progress}%"
                        st.error(f"⛔ {tr('Generation failed')} — {detail}")

                start_time = st.session_state.get("task_start_time", time.time())
                elapsed = time.time() - start_time
                if not thread_dead and elapsed > 1800:
                    thread_dead = True
                    st.session_state["task_running"] = False
                    sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
                    _cleanup_log_handler()
                    error_msg = task_state_data.get("error")
                    detail = error_msg or f"{tr('Processing')} {progress}%"
                    st.error(f"⏰ {tr('Generation failed')} — {detail}")

                if not thread_dead:
                    if st.button("⏹️ " + tr("Cancel")):
                        st.session_state["task_running"] = False
                        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
                        _cleanup_log_handler()
                        st.rerun()

                if not thread_dead:
                    time.sleep(3)
                    st.rerun()
        else:
            time.sleep(2)
            st.rerun()

# ════════════════════════════════════════════════════════
# 有已选中的历史任务 → 展示视频
# ════════════════════════════════════════════════════════
selected_task = st.session_state.get("selected_task")
if selected_task and selected_task.get("videos"):
    st.divider()
    st.subheader("🎬 " + tr("Generated Videos"))
    short_id = selected_task["task_id"][:8]
    st.caption(f"{tr('Task')}: {short_id}")
    if selected_task.get("subject"):
        st.write(f"**{tr('Script')}**: {selected_task['subject']}")

    video_files = selected_task["videos"]
    cols = st.columns(min(len(video_files), 3))
    for i, video_path in enumerate(video_files):
        col_idx = i % 3
        with cols[col_idx]:
            video_name = os.path.basename(video_path)
            st.caption(video_name)
            st.video(video_path)

config.save_config()
