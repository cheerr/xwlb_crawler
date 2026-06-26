#!/usr/bin/env python3
"""
央视《新闻联播》完整采集工具 (Skill: cctv-xwlb)
=================================================
一键完成：视频下载 → 新闻切片 → 文字稿提取 → 口播稿件输出

输入: 日期(YYYY-MM-DD)，输出目录(可选，默认 ./{date}_news/)
输出:
  {date}_news/
  ├── README.md           # 汇总文档（文字稿 + 切片路径）
  ├── full.mp4            # 完整视频
  ├── metadata.json       # 结构化元数据
  ├── segments/           # 视频切片（每条新闻独立）
  │   ├── 01_xxx.mp4
  │   └── ...
  └── manuscripts/        # 口播稿件（逐条纯文本）
      ├── 01_[视频]xxx.txt
      └── ...

用法:
  python xwlb_tool.py                          # 下载昨天的
  python xwlb_tool.py 2026-06-20               # 指定日期
  python xwlb_tool.py 2026-06-20 -o ./output   # 指定输出目录
  python xwlb_tool.py --quality 2000           # 超清画质
  python xwlb_tool.py --no-video               # 仅获取文字稿
  python xwlb_tool.py --no-asr                 # 跳过语音识别

依赖:
  pip install requests playwright beautifulsoup4 faster-whisper
  python -m playwright install chromium
  brew install ffmpeg

作者：cheerr
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ============================================================================
# 配置
# ============================================================================

COLUMN_PAGE_URL = "https://tv.cctv.com/lm/xwlb/"
DAY_PAGE_URL = "https://tv.cctv.com/lm/xwlb/day/{date}.shtml"
VIDEO_INFO_API = "https://vdn.apps.cntv.cn/api/getHttpVideoInfo.do"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://tv.cctv.com/",
}

QUALITY_OPTIONS = {
    418: "chapters",
    818: "chapters2",
    1200: "chapters3",
    2000: "chapters4",
}


# ============================================================================
# 工具函数
# ============================================================================

def date_str(target: datetime) -> str:
    return target.strftime("%Y%m%d")


def date_path(target: datetime) -> str:
    return target.strftime("%Y/%m/%d")


def parse_date(s: str) -> datetime:
    s = s.strip()
    return datetime.strptime(s, "%Y%m%d") if len(s) == 8 else datetime.strptime(s, "%Y-%m-%d")


def safe_filename(name: str, max_len: int = 60) -> str:
    """将标题转为安全的文件名"""
    name = re.sub(r'[\\/:*?"<>|]', '', name)
    name = re.sub(r'\s+', '', name)
    if len(name) > max_len:
        name = name[:max_len]
    return name


def human_size(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


# ============================================================================
# 步骤1: 获取视频 VID（方案A: requests）
# ============================================================================

def find_full_episode_vid(target_date: datetime) -> str | None:
    """
    从栏目首页找到目标日期的完整版新闻联播视频URL。
    """
    ds = date_str(target_date)
    print(f"  [方案A] 抓取栏目首页: {COLUMN_PAGE_URL}")

    try:
        resp = requests.get(COLUMN_PAGE_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        html = resp.text
    except requests.RequestException as e:
        print(f"  ✗ 请求失败: {e}")
        return None

    # 匹配: <a href=".../VIDE....shtml">...<i class="sql0">完整版</i>《新闻联播》 YYYYMMDD 19:00</a>
    vid_pattern = re.compile(
        r'<a[^>]*href="(https?://tv\.cctv\.com/\d{4}/\d{2}/\d{2}/VIDE[a-zA-Z0-9]+\.shtml)"[^>]*>'
        r'((?:(?!</a>).)*)</a>',
        re.IGNORECASE | re.DOTALL,
    )

    for m in vid_pattern.finditer(html):
        url = m.group(1)
        inner = m.group(2)
        has_sql0 = 'sql0' in inner
        has_xwlb = '新闻联播' in inner
        has_date = ds in inner
        if has_sql0 and has_xwlb and has_date:
            print(f"  ✓ 找到完整版视频: {url}")
            return url

    # 宽松匹配
    for m in vid_pattern.finditer(html):
        url = m.group(1)
        inner = m.group(2)
        if '新闻联播' in inner and ds in inner:
            print(f"  ✓ 找到视频(宽松匹配): {url}")
            return url

    # 策略4: 栏目页找不到，尝试日期页
    print(f"  [策略4] 栏目页未找到，尝试日期页...")
    day_url = DAY_PAGE_URL.format(date=ds)
    try:
        resp_day = requests.get(day_url, headers=HEADERS, timeout=30)
        resp_day.encoding = "utf-8"
        for m in vid_pattern.finditer(resp_day.text):
            url = m.group(1)
            inner = m.group(2)
            if '新闻联播' in inner and ds in inner:
                print(f"  ✓ 从日期页找到: {url}")
                return url
    except Exception:
        pass

    print(f"  ✗ 未找到 {ds} 的新闻联播视频")
    return None


# ============================================================================
# 步骤1B: 获取视频 VID（方案B: Playwright 兜底）
# ============================================================================

async def find_full_episode_vid_pw(target_date: datetime) -> str | None:
    """Playwright 浏览器兜底方案"""
    ds = date_str(target_date)
    print(f"  [方案B-Playwright] 启动浏览器兜底...")

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(user_agent=HEADERS["User-Agent"])
            page = await ctx.new_page()

            await page.goto(COLUMN_PAGE_URL, wait_until="networkidle", timeout=60000)

            video_url = await page.evaluate(
                """(ds) => {
                    const links = document.querySelectorAll('a[href*="VIDE"]');
                    for (const link of links) {
                        if (!/tv\\.cctv\\.com\\/\\d{4}\\/\\d{2}\\/\\d{2}\\/VIDE/.test(link.href))
                            continue;
                        const inner = link.innerHTML;
                        if (inner.includes('sql0') && inner.includes('新闻联播') && inner.includes(ds))
                            return link.href;
                    }
                    for (const link of links) {
                        if (!/tv\\.cctv\\.com\\/\\d{4}\\/\\d{2}\\/\\d{2}\\/VIDE/.test(link.href))
                            continue;
                        if (link.innerHTML.includes('新闻联播') && link.innerHTML.includes(ds))
                            return link.href;
                    }
                    return null;
                }""",
                ds,
            )

            await browser.close()

            if video_url:
                print(f"  ✓ [Playwright] 找到视频: {video_url}")
                return video_url

    except ImportError:
        print("  ⚠ Playwright 未安装，跳过兜底方案")
    except Exception as e:
        print(f"  ✗ Playwright 出错: {e}")

    return None


# ============================================================================
# 步骤2: 提取内部 GUID + 获取视频信息
# ============================================================================

def extract_guid_and_video_info(video_page_url: str) -> tuple[str | None, dict | None]:
    """
    从视频详情页提取内部GUID，然后调用VDN API获取完整视频信息。
    返回 (guid, video_info_dict)
    """
    print(f"  正在访问视频详情页...")
    try:
        resp = requests.get(video_page_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        html = resp.text
    except requests.RequestException as e:
        print(f"  ✗ 请求视频页失败: {e}")
        return None, None

    # 提取 var guid = "32位hex"
    m = re.search(r'var\s+guid\s*=\s*"([0-9a-fA-F]{32})"', html)
    if not m:
        print("  ✗ 未找到内部GUID")
        return None, None

    guid = m.group(1)
    print(f"  ✓ 内部GUID: {guid}")

    # 调用VDN API
    print(f"  正在获取视频流信息...")
    try:
        resp = requests.get(
            VIDEO_INFO_API, params={"pid": guid}, headers=HEADERS, timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  ✗ VDN API 失败: {e}")
        return guid, None

    if data.get("ack") != "yes":
        print(f"  ✗ API错误: {data.get('msg', '未知')}")
        return guid, None

    print(f"  ✓ 标题: {data.get('title')}")
    print(f"  ✓ 时长: {data.get('video', {}).get('totalLength', '?')}秒")
    print(f"  ✓ 分段: {len(data.get('segments', []))}条新闻")
    return guid, data


# ============================================================================
# 步骤3: 下载完整视频
# ============================================================================

def download_full_video(video_info: dict, output_path: str, quality: int,
                        prefer_chapters: bool = False) -> bool:
    """下载完整版视频，优先HLS流，备选MP4分段。"""
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    # 优先HLS
    if not prefer_chapters:
        hls_url = video_info.get("hls_url", "")
        if hls_url:
            print(f"  使用ffmpeg下载HLS流...")
            cmd = [
                "ffmpeg", "-y",
                "-user_agent", HEADERS["User-Agent"],
                "-referer", "https://tv.cctv.com/",
                "-i", hls_url,
                "-c", "copy",
                "-bsf:a", "aac_adtstoasc",
                "-movflags", "+faststart",
                output_path,
            ]
            try:
                subprocess.run(cmd, check=True)
                if os.path.exists(output_path) and os.path.getsize(output_path) > 1024 * 1024:
                    print(f"  ✓ HLS下载完成: {human_size(os.path.getsize(output_path))}")
                    return True
            except (subprocess.CalledProcessError, FileNotFoundError):
                print(f"  ⚠ HLS下载失败，切换到MP4分段模式...")

    # MP4分段下载
    qk = QUALITY_OPTIONS[quality]
    chapters = video_info.get("video", {}).get(qk, [])
    if not chapters:
        print(f"  ✗ 画质{quality}kbps无可用分段")
        return False

    print(f"  下载MP4分段（{quality}kbps, {len(chapters)}段）...")
    tmpdir = tempfile.mkdtemp(prefix="xwlb_full_")
    part_files = []

    try:
        for i, ch in enumerate(chapters):
            url = ch.get("url", "")
            if not url:
                continue
            pf = os.path.join(tmpdir, f"part_{i:04d}.mp4")
            print(f"    {i+1}/{len(chapters)} ...", end=" ", flush=True)
            try:
                r = requests.get(url, headers=HEADERS, timeout=120)
                r.raise_for_status()
                Path(pf).write_bytes(r.content)
                part_files.append(pf)
                print(f"✓ ({len(r.content)//(1024*1024)}MB)")
            except Exception as e:
                print(f"✗ ({e})")

        if not part_files:
            return False

        if len(part_files) == 1:
            os.rename(part_files[0], output_path)
        else:
            concat = os.path.join(tmpdir, "concat.txt")
            with open(concat, "w") as f:
                for pf in part_files:
                    f.write(f"file '{pf}'\n")
            subprocess.run([
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", concat, "-c", "copy", "-movflags", "+faststart", output_path,
            ], check=True)

        print(f"  ✓ MP4下载完成: {human_size(os.path.getsize(output_path))}")
        return True
    except Exception as e:
        print(f"  ✗ 下载失败: {e}")
        return False
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# ============================================================================
# 步骤4: 获取文字稿
# ============================================================================

def fetch_text_articles(target_date: datetime, segments: list[dict] = None,
                        video_page_url: str = None) -> list[dict]:
    """
    获取每条新闻的口播全文。
    1. 优先用 segment 已有的 article_url（metadata.json 中）
    2. 其次从日期页按顺序匹配分段链接
    3. 提取页面 #content_area 完整文字稿
    4. 内容<50字则跳过，交ASR兜底
    """
    ds = date_str(target_date)
    articles = []
    MIN_LEN = 50

    # 1. 从日期页获取分段链接（备用）
    day_url = DAY_PAGE_URL.format(date=ds)
    seg_urls = []
    try:
        resp = requests.get(day_url, headers=HEADERS, timeout=30)
        resp.encoding = "utf-8"
        html = resp.text
        seen = set()
        for url in re.findall(
            r'href="(https?://tv\.cctv\.com/\d{4}/\d{2}/\d{2}/VIDE[a-zA-Z0-9]+\.shtml)"',
            html):
            if url not in seen:
                seen.add(url); seg_urls.append(url)
    except requests.RequestException:
        pass

    print(f"  [文字稿] 提取 #content_area ({len(seg_urls)}个分段链接)...")

    if not segments:
        return articles

    for i, seg in enumerate(segments):
        title = seg.get("title", "")

        # 策略1: 优先用已有 article_url
        url = seg.get("article_url", "")

        # 策略2: 日期页按顺序匹配
        if not url:
            seg_idx = i + 1  # 跳过第一个完整版链接
            if seg_idx < len(seg_urls):
                url = seg_urls[seg_idx]

        if not url:
            print(f"    [{i+1}/{len(segments)}] {title[:40]}... \u2717 (无URL)")
            continue

        # 提取 #content_area
        content = ""
        try:
            r2 = requests.get(url, headers=HEADERS, timeout=15)
            r2.encoding = "utf-8"
            m = re.search(r'id="content_area"[^>]*>(.*?)</div>', r2.text, re.DOTALL)
            if not m:
                m = re.search(r'class="[^"]*cnt_bd[^"]*"[^>]*>(.*?)</div>', r2.text, re.DOTALL)
            if m:
                content = re.sub(r'<[^>]+>', ' ', m.group(1))
                content = re.sub(r'&[a-z]+;', ' ', content)
                content = re.sub(r'\s+', ' ', content).strip()
                content = re.sub(r'^央视网消息\s*（新闻联播）[：:]\s*', '', content)
        except requests.RequestException:
            pass

        if content and len(content) >= MIN_LEN:
            articles.append({"title": title, "content": content, "url": url,
                             "source": "content_area"})
            print(f"    [{i+1}/{len(segments)}] {title[:40]}... \u2713 ({len(content)}字)")
        else:
            status = f"\u2717 ({len(content)}字)" if content else "\u2717"
            print(f"    [{i+1}/{len(segments)}] {title[:40]}... {status}")
        time.sleep(0.1)

    print(f"  \u2713 口播全文: {len(articles)}/{len(segments)}条 (来源: #content_area, ≥{MIN_LEN}字)")
    return articles


def _clean_title(title: str) -> str:
    """清理标题用于比较"""
    return re.sub(r'^完整版|\[视频\]|\s+', '', title)


def _fetch_article_content(url: str) -> str:
    """抓取单篇新闻文章的正文内容（支持 news.cctv.com 和 tv.cctv.com）"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        html = resp.text
    except requests.RequestException:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # ---- news.cctv.com 文章 ----
    if "news.cctv.com" in url:
        # 移除无用标签
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()

        # news.cctv.com 正文通常在 <p> 标签中
        paragraphs = []
        for p in soup.find_all("p"):
            text = p.get_text(strip=True)
            # 过滤太短或非正文的内容（如"正在加载"、"责任编辑"等）
            if len(text) > 20 and not any(
                skip in text for skip in ["正在加载", "责任编辑", "央视网", "版权声明"]
            ):
                paragraphs.append(text)

        if paragraphs:
            content = "\n\n".join(paragraphs)
            return content

        # fallback: 取最长文本的div
        best = ""
        for div in soup.find_all("div"):
            t = div.get_text(strip=True)
            if len(t) > len(best):
                best = t
        if len(best) > 100:
            return best

    # ---- tv.cctv.com 视频页 ----
    else:
        # 视频简介
        for sel in [
            {"class": "video_brief"},
            {"class": "brief"},
            {"class": "cnt_bd"},
        ]:
            elem = soup.find("div", sel)
            if elem:
                text = elem.get_text(strip=True)
                if len(text) > 15:
                    return text

        # meta description
        meta = soup.find("meta", attrs={"name": "description"})
        if meta and meta.get("content"):
            desc = meta["content"].strip()
            if len(desc) > 15:
                return desc

    return ""


# ============================================================================
# 步骤5: 视频切片
# ============================================================================

def slice_video(video_path: str, segments: list[dict], output_dir: str,
                trim_start: float = 0.7, trim_end: float = 0.7) -> list[dict]:
    """
    使用ffmpeg按时间戳帧精确切割视频，前后各裁剪trim秒避免转场残留。

    关键：-ss 放在 -i 之后（输出级seek），实现帧精确切割。
          -ss 放在 -i 之前（输入级seek）会跳到最近关键帧，
          导致切片开头带上一段新闻的残留帧。

    参数:
      video_path: 完整视频文件路径
      segments: VDN API返回的segments数组
      output_dir: 输出目录
      trim_start: 开头裁剪秒数（默认0.7s，消除首帧跳动和上一段残留）
      trim_end:   结尾裁剪秒数（默认0.7s，避免下一段新闻开头渗入）

    返回: 切片结果列表
    """
    os.makedirs(output_dir, exist_ok=True)
    results = []

    print(f"  切割视频（共{len(segments)}段，前后各裁剪{trim_start}s/{trim_end}s，帧精确模式）...")

    for i, seg in enumerate(segments):
        title = seg.get("title", f"segment_{i}")
        start_ms = seg.get("start", 0)
        end_ms = seg.get("end", 0)

        # 转换为秒，加上裁剪偏移
        start_sec = start_ms / 1000.0 + trim_start
        end_sec = end_ms / 1000.0 - trim_end
        duration_sec = end_sec - start_sec

        if duration_sec <= 0:
            # 如果裁剪后时长不够，使用原始时间
            start_sec = start_ms / 1000.0
            end_sec = end_ms / 1000.0
            duration_sec = end_sec - start_sec
            if duration_sec <= 0:
                continue

        idx = i + 1
        safe_title = safe_filename(title)
        fname = f"{idx:02d}_{safe_title}.mp4"
        outpath = os.path.join(output_dir, fname)

        print(f"    [{idx}/{len(segments)}] {title[:50]}... ({duration_sec:.0f}s)",
              end=" ", flush=True)

        # 帧精确切割方案：
        # -c copy 模式下，无论 -ss 放哪都会对齐到关键帧（I-frame），
        # 导致切片开头包含上一段新闻的残留帧（首帧跳动）。
        # 解决：使用重编码模式，-ss 放 -i 后 = 输出级seek，逐帧精确定位。
        # 性能：ultrafast preset + 720p，编码速度 8-15x 实时，秒级完成。
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(max(0, start_sec - 1.0)),  # 输入级粗跳（快速跳过前文）
            "-i", video_path,
            "-ss", str(min(start_sec, 1.0)),       # 输出级精跳（帧精确跳过残留）
            "-t", str(duration_sec),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            "-avoid_negative_ts", "make_zero",
            outpath,
        ]

        try:
            subprocess.run(cmd, check=True, capture_output=True)
            if os.path.exists(outpath) and os.path.getsize(outpath) > 1024:
                results.append({
                    "index": idx,
                    "title": title,
                    "path": outpath,
                    "start": start_ms,
                    "end": end_ms,
                    "duration": duration_sec,
                })
                print("✓")
            else:
                print("✗ (空文件)")
        except subprocess.CalledProcessError as e:
            # 降级：用 -c copy 碰运气（用户可接受时有概率成功）
            print("⚠ (退到copy模式)", end=" ", flush=True)
            cmd_copy = [
                "ffmpeg", "-y",
                "-ss", str(start_sec),
                "-i", video_path,
                "-t", str(duration_sec),
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                outpath,
            ]
            try:
                subprocess.run(cmd_copy, check=True, capture_output=True)
                if os.path.exists(outpath) and os.path.getsize(outpath) > 1024:
                    results.append({
                        "index": idx, "title": title, "path": outpath,
                        "start": start_ms, "end": end_ms,
                        "duration": duration_sec,
                    })
                    print("✓")
                else:
                    print("✗")
            except subprocess.CalledProcessError:
                print("✗")

    print(f"  ✓ 成功切割 {len(results)}/{len(segments)} 段")
    return results


# ============================================================================
# 步骤5B: 语音识别（whisper）- 当文字稿不可用时的兜底方案
# ============================================================================

def transcribe_segments(video_path: str, segments: list[dict],
                         output_dir: str, target_date: datetime) -> list[dict]:
    """
    使用 whisper 对视频片段进行语音识别，生成口播稿件。

    返回: [{"title": "...", "content": "...", "url": "", "source": "asr"}, ...]
    """
    # 优先使用 Python faster-whisper 库
    try:
        from faster_whisper import WhisperModel
        return _transcribe_with_faster_whisper(
            video_path, segments, output_dir, target_date
        )
    except ImportError:
        pass


    # 最后尝试命令行
    import shutil
    asr_tool = shutil.which("whisper") or shutil.which("faster-whisper")
    if asr_tool:
        return _transcribe_with_cli_whisper(video_path, segments, output_dir, target_date)

    print("  ⚠ 未安装whisper（pip install faster-whisper），跳过语音识别")
    return []


def _transcribe_with_faster_whisper(video_path, segments, output_dir, target_date):
    """使用faster-whisper进行语音识别（并行处理 + 简体中文转换）"""
    from faster_whisper import WhisperModel
    from concurrent.futures import ThreadPoolExecutor, as_completed

    print("    使用 faster-whisper 进行语音识别（并行模式）...")
    model = WhisperModel("small", device="cpu", compute_type="int8")
    tmpdir = tempfile.mkdtemp(prefix="xwlb_asr_")
    results = []

    def _to_simplified(text: str) -> str:
        """繁体转简体（简单映射）"""
        try:
            import opencc
            return opencc.OpenCC('t2s').convert(text)
        except ImportError:
            pass
        # 内置常用繁简映射
        tc_map = str.maketrans(
            '國權黨軍張趙劉孫馬鄭黃吳林陳楊周何郭梁宋謝韓唐馮於董程曹袁鄧許傅沈曾彭呂蘇盧蔣蔡賈丁魏薛葉閻餘潘杜戴夏鐘汪田任薑范方石姚譚廖鄒熊金陸郝孔白崔康毛邱秦江史顧侯邵孟龍萬段雷錢湯尹黎易常武喬賀賴龔文',
            '国权党军张赵刘孙马郑黄吴林陈杨周何郭梁宋谢韩唐冯于董程曹袁邓许傅沈曾彭吕苏卢蒋蔡贾丁魏薛叶阎余潘杜戴夏钟汪田任姜范方石姚谭廖邹熊金陆郝孔白崔康毛邱秦江史顾侯邵孟龙万段雷钱汤尹黎易常武乔贺赖龚文'
        )
        return text.translate(tc_map)

    def _transcribe_one(seg_info):
        """单个片段的转录任务"""
        i, seg = seg_info
        title = seg.get("title", f"segment_{i}")
        start_s = seg.get("start", 0) / 1000.0
        end_s = seg.get("end", 0) / 1000.0
        dur_s = end_s - start_s

        if dur_s <= 5:
            return None

        idx = i + 1
        audio_path = os.path.join(tmpdir, f"seg_{idx:02d}.wav")
        extract_cmd = [
            "ffmpeg", "-y", "-ss", str(start_s), "-i", video_path,
            "-t", str(dur_s), "-vn", "-acodec", "pcm_s16le",
            "-ar", "16000", "-ac", "1", audio_path,
        ]
        subprocess.run(extract_cmd, check=True, capture_output=True)

        try:
            seg_texts, _ = model.transcribe(audio_path, language="zh",
                                             beam_size=5, vad_filter=True)
            full_text = " ".join(s.text for s in seg_texts)
            full_text = _to_simplified(full_text)  # 转简体
            if full_text and len(full_text) > 10:
                return {"title": title, "content": full_text, "url": "", "source": "asr"}
        except Exception as e:
            print(f"    ✗ [{idx}] {title[:30]}: {e}")
        return None

    # 并行执行（最多4个并发）
    tasks = [(i, seg) for i, seg in enumerate(segments)]
    completed = 0
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_transcribe_one, t): t for t in tasks}
        for future in as_completed(futures):
            completed += 1
            result = future.result()
            seg_i = futures[future][0]
            if result:
                results.append(result)
                print(f"    [{completed}/{len(tasks)}] {result['title'][:30]}... ✓ ({len(result['content'])}字)")
            else:
                print(f"    [{completed}/{len(tasks)}] ✗")

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"    ASR完成: {len(results)}/{len(segments)} 段")
    return results


def _transcribe_with_cli_whisper(video_path, segments, output_dir, target_date):
    """使用命令行whisper进行语音识别"""
    print("    使用命令行whisper进行语音识别...")
    tmpdir = tempfile.mkdtemp(prefix="xwlb_asr_")
    results = []

    try:
        for i, seg in enumerate(segments):
            title = seg.get("title", f"segment_{i}")
            start_s = seg.get("start", 0) / 1000.0
            end_s = seg.get("end", 0) / 1000.0
            dur_s = end_s - start_s

            if dur_s <= 5:
                continue

            idx = i + 1
            print(f"    [{idx}/{len(segments)}] ASR: {title[:40]}...", end=" ", flush=True)

            audio_path = os.path.join(tmpdir, f"seg_{idx:02d}.wav")
            extract_cmd = [
                "ffmpeg", "-y", "-ss", str(start_s), "-i", video_path,
                "-t", str(dur_s), "-vn", "-acodec", "pcm_s16le",
                "-ar", "16000", "-ac", "1", audio_path,
            ]
            subprocess.run(extract_cmd, check=True, capture_output=True)

            try:
                result = subprocess.run(
                    ["whisper", audio_path, "--language", "zh",
                     "--model", "small", "--output_format", "txt",
                     "--output_dir", tmpdir],
                    check=True, capture_output=True, text=True,
                )
                txt_path = audio_path.replace(".wav", ".txt")
                if os.path.exists(txt_path):
                    with open(txt_path, "r") as f:
                        text = f.read().strip()
                    if text and len(text) > 10:
                        results.append({
                            "title": title, "content": text,
                            "url": "", "source": "asr",
                        })
                        print(f"✓ ({len(text)}字)")
                    else:
                        print("✗")
                else:
                    print("✗")
            except Exception as e:
                print(f"✗ ({e})")

    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"    ASR完成: {len(results)}/{len(segments)} 段")
    return results


# ============================================================================
# 步骤6: 生成文字稿和口播稿件
# ============================================================================

def generate_manuscripts(segments: list[dict], articles: list[dict],
                          output_dir: str, target_date: datetime,
                          slice_results: list[dict] = None) -> str:
    """
    生成文字稿文件（与metadata.json同步）：
      - manuscripts/ 目录下单条口播稿件
      - daily_manuscript.md 当日汇总

    返回: daily_manuscript.md 的内容
    """
    manuscripts_dir = os.path.join(output_dir, "manuscripts")
    os.makedirs(manuscripts_dir, exist_ok=True)

    # 构建slice路径映射（从metadata同步）
    slice_map = {}
    if slice_results:
        for sr in slice_results:
            slice_map[sr.get("title", "")] = sr.get("path", "")

    # 将文章标题与segment匹配
    def match_article(seg_title: str) -> dict | None:
        clean_seg = re.sub(r'[［［\]］\s【】\[\]]', '', seg_title)
        clean_seg = clean_seg.replace('[视频]', '').replace('（', '').replace('）', '')
        for art in articles:
            clean_art = re.sub(r'[［［\]］\s【】\[\]]', '', art["title"])
            clean_art = clean_art.replace('[视频]', '')
            if clean_seg == clean_art: return art
            if len(clean_seg) > 6 and clean_seg[:6] in clean_art: return art
            if len(clean_art) > 6 and clean_art[:6] in clean_seg: return art
        return None

    md_lines = [
        f"# 《新闻联播》{target_date.strftime('%Y年%m月%d日')} 文字稿",
        "",
        f"**日期**: {target_date.strftime('%Y-%m-%d')}",
        f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**新闻条数**: {len(segments)}",
        "",
        "---",
        "",
    ]

    for i, seg in enumerate(segments):
        idx = i + 1
        title = seg.get("title", f"未知标题")
        duration = (seg.get("end", 0) - seg.get("start", 0)) / 1000.0

        md_lines.append(f"## {idx}. {title}")
        md_lines.append(f"")

        # 从slice_map获取视频切片路径
        slice_path = slice_map.get(title, "")
        if slice_path:
            rel_path = os.path.relpath(slice_path, output_dir)
            md_lines.append(f"📹 **切片**: [{rel_path}]({rel_path}) | *时长: {duration:.0f} 秒*")
        else:
            md_lines.append(f"*时长: {duration:.0f} 秒*")
        md_lines.append(f"")
        md_lines.append(f"")

        # 匹配文字稿
        article = match_article(title)
        content = article["content"] if article else ""

        if content:
            source_note = ""
            if article and article.get("source") == "asr":
                source_note = " *(语音识别)*"
            elif article and article.get("url", "").startswith("https://news.cctv.com"):
                source_note = " *(来源: 央视网)*"
            md_lines.append(content)
            if source_note:
                md_lines.append(f"")
                md_lines.append(source_note)
        else:
            md_lines.append(f"> ⚠ 未找到对应文字稿（可安装whisper启用语音识别）")
            md_lines.append(f"")

        md_lines.append(f"---")
        md_lines.append(f"")

        # 写入单条口播稿件（已有更长内容则保留，不覆盖）
        safe_t = safe_filename(title).replace('[视频]','')
        manu_filename = f"{idx:02d}_[视频]{safe_t}.txt"
        manu_path = os.path.join(manuscripts_dir, manu_filename)
        existing_len = 0
        if os.path.exists(manu_path):
            existing_len = os.path.getsize(manu_path)
        new_content = content if content else "（未获取到对应文字稿，请参考视频内容或使用语音识别工具提取）\n"
        if len(new_content) < existing_len:
            continue  # 已有更完整内容，跳过
        with open(manu_path, "w", encoding="utf-8") as f:
            f.write(f"# {title}\n")
            if article and article.get("source") == "asr":
                f.write(f"# 日期: {target_date.strftime('%Y-%m-%d')}\n")
                f.write(f"# 来源: 语音识别 (faster-whisper)\n")
                f.write(f"# 时长: {duration:.0f}秒\n\n")
            else:
                f.write(f"# 日期: {target_date.strftime('%Y-%m-%d')}\n")
                f.write(f"# 时长: {duration:.0f}秒\n\n")
            f.write(new_content)

    md_content = "\n".join(md_lines)
    md_path = os.path.join(output_dir, "daily_manuscript.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    return md_content


# ============================================================================
# 步骤7: 生成元数据
# ============================================================================

def generate_readme(output_dir: str, target_date: datetime,
                    video_info: dict, segments: list[dict],
                    articles: list[dict], slice_results: list[dict],
                    full_video_path: str):
    """
    生成汇总 README.md：包含完整文字稿、每条新闻的切片路径、视频信息。
    这是主要的输出文档。
    """
    ds = date_str(target_date)
    date_display = target_date.strftime('%Y年%m月%d日')

    lines = [
        f"# 《新闻联播》{date_display} — 采集结果",
        "",
        f"**日期**: {target_date.strftime('%Y-%m-%d')}",
        f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**新闻条数**: {len(segments)}",
        "",
        "---",
        "",
        "## 📁 文件索引",
        "",
    ]

    # 完整视频
    if os.path.exists(full_video_path):
        size = human_size(os.path.getsize(full_video_path))
        lines.append(f"- **完整视频**: [`full.mp4`](full.mp4) ({size})")
    else:
        lines.append(f"- **完整视频**: 未下载")
    lines.append(f"- **视频切片**: [`segments/`](segments/) ({len(slice_results)} 段)")
    lines.append(f"- **口播稿件**: [`manuscripts/`](manuscripts/)")
    lines.append(f"- **元数据**: [`metadata.json`](metadata.json)")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 每条新闻详情
    for i, seg in enumerate(segments):
        idx = i + 1
        title = seg.get("title", f"未知标题")
        duration = (seg.get("end", 0) - seg.get("start", 0)) / 1000.0

        lines.append(f"## {idx}. {title}")
        lines.append("")

        # 切片文件路径
        slice_found = None
        for sr in slice_results:
            if sr["title"] == title:
                slice_found = sr
                break
        if slice_found:
            seg_rel_path = os.path.relpath(slice_found["path"], output_dir)
            seg_size = human_size(os.path.getsize(slice_found["path"]))
            lines.append(f"📹 **切片视频**: [`{seg_rel_path}`]({seg_rel_path}) ({seg_size}, {duration:.0f}秒)")
        else:
            lines.append(f"📹 **切片视频**: 未生成 ({duration:.0f}秒)")
        lines.append("")

        # 文字稿/口播稿件
        article = None
        for a in articles:
            if _titles_match(title, a["title"]):
                article = a
                break

        # 口播稿件文件路径（统一[视频]前缀命名）
        safe_t = safe_filename(title).replace('[视频]','')
        manu_rel = f"manuscripts/{idx:02d}_[视频]{safe_t}.txt"
        if os.path.exists(os.path.join(output_dir, manu_rel)):
            lines.append(f"📝 **口播稿件**: [`{manu_rel}`]({manu_rel})")

        if article and article.get("content"):
            source_tag = ""
            if article.get("source") == "asr":
                source_tag = " *(🤖 语音识别)*"
            elif article.get("url", "").startswith("https://news.cctv.com"):
                source_tag = " *(📰 央视网)*"
            elif article.get("url", "").startswith("https://tv.cctv.com"):
                source_tag = " *(📺 视频简介)*"
            lines.append(f"{source_tag}")
            lines.append("")
            lines.append(article["content"])
        else:
            lines.append("")
            lines.append("> ⚠️ 未获取到文字稿。安装 faster-whisper 可启用语音识别：")
            lines.append("> `pip install faster-whisper`")
        lines.append("")
        lines.append("---")
        lines.append("")

    readme_path = os.path.join(output_dir, "README.md")
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return readme_path


def save_metadata(output_dir: str, target_date: datetime,
                  video_info: dict, segments: list[dict],
                  articles: list[dict], slice_results: list[dict],
                  download_log: str):
    """保存结构化元数据JSON"""
    meta = {
        "date": date_str(target_date),
        "title": video_info.get("title", ""),
        "channel": video_info.get("play_channel", ""),
        "duration_seconds": video_info.get("video", {}).get("totalLength", 0),
        "total_segments": len(segments),
        "segments": [],
        "files": {
            "full_video": os.path.join(output_dir, "full.mp4"),
            "daily_manuscript": os.path.join(output_dir, "daily_manuscript.md"),
            "segments_dir": os.path.join(output_dir, "segments"),
            "manuscripts_dir": os.path.join(output_dir, "manuscripts"),
        },
        "download_log": download_log,
    }

    for seg in segments:
        entry = {
            "title": seg.get("title", ""),
            "start_ms": seg.get("start", 0),
            "end_ms": seg.get("end", 0),
            "duration_seconds": (seg.get("end", 0) - seg.get("start", 0)) / 1000.0,
        }

        # 查找匹配的切片文件
        for sr in slice_results:
            if sr["title"] == seg.get("title"):
                entry["video_slice"] = sr["path"]
                break

        # 查找匹配的文字稿
        entry["has_manuscript"] = False
        for art in articles:
            if _titles_match(seg.get("title", ""), art["title"]):
                entry["has_manuscript"] = True
                entry["article_url"] = art["url"]
                entry["article_word_count"] = len(art["content"])
                break

        meta["segments"].append(entry)

    meta_path = os.path.join(output_dir, "metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def _titles_match(t1: str, t2: str) -> bool:
    """判断两个标题是否匹配"""
    c1 = re.sub(r'[［［\]］\s【】\[\]\(\)（）]', '', t1.replace('[视频]', ''))
    c2 = re.sub(r'[［［\]］\s【】\[\]\(\)（）]', '', t2.replace('[视频]', ''))
    if c1 == c2:
        return True
    if len(c1) > 8 and c1[:8] in c2:
        return True
    if len(c2) > 8 and c2[:8] in c1:
        return True
    return False


# ============================================================================
# 主流程
# ============================================================================

async def main_async(args):
    # ---- 解析参数 ----
    target_date = parse_date(args.date) if args.date else datetime.now() - timedelta(days=1)
    ds = date_str(target_date)
    output_dir = args.output or f"{ds}_news"
    log_lines = []

    print("=" * 60)
    print(f"  央视《新闻联播》采集工具")
    print(f"  目标日期: {target_date.strftime('%Y-%m-%d')} ({ds})")
    print(f"  输出目录: {output_dir}")
    print(f"  画质: {args.quality} kbps")
    print("=" * 60)
    print()

    os.makedirs(output_dir, exist_ok=True)

    # ======== 阶段1: 获取视频VID ========
    print("▶ 阶段1: 获取视频信息")
    video_url = find_full_episode_vid(target_date)

    if not video_url and not args.no_video:
        print("  ⚠ 方案A失败，启动方案B(Playwright)兜底...")
        video_url = await find_full_episode_vid_pw(target_date)

    if not video_url:
        if args.no_video:
            print("  ℹ --no-video 模式，跳过视频获取")
            video_info = None
            guid = None
        else:
            print("❌ 无法找到视频。可能该日期无播出或尚未上线。")
            sys.exit(1)
    else:
        # ======== 阶段2: 获取视频信息(GUID+API) ========
        guid, video_info = extract_guid_and_video_info(video_url)
        if not guid or not video_info:
            print("❌ 无法获取视频信息")
            sys.exit(1)

        log_lines.append(f"VID: {video_url.split('/')[-1].replace('.shtml','')}")
        log_lines.append(f"GUID: {guid}")
        log_lines.append(f"标题: {video_info.get('title', '')}")
        log_lines.append(f"时长: {video_info.get('video', {}).get('totalLength', '?')}秒")

    # ======== 阶段3: 下载完整视频 ========
    full_video_path = os.path.join(output_dir, "full.mp4")

    if not args.no_video and video_info:
        print()
        print("▶ 阶段2: 下载完整视频")
        if os.path.exists(full_video_path) and os.path.getsize(full_video_path) > 1024 * 1024:
            print(f"  ℹ 完整视频已存在，跳过下载: {full_video_path}")
        else:
            ok = download_full_video(
                video_info, full_video_path,
                args.quality, args.prefer_chapters,
            )
            if not ok:
                print("❌ 视频下载失败")
                sys.exit(1)
            log_lines.append(f"视频文件: {full_video_path} ({human_size(os.path.getsize(full_video_path))})")
    elif args.no_video:
        print("  ℹ --no-video 模式，跳过视频下载")

    # 初始化segments（后续阶段共用）
    segments = video_info.get("segments", []) if video_info else []

    # ======== 阶段3: 获取文字稿 ========
    articles = []
    if not args.no_text:
        print()
        print("▶ 阶段3: 获取文字稿")
        articles = fetch_text_articles(target_date, segments, video_url)
        log_lines.append(f"文字稿: {len(articles)}条")
        if articles:
            total_chars = sum(len(a["content"]) for a in articles)
            log_lines.append(f"总字数: {total_chars}")

        # 对于未找到文字稿的segment，使用whisper语音识别兜底
        matched_titles = {_clean_title(a["title"]) for a in articles}
        missing_segs = [
            s for s in segments
            if _clean_title(s.get("title", "")) not in matched_titles
        ]
        # ASR默认开启（除非显式--no-asr）
        if missing_segs and os.path.exists(full_video_path) and not args.no_asr:
            print(f"\n  [ASR兜底] {len(missing_segs)}条新闻未达字数标准(每分钟≥25字)，启动语音识别...")
            print(f"    缺失列表: {[s.get('title','')[:40] for s in missing_segs]}")
            asr_results = transcribe_segments(
                full_video_path, missing_segs, output_dir, target_date
            )
            for r in asr_results:
                articles.append(r)
        elif missing_segs:
            print(f"\n  ⚠ {len(missing_segs)}条新闻无有效文字稿（ASR被跳过或视频未下载）")
            for s in missing_segs:
                print(f"    - {s.get('title','')[:50]}")
    else:
        print("  ℹ --no-text 模式，跳过文字稿获取")

    # ======== 阶段4: 视频切片 ========
    slice_results = []

    if not args.keep_full_only and not args.no_video and segments and video_info:
        print()
        print("▶ 阶段4: 视频切片")
        seg_dir = os.path.join(output_dir, "segments")
        slice_results = slice_video(full_video_path, segments, seg_dir)
        log_lines.append(f"切片: {len(slice_results)}/{len(segments)} 段成功")
    elif args.keep_full_only:
        print("  ℹ --keep-full-only 模式，跳过视频切片")

    # ======== 阶段5: 生成README汇总 + 口播稿件 ========
    if not args.no_text and segments:
        print()
        print("▶ 阶段5: 生成汇总文档和口播稿件")
        # 生成逐条口播稿件
        generate_manuscripts(segments, articles, output_dir, target_date, slice_results)

        # 清理：只保留标准命名格式 {idx:02d}_[视频]{title}.txt
        manu_dir = os.path.join(output_dir, "manuscripts")
        if os.path.exists(manu_dir):
            for f in sorted(os.listdir(manu_dir)):
                if not f.endswith(".txt"): continue
                prefix = f[:2]
                if prefix.isdigit() and "[视频]" not in f:
                    os.remove(os.path.join(manu_dir, f))
                    print(f"    🧹 清理非标准格式: {f}")

        log_lines.append(f"口播稿件: manuscripts/ 目录")
        # 生成README汇总（含文字稿+切片路径）
        generate_readme(output_dir, target_date, video_info,
                        segments, articles, slice_results, full_video_path)
        log_lines.append(f"汇总文档: README.md")

    # ======== 阶段6: 保存元数据 ========
    if video_info:
        save_metadata(output_dir, target_date, video_info, segments,
                      articles, slice_results, "\n".join(log_lines))

    # ======== 完成 ========
    print()
    print("=" * 60)
    print("  ✅ 采集完成！")
    print("=" * 60)
    print(f"  输出目录: {os.path.abspath(output_dir)}")
    print(f"  汇总文档: README.md")

    def show_file(label, path):
        full = os.path.join(output_dir, path)
        if os.path.exists(full):
            size = human_size(os.path.getsize(full)) if os.path.isfile(full) else "目录"
            print(f"    {label}: {path} ({size})")
        else:
            print(f"    {label}: {path} (未生成)")

    show_file("汇总文档  ", "README.md")
    show_file("完整视频  ", "full.mp4")
    show_file("切片段落  ", "segments/")
    show_file("口播稿件  ", "manuscripts/")

    if articles:
        matched = sum(1 for seg in segments if any(
            _titles_match(seg.get("title", ""), a["title"]) for a in articles
        ))
        print(f"  文字稿匹配: {matched}/{len(segments)} 条")

    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="央视《新闻联播》采集工具 - 视频下载 + 新闻切片 + 文字稿提取",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s                                  # 全功能：下载+切片+文字
  %(prog)s 2026-06-20                       # 指定日期
  %(prog)s --quality 2000                   # 超清画质
  %(prog)s --no-video                       # 仅文字稿，不下载视频
  %(prog)s --no-text                        # 仅视频，不获取文字
  %(prog)s -o ./output_dir                  # 指定输出目录
        """,
    )
    parser.add_argument("date", nargs="?", default=None, help="目标日期 YYYY-MM-DD 或 YYYYMMDD")
    parser.add_argument("-q", "--quality", type=int, default=1200,
                        choices=list(QUALITY_OPTIONS.keys()), help="视频码率(kbps)")
    parser.add_argument("-o", "--output", type=str, default=None, help="输出目录")
    parser.add_argument("--prefer-chapters", action="store_true", help="优先MP4分段下载")
    parser.add_argument("--no-video", action="store_true", help="跳过视频下载")
    parser.add_argument("--no-text", action="store_true", help="跳过文字稿获取")
    parser.add_argument("--keep-full-only", action="store_true",
                        help="只保留完整视频，不切片")
    parser.add_argument("--no-asr", action="store_true",
                        help="跳过语音识别（即使未找到文字稿）")
    args = parser.parse_args()

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
