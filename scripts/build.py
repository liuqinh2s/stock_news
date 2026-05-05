"""
构建脚本：将 reports/ 下的报告数据整理到 site/data/ 供前端读取
- reports-index.json: 所有报告的索引（日期 + 小时 + 标题列表）
- 每期 JSON 复制到 site/data/
- latest.json: 最新一期报告的快速访问入口（为 bitget_bot 联动准备）
"""

import json
import shutil
from pathlib import Path

REPORTS_DIR = Path("reports")
SITE_DATA_DIR = Path("site/data")
SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)

def build():
    index = []

    # 遍历所有非 raw 的 JSON 报告
    for json_file in sorted(REPORTS_DIR.glob("*.json"), reverse=True):
        name = json_file.name
        # 跳过 raw 文件和 ai-raw 文件
        if name.endswith("-raw.json") or name.endswith("-ai-raw.json"):
            continue

        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            news = data.get("news", [])
            news_titles = [n.get("title", "") for n in news]
            date = data.get("date", "")
            hour = data.get("hour", "00")

            index.append({
                "date": date,
                "hour": hour,
                "titles": news_titles,
                "count": len(news_titles),
            })
            # 复制 JSON 到 site/data/
            shutil.copy2(json_file, SITE_DATA_DIR / json_file.name)
        except Exception as e:
            print(f"[WARN] 解析失败 {json_file}: {e}")

    # 写入索引
    index_path = SITE_DATA_DIR / "reports-index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    # 生成 latest.json（最新一期报告的快速访问入口）
    if index:
        latest_entry = index[0]  # 已按倒序排列，第一个就是最新的
        latest_file = SITE_DATA_DIR / f"{latest_entry['date']}-{latest_entry['hour']}.json"
        if latest_file.exists():
            latest_data = json.loads(latest_file.read_text(encoding="utf-8"))
            latest_path = SITE_DATA_DIR / "latest.json"
            latest_path.write_text(json.dumps(latest_data, ensure_ascii=False, indent=2), encoding="utf-8")

    # 生成内联数据 JS（兼容 file:// 本地打开）
    all_data = {}
    for entry in index:
        key = f"{entry['date']}-{entry['hour']}"
        data_file = SITE_DATA_DIR / f"{key}.json"
        if data_file.exists():
            all_data[key] = json.loads(data_file.read_text(encoding="utf-8"))

    js_content = f"window.__NEWS_INDEX__ = {json.dumps(index, ensure_ascii=False)};\n"
    js_content += f"window.__NEWS_DATA__ = {json.dumps(all_data, ensure_ascii=False)};\n"
    (SITE_DATA_DIR / "all-data.js").write_text(js_content, encoding="utf-8")

    print(f"✅ 构建完成: {len(index)} 份报告, 索引已写入 {index_path}")

if __name__ == "__main__":
    build()
