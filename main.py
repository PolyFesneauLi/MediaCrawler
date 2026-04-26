# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/main.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

import sys
import io

# Force UTF-8 encoding for stdout/stderr to prevent encoding errors
# when outputting Chinese characters in non-UTF-8 terminals
if sys.stdout and hasattr(sys.stdout, 'buffer'):
    if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr and hasattr(sys.stderr, 'buffer'):
    if sys.stderr.encoding and sys.stderr.encoding.lower() != 'utf-8':
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional, Type

import cmd_arg
import config
from database import db
from base.base_crawler import AbstractCrawler
from media_platform.bilibili import BilibiliCrawler
from media_platform.douyin import DouYinCrawler
from media_platform.kuaishou import KuaishouCrawler
from media_platform.tieba import TieBaCrawler
from media_platform.weibo import WeiboCrawler
from media_platform.xhs import XiaoHongShuCrawler
from media_platform.zhihu import ZhihuCrawler
from tools.search_pipeline import run_search_pipeline
from tools.async_file_writer import AsyncFileWriter
from var import crawler_type_var


class CrawlerFactory:
    CRAWLERS: dict[str, Type[AbstractCrawler]] = {
        "xhs": XiaoHongShuCrawler,
        "dy": DouYinCrawler,
        "ks": KuaishouCrawler,
        "bili": BilibiliCrawler,
        "wb": WeiboCrawler,
        "tieba": TieBaCrawler,
        "zhihu": ZhihuCrawler,
    }

    @staticmethod
    def create_crawler(platform: str) -> AbstractCrawler:
        crawler_class = CrawlerFactory.CRAWLERS.get(platform)
        if not crawler_class:
            supported = ", ".join(sorted(CrawlerFactory.CRAWLERS))
            raise ValueError(f"Invalid media platform: {platform!r}. Supported: {supported}")
        return crawler_class()


crawler: Optional[AbstractCrawler] = None
PLATFORM_DATA_ALIASES: dict[str, str] = {
    "dy": "douyin",
    "ks": "kuaishou",
    "wb": "weibo",
}


def _collect_search_source_files(platform: str) -> set[Path]:
    data_root = Path(config.SAVE_DATA_PATH) if config.SAVE_DATA_PATH else Path("data")
    normalized_platform = str(platform or "").strip().lower()
    candidate_names = [normalized_platform]
    alias = PLATFORM_DATA_ALIASES.get(normalized_platform)
    if alias and alias not in candidate_names:
        candidate_names.append(alias)

    for short_name, long_name in PLATFORM_DATA_ALIASES.items():
        if normalized_platform == long_name and short_name not in candidate_names:
            candidate_names.append(short_name)

    source_files: set[Path] = set()
    for candidate in candidate_names:
        platform_root = data_root / candidate
        if not platform_root.exists() or not platform_root.is_dir():
            continue
        for pattern in ("jsonl/search_*", "json/search_*"):
            for path in platform_root.glob(pattern):
                if path.is_file() and path.suffix.lower() in {".jsonl", ".json"}:
                    source_files.add(path.resolve())
    return source_files


def _collect_search_source_snapshots(platform: str) -> dict[Path, tuple[int, int]]:
    snapshots: dict[Path, tuple[int, int]] = {}
    for path in _collect_search_source_files(platform):
        stat = path.stat()
        # Use nanosecond mtime + size to detect append/overwrite changes reliably.
        snapshots[path] = (int(stat.st_mtime_ns), int(stat.st_size))
    return snapshots


def _flush_excel_if_needed() -> None:
    if config.SAVE_DATA_OPTION != "excel":
        return

    try:
        from store.excel_store_base import ExcelStoreBase

        ExcelStoreBase.flush_all()
        print("[Main] Excel files saved successfully")
    except Exception as e:
        print(f"[Main] Error flushing Excel data: {e}")


async def _generate_wordcloud_if_needed() -> None:
    if config.SAVE_DATA_OPTION not in ("json", "jsonl") or not config.ENABLE_GET_WORDCLOUD:
        return

    try:
        file_writer = AsyncFileWriter(
            platform=config.PLATFORM,
            crawler_type=crawler_type_var.get(),
        )
        await file_writer.generate_wordcloud_from_comments()
    except Exception as e:
        print(f"[Main] Error generating wordcloud: {e}")


async def _cleanup_crawler_only() -> None:
    global crawler
    if not crawler:
        return

    if getattr(crawler, "cdp_manager", None):
        try:
            await crawler.cdp_manager.cleanup(force=True)
        except Exception as e:
            error_msg = str(e).lower()
            if "closed" not in error_msg and "disconnected" not in error_msg:
                print(f"[Main] Error cleaning up CDP browser: {e}")
    elif getattr(crawler, "browser_context", None):
        try:
            await crawler.browser_context.close()
        except Exception as e:
            error_msg = str(e).lower()
            if "closed" not in error_msg and "disconnected" not in error_msg:
                print(f"[Main] Error closing browser context: {e}")

    crawler = None


async def main() -> None:
    global crawler

    args = await cmd_arg.parse_cmd()
    if args.init_db:
        await db.init_db(args.init_db)
        print(f"Database {args.init_db} initialized successfully.")
        return

    keyword_lines = getattr(config, "KEYWORD_LINES", [])
    run_each_keyword = (
        config.CRAWLER_TYPE == "search"
        and bool(keyword_lines)
    )

    if run_each_keyword:
        batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        config.SEARCH_BATCH_ID = batch_id
        for keyword in keyword_lines:
            source_snapshots_before = _collect_search_source_snapshots(config.PLATFORM)
            config.KEYWORDS = keyword
            print(f"[Main] Running {config.PLATFORM} search for keyword: {keyword}")
            crawler = CrawlerFactory.create_crawler(platform=config.PLATFORM)
            await crawler.start()
            source_snapshots_after = _collect_search_source_snapshots(config.PLATFORM)
            changed_source_files = sorted(
                path
                for path, snapshot in source_snapshots_after.items()
                if source_snapshots_before.get(path) != snapshot
            )
            source_offsets = {
                path: source_snapshots_before.get(path, (0, 0))[1]
                for path in changed_source_files
            }
            run_search_pipeline(
                platform=config.PLATFORM,
                source_files=changed_source_files,
                batch_id=batch_id,
                source_offsets=source_offsets,
                allowed_keywords=[keyword],
            )
            await _cleanup_crawler_only()
    else:
        crawler = CrawlerFactory.create_crawler(platform=config.PLATFORM)
        await crawler.start()
        if config.CRAWLER_TYPE == "search":
            run_search_pipeline(platform=config.PLATFORM)

    _flush_excel_if_needed()

    # Generate wordcloud after crawling is complete
    # Only for JSON save mode
    await _generate_wordcloud_if_needed()


async def async_cleanup() -> None:
    await _cleanup_crawler_only()

    if config.SAVE_DATA_OPTION in ("db", "sqlite"):
        await db.close()

if __name__ == "__main__":
    from tools.app_runner import run

    def _force_stop() -> None:
        c = crawler
        if not c:
            return
        cdp_manager = getattr(c, "cdp_manager", None)
        launcher = getattr(cdp_manager, "launcher", None)
        if not launcher:
            return
        try:
            launcher.cleanup()
        except Exception:
            pass

    run(main, async_cleanup, cleanup_timeout_seconds=15.0, on_first_interrupt=_force_stop)
