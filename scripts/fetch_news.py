from __future__ import annotations

"""
A股新闻抓取脚本
从 RSS 源、财经媒体抓取A股相关新闻，保存原始数据到 reports/ 目录。

用法：
    python scripts/fetch_news.py
"""

import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import httpx
from dotenv import load_dotenv

load_dotenv()

# ── 配置 ──────────────────────────────────────────────

REPORTS_DIR = Path("reports")
REPORTS_DIR.mkdir(exist_ok=True)

LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)

CONFIG_DIR = Path("config")

FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")

BJT = timezone(timedelta(hours=8))
NOW = datetime.now(BJT)
TODAY = NOW.strftime("%Y-%m-%d")
HOUR = NOW.strftime("%H")

# 保留最近 2 小时内的内容
RETENTION_HOURS = 2

# ── 日志配置 ──────────────────────────────────────────

logger = logging.getLogger("fetch_news")
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

# ── 加载新闻源配置 ───────────────────────────────────

def load_feeds():
    """从 config/feeds.json 加载新闻源配置"""
    feeds_path = CONFIG_DIR / "feeds.json"
    if not feeds_path.exists():
        raise FileNotFoundError(f"新闻源配置文件不存在: {feeds_path}")
    data = json.loads(feeds_path.read_text(encoding="utf-8"))
    return data.get("rss_feeds", {}), data.get("trending_feeds", {})

RSS_FEEDS, TRENDING_FEEDS = load_feeds()

# ── 抓取函数 ──────────────────────────────────────────

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
REQUEST_TIMEOUT = 15

def _is_recent(published_str: str) -> bool:
    """判断 RSS 条目的发布时间是否在最近 2 小时内（北京时间）"""
    if not published_str:
        return True

    from email.utils import parsedate_to_datetime

    cutoff = NOW - timedelta(hours=RETENTION_HOURS)

    try:
        dt = parsedate_to_datetime(published_str)
        dt_bjt = dt.astimezone(BJT)
        return dt_bjt >= cutoff
    except Exception:
        pass

    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            dt = datetime.strptime(published_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=BJT)
            dt_bjt = dt.astimezone(BJT)
            return dt_bjt >= cutoff
        except ValueError:
            continue

    if TODAY in published_str:
        return True

    return True

def _parse_rss_response(name: str, text: str) -> list[dict]:
    """解析 RSS 响应文本，返回新闻列表（只保留最近 2 小时的新闻）"""
    feed = feedparser.parse(text)
    items = []
    skipped = 0
    for entry in feed.entries[:50]:
        published = entry.get("published", "")
        if not _is_recent(published):
            skipped += 1
            continue
        items.append({
            "source": name,
            "title": entry.get("title", "").strip(),
            "summary": entry.get("summary", "")[:500].strip(),
            "link": entry.get("link", ""),
            "published": published,
        })
        if len(items) >= 20:
            break
    if skipped:
        logger.info(f"  ⏭ {name}: 过滤掉 {skipped} 条非近期新闻")
    return items

async def _async_fetch_url(client: httpx.AsyncClient, url: str) -> str | None:
    """异步抓取单个 URL，返回响应文本或 None"""
    try:
        resp = await client.get(url, timeout=REQUEST_TIMEOUT, follow_redirects=True)
        if resp.status_code == 200:
            return resp.text
    except Exception as e:
        logger.debug(f"请求失败 {url}: {e}")
    return None

async def _async_fetch_rss(client: httpx.AsyncClient, name: str, urls: list[str]) -> list[dict]:
    """对一个源的多个 URL 依次尝试（异步），返回第一个成功的结果"""
    for url in urls:
        text = await _async_fetch_url(client, url)
        if text:
            items = _parse_rss_response(name, text)
            if items:
                return items
    return []

def fetch_with_firecrawl(url: str) -> str:
    """使用 Firecrawl API 抓取页面内容（备用方案）"""
    if not FIRECRAWL_API_KEY:
        return ""
    try:
        resp = httpx.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}"},
            json={"url": url, "formats": ["markdown"]},
            timeout=30,
        )
        data = resp.json()
        return data.get("data", {}).get("markdown", "")[:2000]
    except Exception as e:
        logger.warning(f"Firecrawl 抓取失败 {url}: {e}")
        return ""

def _get_urls(config) -> list[str]:
    """从配置中提取 URL 列表，兼容旧格式"""
    if isinstance(config, str):
        return [config]
    if isinstance(config, dict):
        if "urls" in config:
            return config["urls"]
        if "url" in config:
            return [config["url"]]
    return []

# ── 抓取主流程 ────────────────────────────────────────

async def collect_all_news() -> list[dict]:
    """并发抓取所有新闻源"""
    all_news = []
    tasks: list[tuple[str, asyncio.Task]] = []

    async with httpx.AsyncClient(headers={"User-Agent": BROWSER_UA}) as client:
        logger.info("📡 抓取 RSS 新闻源...")
        for name, config in RSS_FEEDS.items():
            urls = _get_urls(config)
            task = asyncio.create_task(_async_fetch_rss(client, name, urls))
            tasks.append((name, task))

        logger.info("📡 抓取趋势数据源...")
        for name, config in TRENDING_FEEDS.items():
            if isinstance(config, str):
                task = asyncio.create_task(_async_fetch_rss(client, name, [config]))
            elif isinstance(config, dict):
                urls = _get_urls(config)
                task = asyncio.create_task(_async_fetch_rss(client, name, urls))
            else:
                continue
            tasks.append((name, task))

        results = await asyncio.gather(*(t for _, t in tasks), return_exceptions=True)

    for (name, _), result in zip(tasks, results):
        if isinstance(result, Exception):
            logger.error(f"  ✗ {name}: 0 条 [ERROR: {result}]")
        else:
            all_news.extend(result)
            logger.info(f"  ✓ {name}: {len(result)} 条")

    logger.info(f"📊 共抓取 {len(all_news)} 条新闻")
    return all_news

def save_raw_news(all_news: list[dict]) -> None:
    """将抓取到的原始新闻保存到 reports/ 目录"""
    raw_path = REPORTS_DIR / f"{TODAY}-{HOUR}-raw.json"
    raw_data = {
        "date": TODAY,
        "hour": HOUR,
        "total": len(all_news),
        "fetched_at": NOW.isoformat(),
        "news": all_news,
    }
    raw_path.write_text(json.dumps(raw_data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"📋 原始新闻已保存: {raw_path} ({len(all_news)} 条)")

    # 生成标题摘要文件
    grouped = defaultdict(list)
    for item in all_news:
        title = item.get("title", "").strip()
        if title:
            grouped[item.get("source", "未知")].append(title)

    lines = [f"# {TODAY} {HOUR}:00 原始新闻标题 ({len(all_news)} 条)", ""]
    for source, titles in grouped.items():
        lines.append(f"## {source} ({len(titles)} 条)")
        for t in titles:
            lines.append(f"- {t}")
        lines.append("")

    titles_path = REPORTS_DIR / f"{TODAY}-{HOUR}-raw-titles.md"
    titles_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"📋 标题摘要已保存: {titles_path}")

def main():
    logger.info(f"🚀 开始抓取 {TODAY} {HOUR}:00 A股新闻")

    all_news = asyncio.run(collect_all_news())

    if not all_news:
        logger.warning("未抓取到任何新闻")

    save_raw_news(all_news)
    logger.info(f"✅ 抓取完成，共 {len(all_news)} 条，已保存到 reports/{TODAY}-{HOUR}-raw.json")

if __name__ == "__main__":
    main()
