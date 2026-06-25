---
name: cctv-xwlb
description: 采集央视《新闻联播》视频：下载完整视频 → 新闻切片 → 文字稿提取 → 口播稿件输出
argument-hint: [日期] [--output 输出目录]
---

# 央视《新闻联播》采集工具

一键完成：视频下载 → 新闻切片 → 文字稿提取 → 口播稿件输出

## 使用方式

```bash
# 下载昨天的新闻联播（默认输出到 ./{YYYYMMDD}_news/）
python xwlb_tool.py

# 指定日期
python xwlb_tool.py 2026-06-20

# 指定输出目录
python xwlb_tool.py 2026-06-20 -o ./my_output/

# 超清画质 + 文字稿
python xwlb_tool.py --quality 2000

# 仅获取文字稿不下载视频
python xwlb_tool.py --no-video

# 完整参数
python xwlb_tool.py [日期] [-o 输出目录] [-q 画质] [--no-video] [--no-text] [--no-asr] [--keep-full-only]
```

## 输出结构

```
{日期}_news/
├── README.md              # 汇总文档（含完整文字稿 + 切片路径）
├── full.mp4               # 完整视频
├── metadata.json          # 结构化元数据
├── segments/              # 视频切片（每条新闻独立）
│   ├── 01_习近平在山东德州考察.mp4
│   ├── 02_央视快评.mp4
│   └── ...
└── manuscripts/           # 口播稿件（逐条纯文本）
    ├── 01_xxx.txt
    └── ...
```

## 工作流程

1. 从栏目首页找到目标日期的完整版视频（方案A: requests；方案B: Playwright 兜底）
2. 提取内部GUID，调用VDN API获取视频流和分段信息
3. 下载完整视频（HLS流或MP4分段，支持多种码率）
4. 通过移动搜索查找news.cctv.com完整文章 → 提取文字稿
5. 未找到文字稿时，自动使用whisper语音识别
6. 视频切片（前后各裁剪0.5秒避免残留画面）
7. 生成逐条口播稿件 + 汇总README

## 依赖

```bash
pip install requests playwright beautifulsoup4 faster-whisper
python -m playwright install chromium
brew install ffmpeg
```

## 注意事项

- 视频版权归央视所有，仅供个人学习研究使用
- 央视API可能随时变更，如方案A失败会自动切换方案B
- 首次运行whisper会下载模型文件（约500MB）
- 高画质视频约400-500MB，请确保磁盘空间充足
