# 📡 A股消息雷达

每小时自动扫描全网A股消息，AI 筛选交易信号。

## 功能特点

- **全覆盖信息源**：新浪财经、东方财富、同花顺、财联社、华尔街见闻等主流财经媒体
- **官方信息**：证监会公告、央行政策、沪深交易所动态
- **资金流向**：北向资金、龙虎榜、大宗交易、融资融券
- **宏观经济**：GDP、CPI、PMI 等宏观数据及海外市场联动
- **AI 智能筛选**：基于交易决策导向，给出看多/看空/中性信号
- **每小时更新**：GitHub Actions 自动执行，数据实时推送
- **暗色主题**：默认深色主题，适合交易员使用

## 快速开始

### 1. 安装依赖

```bash
pip3 install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 文件，填入 AI 提供商的 API Key
```

### 3. 运行

```bash
# 一键运行（抓取 + AI 筛选）
python3 scripts/generate_report.py

# 或分步运行
python3 scripts/fetch_news.py      # 仅抓取
python3 scripts/ai_filter.py       # 仅 AI 筛选

# 去重 + 构建站点数据
python3 scripts/dedup_reports.py
python3 scripts/build.py
```

### 4. 本地预览

```bash
# 方式一：启动本地服务器（推荐，支持热更新）
python3 -m http.server 8080 -d site
# 然后访问 http://localhost:8080

# 方式二：直接用浏览器打开
open site/index.html  # macOS
```

## 信息源列表

| 类别 | 来源 |
|------|------|
| 综合财经 | 新浪财经、东方财富、同花顺、财联社、华尔街见闻、第一财经 |
| 证券媒体 | 证券时报、中国证券报、上海证券报 |
| 社区/研报 | 雪球热帖、行业研报 |
| 监管机构 | 证监会新闻、央行政策 |
| 资金数据 | 北向资金、龙虎榜、大宗交易、融资融券、限售股解禁 |
| 宏观/海外 | 宏观经济数据、Bloomberg China、Reuters China |
| 基金动态 | 公募/私募基金动态 |
| IPO | 新股/IPO 动态 |

## 技术架构

```
RSS/Google News → fetch_news.py → reports/{date}-{hour}-raw.json
                                              ↓
                                  ai_filter.py + prompts/
                                              ↓
                                  reports/{date}-{hour}.json
                                              ↓
                              dedup_reports.py (跨报告去重)
                                              ↓
                                        build.py
                                              ↓
                              site/data/ (JSON + all-data.js)
                                              ↓
                                  GitHub Pages (site/ 目录)
```

## License

MIT
