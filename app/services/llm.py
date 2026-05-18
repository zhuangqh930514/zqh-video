import json
import logging
import re
import requests
from typing import List

import g4f
from loguru import logger
from openai import AzureOpenAI, OpenAI
from openai.types.chat import ChatCompletion

from app.config import config

_max_retries = 5
_DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
_DEPRECATED_GEMINI_MODELS = {"gemini-pro", "gemini-1.0-pro"}


def _normalize_text_response(content, llm_provider: str) -> str:
    # 不同 LLM SDK 在异常或被拦截场景下，可能返回 None、空字符串，
    # 甚至返回非字符串对象。这里统一做兜底校验，避免后续直接调用
    # `.replace()` 时抛出 `NoneType` 之类的属性错误。
    if content is None:
        raise ValueError(f"[{llm_provider}] returned empty text content")

    if not isinstance(content, str):
        raise TypeError(
            f"[{llm_provider}] returned non-text content: {type(content).__name__}"
        )

    content = content.strip()
    if not content:
        raise ValueError(f"[{llm_provider}] returned empty text content")

    return re.sub(r"\r\n?", "\n", content)


def _extract_chat_completion_text(response, llm_provider: str) -> str:
    # OpenAI 兼容接口在异常场景下，可能返回没有 choices、
    # 或者 choices/message/content 为空的响应对象。
    # 这里统一做结构校验，避免出现 `NoneType is not subscriptable`
    # 这类底层属性访问错误。
    choices = getattr(response, "choices", None)
    if not choices:
        raise ValueError(f"[{llm_provider}] returned empty choices")

    first_choice = choices[0]
    message = getattr(first_choice, "message", None)
    if message is None:
        raise ValueError(f"[{llm_provider}] returned empty message")

    content = getattr(message, "content", None)
    return _normalize_text_response(content, llm_provider)


def _generate_response(prompt: str) -> str:
    try:
        content = ""
        llm_provider = config.app.get("llm_provider", "openai")
        logger.info(f"llm provider: {llm_provider}")
        if llm_provider == "g4f":
            model_name = config.app.get("g4f_model_name", "")
            if not model_name:
                model_name = "gpt-3.5-turbo-16k-0613"
            content = g4f.ChatCompletion.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
            )
        else:
            api_version = ""  # for azure
            if llm_provider == "moonshot":
                api_key = config.app.get("moonshot_api_key")
                model_name = config.app.get("moonshot_model_name")
                base_url = "https://api.moonshot.cn/v1"
            elif llm_provider == "ollama":
                # api_key = config.app.get("openai_api_key")
                api_key = "ollama"  # any string works but you are required to have one
                model_name = config.app.get("ollama_model_name")
                base_url = config.app.get("ollama_base_url", "")
                if not base_url:
                    base_url = "http://localhost:11434/v1"
            elif llm_provider == "openai":
                api_key = config.app.get("openai_api_key")
                model_name = config.app.get("openai_model_name")
                base_url = config.app.get("openai_base_url", "")
                if not base_url:
                    base_url = "https://api.openai.com/v1"
            elif llm_provider == "oneapi":
                api_key = config.app.get("oneapi_api_key")
                model_name = config.app.get("oneapi_model_name")
                base_url = config.app.get("oneapi_base_url", "")
            elif llm_provider == "azure":
                api_key = config.app.get("azure_api_key")
                model_name = config.app.get("azure_model_name")
                base_url = config.app.get("azure_base_url", "")
                api_version = config.app.get("azure_api_version", "2024-02-15-preview")
            elif llm_provider == "gemini":
                api_key = config.app.get("gemini_api_key")
                model_name = config.app.get("gemini_model_name")
                base_url = config.app.get("gemini_base_url", "")
                # Gemini 旧模型名已经陆续下线，这里自动兼容历史配置，
                # 避免用户沿用旧值时直接收到 404。
                if not model_name:
                    model_name = _DEFAULT_GEMINI_MODEL
                elif model_name in _DEPRECATED_GEMINI_MODELS:
                    logger.warning(
                        f"gemini model '{model_name}' is deprecated, fallback to '{_DEFAULT_GEMINI_MODEL}'"
                    )
                    model_name = _DEFAULT_GEMINI_MODEL
            elif llm_provider == "qwen":
                api_key = config.app.get("qwen_api_key")
                model_name = config.app.get("qwen_model_name")
                base_url = "***"
            elif llm_provider == "cloudflare":
                api_key = config.app.get("cloudflare_api_key")
                model_name = config.app.get("cloudflare_model_name")
                account_id = config.app.get("cloudflare_account_id")
                base_url = "***"
            elif llm_provider == "minimax":
                api_key = config.app.get("minimax_api_key")
                model_name = config.app.get("minimax_model_name")
                base_url = config.app.get("minimax_base_url", "")
                if not base_url:
                    base_url = "https://api.minimax.io/v1"
            elif llm_provider == "deepseek":
                api_key = config.app.get("deepseek_api_key")
                model_name = config.app.get("deepseek_model_name")
                base_url = config.app.get("deepseek_base_url")
                if not base_url:
                    base_url = "https://api.deepseek.com"
            elif llm_provider == "modelscope":
                api_key = config.app.get("modelscope_api_key")
                model_name = config.app.get("modelscope_model_name")
                base_url = config.app.get("modelscope_base_url")
                if not base_url:
                    base_url = "https://api-inference.modelscope.cn/v1/"
            elif llm_provider == "ernie":
                api_key = config.app.get("ernie_api_key")
                secret_key = config.app.get("ernie_secret_key")
                base_url = config.app.get("ernie_base_url")
                model_name = "***"
                if not secret_key:
                    raise ValueError(
                        f"{llm_provider}: secret_key is not set, please set it in the config.toml file."
                    )
            elif llm_provider == "pollinations":
                try:
                    base_url = config.app.get("pollinations_base_url", "")
                    if not base_url:
                        base_url = "https://text.pollinations.ai/openai"
                    model_name = config.app.get("pollinations_model_name", "openai-fast")

                    # Prepare the payload
                    payload = {
                        "model": model_name,
                        "messages": [
                            {"role": "user", "content": prompt}
                        ],
                        "seed": 101  # Optional but helps with reproducibility
                    }

                    # Optional parameters if configured
                    if config.app.get("pollinations_private"):
                        payload["private"] = True
                    if config.app.get("pollinations_referrer"):
                        payload["referrer"] = config.app.get("pollinations_referrer")

                    headers = {
                        "Content-Type": "application/json"
                    }

                    # Make the API request
                    response = requests.post(
                        base_url, headers=headers, json=payload, timeout=(30, 120)
                    )
                    response.raise_for_status()
                    result = response.json()

                    if result and "choices" in result and len(result["choices"]) > 0:
                        content = result["choices"][0]["message"]["content"]
                        return _normalize_text_response(content, llm_provider)
                    else:
                        raise Exception(f"[{llm_provider}] returned an invalid response format")

                except requests.exceptions.RequestException as e:
                    raise Exception(f"[{llm_provider}] request failed: {str(e)}")
                except Exception as e:
                    raise Exception(f"[{llm_provider}] error: {str(e)}")

            if llm_provider not in ["pollinations", "ollama"]:  # Skip validation for providers that don't require API key
                if not api_key:
                    raise ValueError(
                        f"{llm_provider}: api_key is not set, please set it in the config.toml file."
                    )
                if not model_name:
                    raise ValueError(
                        f"{llm_provider}: model_name is not set, please set it in the config.toml file."
                    )
                if not base_url and llm_provider not in ["gemini"]:
                    raise ValueError(
                        f"{llm_provider}: base_url is not set, please set it in the config.toml file."
                    )

            if llm_provider == "qwen":
                import dashscope
                from dashscope.api_entities.dashscope_response import GenerationResponse

                dashscope.api_key = api_key
                response = dashscope.Generation.call(
                    model=model_name, messages=[{"role": "user", "content": prompt}]
                )
                if response:
                    if isinstance(response, GenerationResponse):
                        status_code = response.status_code
                        if status_code != 200:
                            raise Exception(
                                f'[{llm_provider}] returned an error response: "{response}"'
                            )

                        content = response["output"]["text"]
                        return _normalize_text_response(content, llm_provider)
                    else:
                        raise Exception(
                            f'[{llm_provider}] returned an invalid response: "{response}"'
                        )
                else:
                    raise Exception(f"[{llm_provider}] returned an empty response")

            if llm_provider == "gemini":
                import google.generativeai as genai

                if not base_url:
                    genai.configure(api_key=api_key, transport="rest")
                else:
                    genai.configure(api_key=api_key, transport="rest", client_options={'api_endpoint': base_url})

                generation_config = {
                    "temperature": 0.5,
                    "top_p": 1,
                    "top_k": 1,
                    "max_output_tokens": 2048,
                }

                safety_settings = [
                    {
                        "category": "HARM_CATEGORY_HARASSMENT",
                        "threshold": "BLOCK_ONLY_HIGH",
                    },
                    {
                        "category": "HARM_CATEGORY_HATE_SPEECH",
                        "threshold": "BLOCK_ONLY_HIGH",
                    },
                    {
                        "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                        "threshold": "BLOCK_ONLY_HIGH",
                    },
                    {
                        "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                        "threshold": "BLOCK_ONLY_HIGH",
                    },
                ]

                model = genai.GenerativeModel(
                    model_name=model_name,
                    generation_config=generation_config,
                    safety_settings=safety_settings,
                )

                try:
                    response = model.generate_content(prompt)
                    candidates = response.candidates
                    generated_text = candidates[0].content.parts[0].text
                except (AttributeError, IndexError) as e:
                    print("Gemini Error:", e)
                    raise ValueError(f"[{llm_provider}] returned invalid response content")

                return _normalize_text_response(generated_text, llm_provider)

            if llm_provider == "cloudflare":
                response = requests.post(
                    f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model_name}",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "messages": [
                            {
                                "role": "system",
                                "content": "You are a friendly assistant",
                            },
                            {"role": "user", "content": prompt},
                        ]
                    },
                    timeout=(30, 120),
                )
                result = response.json()
                logger.info(result)
                return _normalize_text_response(result["result"]["response"], llm_provider)

            if llm_provider == "ernie":
                response = requests.post(
                    "https://aip.baidubce.com/oauth/2.0/token",
                    params={
                        "grant_type": "client_credentials",
                        "client_id": api_key,
                        "client_secret": secret_key,
                    },
                    timeout=(30, 120),
                )
                access_token = response.json().get("access_token")
                url = f"{base_url}?access_token={access_token}"

                payload = json.dumps(
                    {
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.5,
                        "top_p": 0.8,
                        "penalty_score": 1,
                        "disable_search": False,
                        "enable_citation": False,
                        "response_format": "text",
                    }
                )
                headers = {"Content-Type": "application/json"}

                response = requests.request(
                    "POST", url, headers=headers, data=payload, timeout=(30, 120)
                ).json()
                return _normalize_text_response(response.get("result"), llm_provider)

            if llm_provider == "azure":
                client = AzureOpenAI(
                    api_key=api_key,
                    api_version=api_version,
                    azure_endpoint=base_url,
                )

            if llm_provider == "modelscope":
                content = ''
                client = OpenAI(
                    api_key=api_key,
                    base_url=base_url,
                )
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    extra_body={"enable_thinking": False},
                    stream=True
                )
                if response:
                    for chunk in response:
                        if not chunk.choices:
                            continue
                        delta = chunk.choices[0].delta
                        if delta and delta.content:
                            content += delta.content

                    if not content.strip():
                        raise ValueError("Empty content in stream response")

                    return _normalize_text_response(content, llm_provider)
                else:
                    raise Exception(f"[{llm_provider}] returned an empty response")

            else:
                client = OpenAI(
                    api_key=api_key,
                    base_url=base_url,
                )

            response = client.chat.completions.create(
                model=model_name, messages=[{"role": "user", "content": prompt}]
            )
            if response:
                if isinstance(response, ChatCompletion):
                    return _extract_chat_completion_text(response, llm_provider)
                else:
                    raise Exception(
                        f'[{llm_provider}] returned an invalid response: "{response}", please check your network '
                        f"connection and try again."
                    )
            else:
                raise Exception(
                    f"[{llm_provider}] returned an empty response, please check your network connection and try again."
                )

        return _normalize_text_response(content, llm_provider)
    except Exception as e:
        return f"Error: {str(e)}"


def generate_script(
    video_subject: str, language: str = "", paragraph_number: int = 3
) -> str:
    prompt = f"""
# Role: Douyin Short Video Script Writer

## Platform Context:
This script is for a Douyin (抖音) short video. The audience has a very short attention span — if the first 2 seconds are not captivating, they will swipe away immediately. Every sentence must fight for retention.

## Goals:
Write a highly engaging voiceover script optimized for Douyin's algorithm (high 2-second retention rate, high 5-second watch rate).

## Douyin Script Structure:
1. Hook (first 1-2 sentences, ~2 seconds): Must immediately grab attention. Use ONE of these techniques:
   - Bold claim: "你可能每天都在做这件事，但完全做错了"
   - Curiosity gap: "这个问题，99%的人都答不上来"
   - Direct challenge: "如果你的答案是A，那你已经被骗了很久了"
   - Number tease: "记住这三个字，你的生活会完全不同"
   - Counter-intuitive: "这个东西越贵反而越害你"
   - Personal address: "你相信吗？其实..."
   DO NOT use generic openings like "今天我们来聊聊" "大家好欢迎收看" "你知道吗" (too overused).

2. Delivery (remaining paragraphs, ~3-5 seconds each):
   - Deliver the actual content with high information density
   - Use short, punchy sentences — no sentence over 30 characters
   - Alternating rhythm: fact → example → insight → fact
   - No filler, no transition words like "那么" "所以呢" "接下来"
   - If listing items, announce the number upfront: "三个原因" "两个方法"
   - Every sentence must make the viewer want to hear the next one

3. Closing: End with a strong takeaway or thought-provoking line. Never end with "以上就是" "总的来说" or any summary phrase.

## Writing Rules:
1. The script must have exactly {paragraph_number} paragraph(s), separated by a blank line.
2. Each paragraph must contain 3-5 information-dense sentences — do NOT write single-sentence paragraphs.
3. Do not use any markdown, formatting, titles, or headings. Return only plain text.
4. Do not include speaker labels like "voiceover", "narrator", or "(pause)".
5. Never reference this prompt, the paragraph count, or the script structure in your response.
6. Respond in the same language as the video subject.
7. Include specific examples, numbers, or surprising comparisons. Avoid vague statements.
8. CRITICAL: No sentence over 30 characters. Break long ideas into multiple short sentences.
9. NO filler words: "那么" "所以呢" "接下来我们" "然后呢" "就是说" "其实呢".
10. NO summary endings: "总的来说" "综上所述" "以上就是" "最后我想说".

## Video Subject
{video_subject}
""".strip()
    if language:
        prompt += f"\n## Language\n{language}"

    final_script = ""
    logger.info(f"subject: {video_subject}")

    def format_response(response):
        # Clean the script
        # Remove asterisks, hashes
        response = response.replace("*", "")
        response = response.replace("#", "")

        # Remove markdown syntax
        response = re.sub(r"\[.*\]", "", response)
        response = re.sub(r"\(.*\)", "", response)

        paragraphs = re.split(r"\n\s*\n", response.strip())

        # Select the specified number of paragraphs
        # selected_paragraphs = paragraphs[:paragraph_number]

        # Join the selected paragraphs into a single string
        return "\n\n".join(paragraph.strip() for paragraph in paragraphs if paragraph.strip())

    for i in range(_max_retries):
        try:
            response = _generate_response(prompt=prompt)
            if response:
                final_script = format_response(response)
            else:
                logging.error("gpt returned an empty response")

            # g4f may return an error message
            if final_script and "当日额度已消耗完" in final_script:
                raise ValueError(final_script)

            if final_script:
                break
        except Exception as e:
            logger.error(f"failed to generate script: {e}")

        if i < _max_retries:
            logger.warning(f"failed to generate video script, trying again... {i + 1}")
    if "Error: " in final_script:
        logger.error(f"failed to generate video script: {final_script}")
    else:
        logger.success(f"completed: \n{final_script}")
    return final_script.strip()


def generate_terms(video_subject: str, video_script: str, amount: int = 8) -> List[str]:
    prompt = f"""
# Role: Stock Video Search Terms Generator

## Goals:
Generate {amount} search terms that will be used to query stock video libraries (Pexels, Pixabay, Mixkit, Coverr) for footage matching the video's content.

## Critical Rules:
1. Return ONLY a JSON array of strings, nothing else.
2. Each term must be 1-4 words in English.
3. Terms must describe GENERIC visual concepts that commonly exist in stock footage libraries.
4. Each term must correspond to a DIFFERENT visual scene or concept from the script — avoid generating multiple terms for the same idea.
5. Terms should cover the FULL scope of the script — distribute them across all paragraphs/sections.

## IMPORTANT - Stock Footage Awareness:
Stock libraries have abundant footage of:
- Business/office: "office workers", "meeting", "typing on laptop", "handshake", "business team"
- Nature/landscape: "mountain", "ocean wave", "sunset", "forest trail", "rain", "starry sky", "river"
- Urban/city: "city street", "night skyline", "traffic", "people walking", "modern building", "subway"
- Lifestyle/people: "family dinner", "person reading", "morning routine", "friends laughing", "couple walking"
- Food (generic): "cooking", "eating", "restaurant", "fresh vegetables", "kitchen", "coffee"
- Health/fitness: "running", "yoga", "meditation", "gym workout", "healthy food", "stretching"
- Technology: "computer", "smartphone", "robot", "data center", "coding", "digital screen"
- Abstract: "light bulb", "clock", "goal", "success", "teamwork", "growth", "journey"
- Science/space: "galaxy", "microscope", "laboratory", "earth from space", "DNA helix"
- Animals: "bird flying", "fish swimming", "butterfly", "dog playing", "wildlife"
- Travel: "airplane", "suitcase", "map", "beach", "mountain hiking", "road trip"
- Emotions: "smiling face", "thinking person", "celebration", "lonely person", "excited crowd"

## What to AVOID:
- Specific named entities: "Tokyo Tower", "Eiffel Tower", "McDonalds", "iPhone 16"
- Specific dishes: "Mapo Tofu", "Ramen", "Sushi"
- Rare or niche activities unlikely to be in stock footage
- Overly abstract terms that don't correspond to visible footage

## Strategy - Step by Step:
1. Read the entire video script carefully.
2. For each paragraph, identify 1-2 key visual scenes or concepts it describes.
3. For each visual scene, think: "What would I type into Pexels to find matching footage?"
4. Translate each concept into a short, generic English search term (1-4 words).
5. Ensure diversity: each term should represent a different visual element.
6. Prioritize terms that are LIKELY to return abundant, high-quality results on stock sites.

## Examples:
- Subject "Sichuan hot pot" → ["cooking", "boiling pot", "eating together", "restaurant", "steam food", "spicy food", "family dinner", "food preparation"]
- Subject "Tokyo travel guide" → ["city street", "night skyline", "people walking", "shopping district", "crosswalk", "subway train", "modern building", "japanese garden"]
- Subject "benefits of meditation" → ["person meditating", "calm nature", "sunrise", "peaceful lake", "yoga", "deep breathing", "mindfulness", "forest"]

## Output Format:
["term 1", "term 2", "term 3", ..., "term {amount}"]

## Context:
### Video Subject
{video_subject}

### Video Script
{video_script}
""".strip()

    logger.info(f"subject: {video_subject}")

    search_terms = []
    response = ""
    for i in range(_max_retries):
        try:
            response = _generate_response(prompt)
            if isinstance(response, str) and response.startswith("Error: "):
                logger.error(f"failed to generate video script: {response}")
                search_terms = []
                break
            search_terms = json.loads(response)
            if not isinstance(search_terms, list) or not all(
                isinstance(term, str) for term in search_terms
            ):
                logger.error("response is not a list of strings.")
                search_terms = []
                continue
            search_terms = [term.strip() for term in search_terms if term.strip()]

        except Exception as e:
            logger.warning(f"failed to generate video terms: {str(e)}")
            if response:
                match = re.search(r"\[.*]", response)
                if match:
                    try:
                        search_terms = json.loads(match.group())
                    except Exception as e:
                        logger.warning(f"failed to generate video terms: {str(e)}")
                        pass

        if search_terms and len(search_terms) > 0:
            break
        if i < _max_retries:
            logger.warning(f"failed to generate video terms, trying again... {i + 1}")

    if search_terms:
        logger.success(f"completed: \n{search_terms}")
    else:
        logger.error("failed to generate usable video terms.")
    return search_terms


def generate_topics(category: str, count: int = 20, exclude_subjects: List[str] = None) -> List[str]:
    """
    Generate a list of video topic ideas for a given category.
    Used by the WebUI's 「智能推荐主题」feature.
    """
    exclude_instruction = ""
    if exclude_subjects:
        exclude_instruction = f"""
## Exclusion List:
The following topics have ALREADY been generated by the user. DO NOT generate any topic that is semantically similar or overlaps with these:
{json.dumps(exclude_subjects, ensure_ascii=False)}

"""
    prompt = f"""
# Role: Short Video Topic Generator

## Goal:
Generate {count} engaging short-video topic ideas for the category "{category}".

## Constraints:
1. Return ONLY a JSON array of strings, nothing else.
2. Each topic must be 10-25 characters in Chinese (or translated equivalents).
3. Topics should be specific enough to be interesting, but conceptual enough that stock footage (Pexels/Pixabay) can provide matching visuals.
4. Focus on universal visual concepts: nature, city, people, technology, food, business, health, etc.
5. Avoid named entities (specific people, brands, products).
6. Cover different angles of the category — don't all say the same thing.
7. CRITICAL: Do NOT generate any topic that is semantically similar to or overlaps with the excluded topics listed below.
{exclude_instruction}
## Examples for category "天文科普":
["太阳系八大行星各自的特点", "月球正在慢慢远离地球", "极光是怎么产生的", "如果地球停止自转", "黑洞到底是什么", "人类离火星还有多远", "流星雨多久能看到一次", "恒星的一生从诞生到死亡", "宇宙到底有多大", "外星人存在吗"]

## Output Format:
["topic 1", "topic 2", ..., "topic {count}"]
""".strip()

    logger.info(f"generating {count} topics for category: {category}")
    topics = []
    response = ""
    for i in range(_max_retries):
        try:
            response = _generate_response(prompt)
            if isinstance(response, str) and response.startswith("Error: "):
                logger.error(f"failed to generate topics: {response}")
                topics = []
                break
            topics = json.loads(response)
            if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
                logger.warning("response is not a list of strings, retrying...")
                topics = []
                continue
            topics = [topic.strip() for topic in topics if topic.strip()][:count]
        except Exception as e:
            logger.warning(f"failed to generate topics (attempt {i+1}): {str(e)}")
            if response:
                match = re.search(r"\[.*\]", response)
                if match:
                    try:
                        topics = json.loads(match.group())[:count]
                    except Exception:
                        pass
        if topics and len(topics) > 0:
            break

    if topics:
        logger.success(f"generated {len(topics)} topics")
    else:
        logger.error("failed to generate usable topics")
    return topics


if __name__ == "__main__":
    video_subject = "生命的意义是什么"
    script = generate_script(
        video_subject=video_subject, language="zh-CN", paragraph_number=1
    )
    print("######################")
    print(script)
    search_terms = generate_terms(
        video_subject=video_subject, video_script=script, amount=5
    )
    print("######################")
    print(search_terms)

