from __future__ import annotations

"""
AI 筛选脚本
读取 reports/{date}-{hour}-raw.json 中的原始新闻，通过 AI 大模型筛选A股交易相关重大消息。

用法：
    python scripts/ai_filter.py
    python scripts/ai_filter.py --date 2026-04-09 --hour 14
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# ── 配置 ──────────────────────────────────────────────

REPORTS_DIR = Path("reports")
REPORTS_DIR.mkdir(exist_ok=True)

LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)

PROMPTS_DIR = Path("prompts")
CONFIG_DIR = Path("config")

BJT = timezone(timedelta(hours=8))
NOW = datetime.now(BJT)
TODAY = NOW.strftime("%Y-%m-%d")
HOUR = NOW.strftime("%H")

# ── 日志配置 ──────────────────────────────────────────

logger = logging.getLogger("ai_filter")
logger.setLevel(logging.DEBUG)

_log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

_file_handler = logging.FileHandler(LOGS_DIR / f"{TODAY}.log", encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(_log_formatter)
logger.addHandler(_file_handler)

_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(_log_formatter)
logger.addHandler(_console_handler)

# ── AI 配置 ───────────────────────────────────────────

def _load_ai_config() -> dict:
    """加载 AI 提供商配置"""
    ai_config_path = CONFIG_DIR / "ai.json"
    if not ai_config_path.exists():
        logger.error(f"AI 配置文件不存在: {ai_config_path}")
        return {}
    with open(ai_config_path, "r", encoding="utf-8") as f:
        return json.load(f)

def _get_ai_client_and_model(provider_name: str = "") -> "tuple[OpenAI, str, int, float | None] | None":
    """根据 config/ai.json 创建 AI 客户端，返回 (client, model_name, max_tokens, temperature)。"""
    config = _load_ai_config()
    if not config:
        return None

    if not provider_name:
        provider_name = config.get("provider", "")
    providers = config.get("providers", {})
    provider = providers.get(provider_name)

    if not provider:
        logger.error(f"未找到 AI 提供商配置: {provider_name}")
        return None

    api_key = os.environ.get(provider.get("api_key_env", ""), "")
    if not api_key:
        logger.error(f"未设置环境变量 {provider.get('api_key_env')}，无法使用 {provider_name}")
        return None

    client = OpenAI(
        api_key=api_key,
        base_url=provider["base_url"],
    )
    model = provider["model"]
    max_tokens = provider.get("max_tokens", 16000)
    temperature = provider.get("temperature", None)
    logger.info(f"🤖 使用 AI 提供商: {provider_name} (模型: {model})")
    return client, model, max_tokens, temperature

def _get_fallback_providers() -> list[str]:
    """获取所有可用的 AI 提供商名称列表（默认提供商排第一）"""
    config = _load_ai_config()
    if not config:
        return []
    default = config.get("provider", "")
    providers = list(config.get("providers", {}).keys())
    ordered = [default] if default in providers else []
    for p in providers:
        if p != default:
            ordered.append(p)
    return ordered

# ── 加载原始新闻 ─────────────────────────────────────

def load_raw_news(date: str, hour: str) -> list[dict]:
    """从 reports/{date}-{hour}-raw.json 加载原始新闻"""
    raw_path = REPORTS_DIR / f"{date}-{hour}-raw.json"
    if not raw_path.exists():
        logger.error(f"原始新闻文件不存在: {raw_path}")
        logger.error("请先运行 python scripts/fetch_news.py 抓取新闻")
        return []
    data = json.loads(raw_path.read_text(encoding="utf-8"))
    news = data.get("news", [])
    logger.info(f"📋 已加载原始新闻: {raw_path} ({len(news)} 条)")
    return news

# ── Prompt 加载 ──────────────────────────────────────

def load_prompt(filename: str) -> str:
    """从 prompts/ 目录加载提示词文件"""
    path = PROMPTS_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"提示词文件不存在: {path}")
    return path.read_text(encoding="utf-8").strip()

# ── JSON 解析与修复 ──────────────────────────────────

def repair_json(text: str) -> str:
    """尝试修复常见的 JSON 格式问题"""
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    text = re.sub(r'^```(?:json)?\s*', '', text.strip())
    text = re.sub(r'\s*```$', '', text.strip())
    text = text.replace('\u201c', '\\"').replace('\u201d', '\\"')
    text = text.replace('\u2018', "'").replace('\u2019', "'")
    text = re.sub(r'(?<=": ")(.*?)(?=")', lambda m: m.group(0).replace('\n', '\\n'), text, flags=re.DOTALL)
    return text

def _fix_unescaped_quotes_in_json(text: str) -> str:
    """修复 JSON 字符串值中未转义的双引号"""
    result = []
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        if ch != '"':
            result.append(ch)
            i += 1
            continue

        result.append(ch)
        i += 1

        while i < n:
            ch = text[i]

            if ch == '\\':
                result.append(ch)
                i += 1
                if i < n:
                    result.append(text[i])
                    i += 1
                continue

            if ch == '"':
                rest = text[i+1:i+20].lstrip()
                if not rest or rest[0] in (',', '}', ']', ':'):
                    result.append(ch)
                    i += 1
                    break
                else:
                    result.append('\\')
                    result.append('"')
                    i += 1
                    continue

            result.append(ch)
            i += 1

    return ''.join(result)

def parse_json_response(content: str) -> "list[dict] | None":
    """从 AI 返回内容中提取并解析 JSON，带容错处理"""
    logger.debug(f"parse_json_response 输入长度: {len(content)} 字符")
    logger.debug(f"输入内容前 300 字符: {content[:300]}")

    content = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', content)

    # 尝试提取 {"news": [...]} 格式
    obj_match = re.search(r'\{.*\}', content, re.DOTALL)
    if obj_match:
        try:
            obj = json.loads(obj_match.group())
            if isinstance(obj.get("news"), list):
                logger.debug(f"从 JSON 对象的 news 字段提取到 {len(obj['news'])} 条记录")
                return obj["news"]
        except json.JSONDecodeError:
            pass

    # 尝试提取 JSON 数组
    json_match = re.search(r'\[.*\]', content, re.DOTALL)
    if not json_match:
        json_match = re.search(r'\[.*', content, re.DOTALL)
    if not json_match:
        logger.debug("完全未找到 JSON 结构，返回 None")
        return None

    raw = json_match.group()
    logger.debug(f"提取到 JSON 片段长度: {len(raw)} 字符")

    # 第一次尝试：直接解析
    try:
        result = json.loads(raw)
        logger.debug(f"第一次尝试（直接解析）成功，得到 {len(result)} 条记录")
        return result
    except json.JSONDecodeError as e:
        logger.debug(f"第一次尝试（直接解析）失败: {e}")

    # 第二次尝试：修复后解析
    try:
        repaired = repair_json(raw)
        result = json.loads(repaired)
        logger.debug(f"第二次尝试（修复后解析）成功，得到 {len(result)} 条记录")
        return result
    except json.JSONDecodeError as e:
        logger.debug(f"第二次尝试（修复后解析）失败: {e}")

    # 第三次尝试：修复未转义引号
    try:
        fixed = _fix_unescaped_quotes_in_json(raw)
        result = json.loads(fixed)
        logger.debug(f"第三次尝试（修复未转义引号）成功，得到 {len(result)} 条记录")
        return result
    except json.JSONDecodeError as e:
        logger.debug(f"第三次尝试（修复未转义引号）失败: {e}")

    # 第四次尝试：截断修复
    try:
        last_complete = -1
        depth = 0
        in_string = False
        escape_next = False
        for i, ch in enumerate(raw):
            if escape_next:
                escape_next = False
                continue
            if ch == '\\' and in_string:
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    last_complete = i

        if last_complete > 0:
            truncated = raw[:last_complete + 1].rstrip().rstrip(',') + ']'
            try:
                result = json.loads(truncated)
                if result:
                    logger.info(f"JSON 被截断，成功恢复 {len(result)} 条完整记录")
                    return result
            except json.JSONDecodeError:
                repaired = repair_json(truncated)
                try:
                    result = json.loads(repaired)
                    if result:
                        logger.info(f"JSON 被截断，修复后恢复 {len(result)} 条完整记录")
                        return result
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass

    # 第五次尝试：逐个顶层对象提取
    try:
        objects = []
        depth = 0
        start = -1
        in_string = False
        escape_next = False
        for i, ch in enumerate(raw):
            if escape_next:
                escape_next = False
                continue
            if ch == '\\' and in_string:
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and start >= 0:
                    objects.append(raw[start:i + 1])
                    start = -1
        results = []
        for obj_str in objects:
            try:
                obj = json.loads(repair_json(obj_str))
                if "title" in obj and "summary" in obj:
                    results.append(obj)
            except json.JSONDecodeError:
                continue
        if results:
            logger.debug(f"第五次尝试成功，恢复 {len(results)} 条记录")
            return results
    except Exception as e:
        logger.debug(f"第五次尝试异常: {e}")

    logger.debug("所有 JSON 解析尝试均失败，返回 None")
    return None

# ── 文本处理 ─────────────────────────────────────────

def _sanitize_text(text: str) -> str:
    """清洗文本：去除 HTML 标签、过长内容"""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&[a-zA-Z]+;', ' ', text)
    text = re.sub(r'&#\d+;', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def _build_news_text(news_items: list[dict], include_summary: bool = True, include_link: bool = True) -> str:
    """构建新闻摘要文本"""
    news_text = ""
    for item in news_items:
        title = _sanitize_text(item.get('title', ''))
        news_text += f"【{item['source']}】{title}\n"
        if include_summary and item.get('summary'):
            summary = _sanitize_text(str(item['summary']))[:200]
            news_text += f"  摘要: {summary}\n"
        if include_link and item.get('link'):
            news_text += f"  链接: {item['link']}\n"
        news_text += "\n"

    if len(news_text) > 50000:
        news_text = news_text[:50000] + "\n...(已截断)"
    return news_text

def _load_history_titles(hours: int = 6) -> str:
    """加载近几小时的历史新闻标题，供 AI 判断持续性/重复性"""
    lines = []
    now_dt = datetime.now(BJT)
    for i in range(1, hours + 1):
        past_dt = now_dt - timedelta(hours=i)
        past_date = past_dt.strftime("%Y-%m-%d")
        past_hour = past_dt.strftime("%H")
        titles_path = REPORTS_DIR / f"{past_date}-{past_hour}-raw-titles.md"
        if titles_path.exists():
            content = titles_path.read_text(encoding="utf-8")
            lines.append(content)
        else:
            lines.append(f"# {past_date} {past_hour}:00：无数据\n")
    return "\n".join(lines)


def _load_history_filtered_titles(hours: int = 6) -> tuple[list[str], set[str]]:
    """加载近几小时已筛选报告中的新闻标题和来源 URL，用于程序化去重"""
    titles = []
    urls = set()
    now_dt = datetime.now(BJT)
    for i in range(1, hours + 1):
        past_dt = now_dt - timedelta(hours=i)
        past_date = past_dt.strftime("%Y-%m-%d")
        past_hour = past_dt.strftime("%H")
        report_path = REPORTS_DIR / f"{past_date}-{past_hour}.json"
        if report_path.exists():
            try:
                data = json.loads(report_path.read_text(encoding="utf-8"))
                for item in data.get("news", []):
                    title = item.get("title", "").strip()
                    if title:
                        titles.append(title)
                    for s in item.get("sources", []):
                        if isinstance(s, dict):
                            url = s.get("url", "")
                            if url and url.startswith("http"):
                                urls.add(url)
            except Exception:
                pass
    return titles, urls


def _normalize_title(title: str) -> str:
    """将标题归一化，用于相似度比较：去除标点、空格、统一大小写"""
    title = title.lower()
    # 去除常见标点和空格
    title = re.sub(r'[\s，。、：；！？""''（）\[\]【】\-—·,.!?:;\'"()\[\]{}]', '', title)
    # 统一数字格式：去除千分位逗号
    title = re.sub(r'(\d),(\d)', r'\1\2', title)
    return title


def _bigram_similarity(a: str, b: str) -> float:
    """计算两个字符串的 bigram（二字组）相似度，适合捕捉中文近义表述"""
    if len(a) < 2 or len(b) < 2:
        return 0.0
    bigrams_a = set(a[i:i+2] for i in range(len(a) - 1))
    bigrams_b = set(b[i:i+2] for i in range(len(b) - 1))
    if not bigrams_a or not bigrams_b:
        return 0.0
    overlap = len(bigrams_a & bigrams_b)
    return (2.0 * overlap) / (len(bigrams_a) + len(bigrams_b))


def _titles_are_similar(title_a: str, title_b: str) -> bool:
    """判断两个标题是否描述同一事件"""
    norm_a = _normalize_title(title_a)
    norm_b = _normalize_title(title_b)

    # 完全相同
    if norm_a == norm_b:
        return True

    # 一个包含另一个
    if len(norm_a) > 4 and len(norm_b) > 4:
        if norm_a in norm_b or norm_b in norm_a:
            return True

    # 提取关键词（长度>=2的中文/英文片段），计算重叠率
    def extract_keywords(text: str) -> set:
        # 提取中文词（2字以上连续中文）和英文词
        cn_words = set(re.findall(r'[\u4e00-\u9fff]{2,}', text))
        en_words = set(re.findall(r'[a-z]{2,}', text))
        # 提取数字（含小数）
        numbers = set(re.findall(r'\d+\.?\d*', text))
        return cn_words | en_words | numbers

    kw_a = extract_keywords(norm_a)
    kw_b = extract_keywords(norm_b)

    if kw_a and kw_b:
        overlap = kw_a & kw_b
        smaller = min(len(kw_a), len(kw_b))
        # 如果较短标题的关键词有 70% 以上重叠，认为是同一事件
        if smaller > 0 and len(overlap) / smaller >= 0.7:
            return True

    # bigram 相似度兜底：捕捉"上线/上架/将上线"等近义表述
    if _bigram_similarity(norm_a, norm_b) >= 0.6:
        return True

    return False


def _dedup_against_history(filtered_news: list[dict], history_titles: list[str],
                          history_urls: set[str] | None = None) -> list[dict]:
    """对 AI 筛选结果进行程序化去重，移除与历史报告重复的新闻"""
    if not history_titles and not history_urls:
        return filtered_news

    if history_urls is None:
        history_urls = set()

    deduped = []
    removed = []

    for news in filtered_news:
        title = news.get("title", "").strip()

        # 优先级 1：URL 精确匹配
        news_urls = set()
        for s in news.get("sources", []):
            if isinstance(s, dict):
                url = s.get("url", "")
                if url and url.startswith("http"):
                    news_urls.add(url)

        dup_url = news_urls & history_urls
        if dup_url:
            removed.append((title, f"URL重复: {next(iter(dup_url))[:60]}"))
            continue

        # 优先级 2：标题相似度匹配
        is_dup = False
        for hist_title in history_titles:
            if _titles_are_similar(title, hist_title):
                is_dup = True
                removed.append((title, f"标题相似: {hist_title}"))
                break
        if not is_dup:
            deduped.append(news)

    if removed:
        logger.info(f"🔄 去重：移除 {len(removed)} 条与历史报告重复的新闻")
        for new_t, reason in removed:
            logger.debug(f"  去重: 「{new_t}」 → {reason}")

    return deduped

# ── AI 调用 ──────────────────────────────────────────

def _call_ai_once(client, model_name: str, system_prompt: str, user_prompt: str,
                  attempt_label: str, temperature: float = 0.3,
                  max_tokens: int = 16000) -> "list[dict] | None | str":
    """单次 AI 调用，返回解析后的列表，失败返回 None，429 过载返回 'rate_limited'"""
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content.strip()
        finish_reason = response.choices[0].finish_reason
        usage = response.usage

        ai_dump_path = LOGS_DIR / f"{TODAY}-ai-response.txt"
        with open(ai_dump_path, "a", encoding="utf-8") as f:
            f.write(
                f"\n{'=' * 60}\n"
                f"attempt: {attempt_label}\n"
                f"finish_reason: {finish_reason}\n"
                f"usage: {usage}\n"
                f"content length: {len(content)}\n"
                f"{'=' * 60}\n"
                f"{content}\n"
            )
        logger.info(f"AI 原始返回已保存: {ai_dump_path} (finish_reason={finish_reason}, {len(content)} 字符)")

        ai_raw_path = REPORTS_DIR / f"{TODAY}-{HOUR}-ai-raw.json"
        ai_raw_data = {
            "date": TODAY,
            "hour": HOUR,
            "model": model_name,
            "attempt": attempt_label,
            "finish_reason": finish_reason,
            "usage": {
                "prompt_tokens": usage.prompt_tokens if usage else None,
                "completion_tokens": usage.completion_tokens if usage else None,
                "total_tokens": usage.total_tokens if usage else None,
            },
            "content_length": len(content),
            "raw_content": content,
        }
        ai_raw_path.write_text(json.dumps(ai_raw_data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"📦 AI 原始返回 JSON 已保存: {ai_raw_path}")

        result = parse_json_response(content)
        if result is not None:
            logger.info(f"✅ AI 返回 {len(result)} 条新闻")
            return result
        else:
            logger.warning(f"AI 返回内容无法解析为 JSON (finish_reason={finish_reason}, 长度={len(content)})")
            return None

    except Exception as e:
        error_str = str(e)
        is_content_filter = ("1301" in error_str or "contentFilter" in error_str
                             or "content_filter" in error_str or "不安全或敏感内容" in error_str
                             or "high risk" in error_str)
        is_rate_limited = "429" in error_str or "overloaded" in error_str or "rate" in error_str.lower()
        if is_content_filter:
            logger.warning(f"⚠️ {attempt_label} 触发内容安全过滤，将尝试更精简的输入策略")
            return None
        elif is_rate_limited:
            logger.warning(f"⏳ {attempt_label} 服务过载 (429)，需要等待后重试")
            return "rate_limited"
        else:
            logger.error(f"AI 调用异常 ({attempt_label}): {e}")
            return None

# ── AI 筛选主逻辑 ────────────────────────────────────

def _try_filter_with_provider(provider_name: str, news_items: list[dict],
                              system_prompt: str, user_prompt_template: str,
                              history_text: str) -> "list[dict] | None":
    """用指定的 AI 提供商尝试筛选新闻"""
    ai_setup = _get_ai_client_and_model(provider_name)
    if not ai_setup:
        return None

    client, model_name, max_tokens, fixed_temperature = ai_setup

    input_strategies = [
        {"name": "仅标题+链接", "include_summary": False, "include_link": True,
         "items": news_items, "skip_history": False},
        {"name": "完整内容", "include_summary": True, "include_link": True,
         "items": news_items, "skip_history": False},
        {"name": "仅标题（无历史）", "include_summary": False, "include_link": True,
         "items": news_items, "skip_history": True},
    ]

    MAX_TOTAL_ATTEMPTS = 6
    MIN_EXPECTED = 3  # A股每小时消息不一定很多
    total_attempt = 0
    best_partial = None

    for strategy_idx, strategy in enumerate(input_strategies):
        news_text = _build_news_text(strategy["items"], strategy["include_summary"],
                                     strategy.get("include_link", True))
        effective_history = "" if strategy.get("skip_history") else history_text
        user_prompt = user_prompt_template.format(
            date=TODAY,
            hour=HOUR,
            count=len(strategy["items"]),
            news_text=news_text,
            history_text=effective_history,
        )

        temp = fixed_temperature if fixed_temperature is not None else 0.3
        max_retries_for_strategy = 3 if strategy_idx == 0 else 2

        for retry in range(max_retries_for_strategy):
            if total_attempt >= MAX_TOTAL_ATTEMPTS:
                logger.warning(f"[{provider_name}] 已达最大尝试次数 {MAX_TOTAL_ATTEMPTS}，停止")
                break

            total_attempt += 1
            label = f"[{provider_name}] {total_attempt}/{MAX_TOTAL_ATTEMPTS} 策略={strategy['name']} retry={retry}"
            logger.info(f"🤖 AI 筛选 [{label}]（{len(strategy['items'])} 条，{len(news_text)} 字符，temp={temp}）")

            result = _call_ai_once(client, model_name, system_prompt, user_prompt, label,
                                   temperature=temp, max_tokens=max_tokens)

            if result == "rate_limited":
                wait_sec = 15 * (retry + 1)
                logger.info(f"⏳ 等待 {wait_sec} 秒后重试...")
                time.sleep(wait_sec)
                continue

            if result is None:
                logger.warning(f"[{label}] AI 调用失败或解析失败，继续下一次尝试")
                continue

            if len(result) >= MIN_EXPECTED:
                logger.info(f"✅ AI 筛选出 {len(result)} 条交易相关消息，符合预期")
                return result

            if len(result) == 0:
                logger.warning(f"[{label}] AI 返回空数组，将重试")
                continue

            if 0 < len(result) < MIN_EXPECTED:
                logger.warning(f"[{label}] AI 只返回了 {len(result)} 条（期望 {MIN_EXPECTED}），将重试")
                if best_partial is None or len(result) > len(best_partial):
                    best_partial = result
                continue

        if total_attempt >= MAX_TOTAL_ATTEMPTS:
            break

    if best_partial:
        logger.warning(f"⚠️ [{provider_name}] 未达到 {MIN_EXPECTED} 条，最佳部分结果: {len(best_partial)} 条")
    return best_partial

def _backfill_sources(filtered_news: list[dict], raw_news: list[dict]) -> list[dict]:
    """后处理：确保每条筛选结果都有带链接的 sources 字段。"""
    raw_index: list[tuple[str, str, str]] = []
    for item in raw_news:
        title = item.get("title", "").strip().lower()
        source = item.get("source", "")
        link = item.get("link", "")
        if title:
            raw_index.append((title, source, link))

    def _find_matching_sources(news_title: str) -> list[dict]:
        """从原始新闻中查找与标题相关的来源"""
        matches = []
        seen_urls = set()
        title_lower = news_title.lower()
        keywords = [w for w in re.split(r'[\s，。、：；！？""''（）\[\]【】]', title_lower) if len(w) >= 2]
        for raw_title, raw_source, raw_link in raw_index:
            if not raw_link:
                continue
            matched = (title_lower in raw_title or raw_title in title_lower)
            if not matched and keywords:
                hit_count = sum(1 for kw in keywords if kw in raw_title)
                matched = hit_count >= max(1, len(keywords) // 2)
            if matched and raw_link not in seen_urls:
                matches.append({"name": raw_source, "url": raw_link})
                seen_urls.add(raw_link)
        return matches

    for news in filtered_news:
        sources = news.get("sources", [])
        normalized = []
        for s in sources:
            if isinstance(s, dict):
                normalized.append(s)
            elif isinstance(s, str):
                normalized.append({"name": s, "url": ""})

        has_valid_url = any(
            isinstance(s, dict) and s.get("url", "").startswith("http")
            for s in normalized
        )

        if not has_valid_url:
            matched = _find_matching_sources(news.get("title", ""))
            if matched:
                news["sources"] = matched
                logger.debug(f"🔗 回填来源链接: {news.get('title', '')[:20]}... → {len(matched)} 个来源")
                continue

        for s in normalized:
            if isinstance(s, dict) and not s.get("url", "").startswith("http"):
                name = s.get("name", "")
                for raw_title, raw_source, raw_link in raw_index:
                    if raw_source == name and raw_link:
                        s["url"] = raw_link
                        break

        news["sources"] = normalized

        still_no_url = not any(
            isinstance(s, dict) and s.get("url", "").startswith("http")
            for s in news["sources"]
        )
        if still_no_url:
            title = news.get("title", "")
            news["sources"] = [{"name": "搜索查看", "url": f"https://www.google.com/search?q={title}+A股"}]
            logger.debug(f"🔗 兜底搜索链接: {title[:20]}...")

    total = len(filtered_news)
    with_url = sum(1 for n in filtered_news
                   if any(isinstance(s, dict) and s.get("url", "").startswith("http")
                          for s in n.get("sources", [])))
    logger.info(f"🔗 来源链接覆盖: {with_url}/{total} 条新闻有有效链接")

    return filtered_news

def ai_filter_news(news_items: list[dict]) -> list[dict]:
    """用 AI 大模型筛选交易相关消息，支持多提供商 fallback。"""

    system_prompt = load_prompt("filter_news.md")
    user_prompt_template = load_prompt("filter_news_user.md")

    history_text = _load_history_titles(hours=6)
    logger.info(f"📚 已加载历史新闻标题（{len(history_text)} 字符）")

    # 加载历史已筛选报告标题和 URL，用于程序化去重
    history_filtered_titles, history_filtered_urls = _load_history_filtered_titles(hours=6)
    logger.info(f"📚 已加载历史筛选标题 {len(history_filtered_titles)} 条、URL {len(history_filtered_urls)} 个，用于去重")

    providers = _get_fallback_providers()
    if not providers:
        logger.error("无可用的 AI 提供商，跳过 AI 筛选")
        return []

    best_result = None

    for provider_name in providers:
        logger.info(f"🔄 尝试 AI 提供商: {provider_name}")
        result = _try_filter_with_provider(
            provider_name, news_items, system_prompt, user_prompt_template, history_text
        )

        if result and len(result) >= 3:
            result = _backfill_sources(result, news_items)
            result = _dedup_against_history(result, history_filtered_titles, history_filtered_urls)
            return result

        if result and (best_result is None or len(result) > len(best_result)):
            best_result = result

        if len(providers) > 1:
            logger.info(f"⚠️ {provider_name} 未能产出满意结果，尝试下一个提供商")

    if best_result:
        logger.warning(f"⚠️ 所有提供商均未达到 3 条，使用最佳部分结果（{len(best_result)} 条）")
        best_result = _backfill_sources(best_result, news_items)
        best_result = _dedup_against_history(best_result, history_filtered_titles, history_filtered_urls)
        return best_result

    logger.error("❌ 所有提供商均失败，返回空列表")
    return []

# ── 主流程 ────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="AI 筛选币圈消息")
    parser.add_argument("--date", default=TODAY, help="指定日期，格式 YYYY-MM-DD，默认今天")
    parser.add_argument("--hour", default=HOUR, help="指定小时，格式 HH，默认当前小时")
    args = parser.parse_args()

    date = args.date
    hour = args.hour
    logger.info(f"🤖 开始 AI 筛选 {date} {hour}:00 A股消息")

    # 1. 加载原始新闻
    news_items = load_raw_news(date, hour)
    if not news_items:
        logger.warning("无原始新闻数据，生成空报告")

    # 2. AI 筛选
    filtered = ai_filter_news(news_items)

    # 3. 保存结构化 JSON
    json_path = REPORTS_DIR / f"{date}-{hour}.json"
    json_data = {
        "date": date,
        "hour": hour,
        "news": filtered,
        "total_fetched": len(news_items),
        "generated_at": datetime.now(BJT).isoformat(),
    }
    json_path.write_text(json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"📦 JSON 已保存: {json_path}")

    logger.info(f"✅ AI 筛选完成，共 {len(filtered)} 条交易相关消息")

if __name__ == "__main__":
    main()
