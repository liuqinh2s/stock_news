"""
报告去重脚本
扫描 reports/ 下所有已生成的报告 JSON，跨报告去除重复新闻。
保留最早出现的那条，后续报告中的重复条目会被移除。

用法：
    python scripts/dedup_reports.py          # 去重所有报告
    python scripts/dedup_reports.py --dry-run # 仅预览，不修改文件

在 GitHub Actions 中，应在 build.py 之前运行。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPORTS_DIR = Path("reports")

# ── 标题相似度判断（与 ai_filter.py 保持一致）────────

def _normalize_title(title: str) -> str:
    """将标题归一化：去除标点、空格、统一小写"""
    title = title.lower()
    title = re.sub(
        r'[\s，。、：；！？""''（）\[\]【】\-—·,.!?:;\'"()\[\]{}]',
        '', title,
    )
    title = re.sub(r'(\d),(\d)', r'\1\2', title)
    return title

def _extract_keywords(text: str) -> set[str]:
    """提取关键词用于相似度比较"""
    cn_words = set(re.findall(r'[\u4e00-\u9fff]{2,}', text))
    en_words = set(re.findall(r'[a-z]{2,}', text))
    numbers = set(re.findall(r'\d+\.?\d*', text))
    return cn_words | en_words | numbers

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

    if norm_a == norm_b:
        return True

    if len(norm_a) > 4 and len(norm_b) > 4:
        if norm_a in norm_b or norm_b in norm_a:
            return True

    # 关键词重叠检测
    kw_a = _extract_keywords(norm_a)
    kw_b = _extract_keywords(norm_b)

    if kw_a and kw_b:
        overlap = kw_a & kw_b
        smaller = min(len(kw_a), len(kw_b))
        if smaller > 0 and len(overlap) / smaller >= 0.7:
            return True

    # bigram 相似度兜底
    if _bigram_similarity(norm_a, norm_b) >= 0.6:
        return True

    return False

# ── 去重主逻辑 ────────────────────────────────────────

def _report_sort_key(path: Path) -> str:
    """从文件名提取排序键，格式 YYYY-MM-DD-HH"""
    return path.stem

def collect_report_files() -> list[Path]:
    """收集所有非 raw 的报告 JSON，按时间正序排列（最早的在前）"""
    files = []
    for f in REPORTS_DIR.glob("*.json"):
        name = f.name
        if name.endswith("-raw.json") or name.endswith("-ai-raw.json"):
            continue
        files.append(f)
    files.sort(key=_report_sort_key)
    return files

def dedup_reports(dry_run: bool = False) -> dict:
    """
    跨报告去重：按时间顺序遍历，后出现的重复新闻被移除。
    返回统计信息。
    """
    files = collect_report_files()
    if not files:
        print("📭 没有找到报告文件")
        return {"total_files": 0, "total_removed": 0}

    seen_titles: list[str] = []
    seen_urls: set[str] = set()
    total_removed = 0
    modified_files = 0

    print(f"📋 找到 {len(files)} 份报告，开始去重...")

    for report_path in files:
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [WARN] 无法读取 {report_path}: {e}")
            continue

        news = data.get("news", [])
        if not news:
            continue

        deduped = []
        removed_in_file = []

        for item in news:
            title = item.get("title", "").strip()
            if not title:
                deduped.append(item)
                continue

            item_urls = set()
            for s in item.get("sources", []):
                if isinstance(s, dict):
                    url = s.get("url", "")
                    if url and url.startswith("http"):
                        item_urls.add(url)

            dup_url = item_urls & seen_urls
            if dup_url:
                removed_in_file.append((title, f"URL重复: {next(iter(dup_url))[:60]}..."))
                continue

            is_dup = False
            for seen in seen_titles:
                if _titles_are_similar(title, seen):
                    is_dup = True
                    removed_in_file.append((title, f"标题相似: {seen}"))
                    break

            if not is_dup:
                deduped.append(item)
                seen_titles.append(title)
                seen_urls.update(item_urls)

        if removed_in_file:
            total_removed += len(removed_in_file)
            modified_files += 1
            tag = "[DRY-RUN] " if dry_run else ""
            print(f"  {tag}📄 {report_path.name}: 移除 {len(removed_in_file)} 条重复")
            for new_t, old_t in removed_in_file:
                print(f"    ✗ 「{new_t}」 ≈ 已有「{old_t}」")

            if not dry_run:
                data["news"] = deduped
                report_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        else:
            for item in news:
                title = item.get("title", "").strip()
                if title:
                    seen_titles.append(title)
                for s in item.get("sources", []):
                    if isinstance(s, dict):
                        url = s.get("url", "")
                        if url and url.startswith("http"):
                            seen_urls.add(url)

    stats = {
        "total_files": len(files),
        "modified_files": modified_files,
        "total_removed": total_removed,
    }

    if total_removed == 0:
        print("✅ 没有发现重复新闻")
    else:
        mode = "预览" if dry_run else "清理"
        print(f"✅ 去重{mode}完成: 扫描 {len(files)} 份报告，"
              f"修改 {modified_files} 份，移除 {total_removed} 条重复新闻")

    return stats

# ── 入口 ──────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("🔍 预览模式（不修改文件）\n")
    else:
        print("🧹 开始清理重复新闻...\n")

    dedup_reports(dry_run=dry_run)

if __name__ == "__main__":
    main()
