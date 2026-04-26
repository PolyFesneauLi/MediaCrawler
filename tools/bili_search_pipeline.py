# -*- coding: utf-8 -*-
"""Bilibili search post-processing pipeline.

This module keeps the crawler unchanged and turns the raw Bilibili search
JSON/JSONL exports into a staged dataset under ``data/bili``:

- 阶段1_原始抓取
- 阶段2_清洗标准化
- 阶段3_筛选结果
- 阶段4_分析汇总

The pipeline is intentionally rule-based and lightweight so it can run right
after the crawl finishes without adding new dependencies.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
from datetime import datetime
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence

import config
from tools import utils


RAW_STAGE_DIR = "阶段1_原始抓取"
CLEAN_STAGE_DIR = "阶段2_清洗标准化"
FILTER_STAGE_DIR = "阶段3_筛选结果"
ANALYSIS_STAGE_DIR = "阶段4_分析汇总"

TEACHER_TERMS = {
    "教师",
    "老师",
    "班主任",
    "教研",
    "备课",
    "批改",
    "学情",
    "家长",
    "课堂",
    "作业",
    "教学",
    "中学",
    "初中",
    "高中",
}

PAIN_TERMS = {
    "累",
    "烦",
    "难",
    "没效率",
    "浪费时间",
    "崩溃",
    "加班",
    "焦虑",
    "耗时",
    "低效",
    "形式主义",
    "压力",
    "折腾",
    "手酸",
    "头大",
}

REQUEST_TERMS = {
    "求推荐",
    "有没有",
    "推荐",
    "想要",
    "如果有",
    "希望",
    "AI能",
    "能不能",
    "怎么快速",
    "工具",
    "软件",
    "系统",
    "帮忙",
}

WUHAN_TERMS = {
    "武汉",
    "江岸",
    "武昌",
    "汉阳",
    "汉口",
    "洪山",
    "光谷",
    "蔡甸",
    "青山",
    "东西湖",
    "黄陂",
    "新洲",
    "武汉中学",
    "武汉一中",
}

AD_TERMS = {
    "下载地址",
    "私信",
    "加微信",
    "加vx",
    "vx",
    "广告",
    "推广",
    "合作",
    "领取",
    "二维码",
    "客服",
    "咨询",
    "链接",
}

CATEGORY_RULES = {
    "备课耗时": ["备课", "教案", "课件", "找资料", "找资源", "PPT", "素材", "备课神器", "公开课"],
    "作业批改量大": ["批改", "作业", "作文", "试卷", "改卷", "错题"],
    "学情分析难": ["学情", "分析表", "数据分析", "成绩分析", "统计", "报表"],
    "教研活动低效": ["教研", "听评课", "公开课", "磨课", "形式主义"],
    "家校沟通繁杂": ["家长", "群", "家校沟通", "家访"],
    "课堂管理累": ["课堂管理", "纪律", "学生", "管不住", "班级管理"],
    "行政任务多": ["填表", "迎检", "材料", "报表", "台账", "上报"],
    "技术培训无用": ["信息技术2.0", "培训", "没用", "走形式", "打卡"],
}


def run_bilibili_search_pipeline(
    source_files: Sequence[Path] | None = None,
    batch_id: str | None = None,
    source_offsets: dict[Path, int] | None = None,
    allowed_keywords: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run the staged post-processing pipeline for Bilibili search output."""

    data_root = Path(config.SAVE_DATA_PATH) if config.SAVE_DATA_PATH else Path("data")
    bili_root = data_root / "bili"
    if source_files is None:
        source_files = _collect_source_files(bili_root)
    else:
        source_files = [Path(path) for path in source_files if Path(path).is_file()]

    if not source_files:
        utils.logger.info("[BiliSearchPipeline] No Bilibili search exports found, skipping staged processing.")
        return {"processed": False, "reason": "no_source_files", "data_root": str(bili_root)}

    resolved_batch_id = str(batch_id or getattr(config, "BILI_SEARCH_BATCH_ID", "")).strip()
    if not resolved_batch_id:
        resolved_batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    raw_records = _load_records(source_files, source_offsets=source_offsets)
    raw_records = _fill_missing_source_keyword(raw_records)
    if allowed_keywords is not None:
        allowed_set = {str(keyword).strip() for keyword in allowed_keywords if str(keyword).strip()}
        if allowed_set:
            raw_records = [
                record
                for record in raw_records
                if str(record.get("source_keyword", "")).strip() in allowed_set
            ]

    if not raw_records:
        utils.logger.info("[BiliSearchPipeline] No new records after incremental filtering, skipping staged processing.")
        return {
            "processed": False,
            "reason": "no_new_records",
            "data_root": str(bili_root),
            "batch_id": resolved_batch_id,
            "source_files": [str(path) for path in source_files],
        }
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    buckets = _group_records_by_keyword(raw_records)
    total_clean = 0
    total_filtered = 0
    bucket_summaries: list[dict[str, Any]] = []

    for keyword, keyword_records in buckets.items():
        run_dir = bili_root / f"{_slugify_keyword(keyword)}_{timestamp}"
        raw_dir = run_dir / RAW_STAGE_DIR
        clean_dir = run_dir / CLEAN_STAGE_DIR
        filter_dir = run_dir / FILTER_STAGE_DIR
        analysis_dir = run_dir / ANALYSIS_STAGE_DIR

        for directory in (raw_dir, clean_dir, filter_dir, analysis_dir):
            directory.mkdir(parents=True, exist_ok=True)

        copied_files = _copy_source_files(source_files, raw_dir)
        clean_records = _clean_records(keyword_records)
        filtered_records = _filter_records(clean_records)
        analysis = _build_analysis(clean_records, filtered_records)

        _write_jsonl(raw_dir / f"raw_records_{timestamp}.jsonl", keyword_records)
        _write_json(
            raw_dir / f"raw_manifest_{timestamp}.json",
            {
                "generated_at": timestamp,
                "batch_id": resolved_batch_id,
                "keyword": keyword,
                "source_files": [str(path) for path in source_files],
                "copied_files": copied_files,
                "raw_record_count": len(keyword_records),
            },
        )

        _write_jsonl(clean_dir / f"cleaned_records_{timestamp}.jsonl", clean_records)
        _write_json(
            clean_dir / f"cleaned_manifest_{timestamp}.json",
            {
                "generated_at": timestamp,
                "batch_id": resolved_batch_id,
                "keyword": keyword,
                "source_files": [str(path) for path in source_files],
                "raw_record_count": len(keyword_records),
                "clean_record_count": len(clean_records),
            },
        )

        _write_jsonl(filter_dir / f"filtered_records_{timestamp}.jsonl", filtered_records)
        _write_json(
            filter_dir / f"filtered_manifest_{timestamp}.json",
            {
                "generated_at": timestamp,
                "batch_id": resolved_batch_id,
                "keyword": keyword,
                "source_files": [str(path) for path in source_files],
                "filtered_record_count": len(filtered_records),
            },
        )

        _write_json(analysis_dir / f"analysis_summary_{timestamp}.json", analysis)
        _write_text(analysis_dir / f"analysis_summary_{timestamp}.md", _render_analysis_markdown(analysis))
        _cleanup_previous_keyword_results_in_batch(
            bili_root=bili_root,
            keyword=keyword,
            current_run_dir=run_dir,
            batch_id=resolved_batch_id,
        )

        total_clean += len(clean_records)
        total_filtered += len(filtered_records)
        bucket_summaries.append(
            {
                "keyword": keyword,
                "run_dir": str(run_dir),
                "raw_record_count": len(keyword_records),
                "clean_record_count": len(clean_records),
                "filtered_record_count": len(filtered_records),
            }
        )

    utils.logger.info(
        "[BiliSearchPipeline] Completed staged processing: "
        f"keywords={len(buckets)}, raw={len(raw_records)}, clean={total_clean}, filtered={total_filtered}"
    )
    return {
        "processed": True,
        "batch_id": resolved_batch_id,
        "data_root": str(bili_root),
        "source_files": [str(path) for path in source_files],
        "raw_record_count": len(raw_records),
        "clean_record_count": total_clean,
        "filtered_record_count": total_filtered,
        "keyword_buckets": bucket_summaries,
    }


def _collect_source_files(bili_root: Path) -> list[Path]:
    allowed_suffixes = {".jsonl", ".json"}
    files: list[Path] = []
    for pattern in ("jsonl/search_*", "json/search_*"):
        for path in bili_root.glob(pattern):
            if path.is_file() and path.suffix.lower() in allowed_suffixes:
                files.append(path)
    return sorted(set(files))


def _cleanup_previous_keyword_results_in_batch(
    bili_root: Path,
    keyword: str,
    current_run_dir: Path,
    batch_id: str,
) -> None:
    slug = _slugify_keyword(keyword)
    deleted_dirs: list[str] = []

    for candidate in bili_root.glob(f"{slug}_*"):
        if not candidate.is_dir():
            continue
        if candidate.resolve() == current_run_dir.resolve():
            continue

        manifest = _load_run_raw_manifest(candidate)
        if not manifest:
            continue

        manifest_batch_id = str(manifest.get("batch_id", "")).strip()
        manifest_keyword = str(manifest.get("keyword", "")).strip()
        if manifest_batch_id != batch_id or manifest_keyword != keyword:
            continue

        try:
            shutil.rmtree(candidate)
            deleted_dirs.append(str(candidate))
        except OSError as exc:
            utils.logger.warning(f"[BiliSearchPipeline] Failed to remove old run dir {candidate}: {exc}")

    if deleted_dirs:
        utils.logger.info(
            f"[BiliSearchPipeline] Removed {len(deleted_dirs)} previous same-batch result dirs for keyword '{keyword}'."
        )


def _load_run_raw_manifest(run_dir: Path) -> dict[str, Any] | None:
    raw_dir = run_dir / RAW_STAGE_DIR
    if not raw_dir.exists() or not raw_dir.is_dir():
        return None

    manifest_files = sorted(raw_dir.glob("raw_manifest_*.json"), reverse=True)
    for manifest_file in manifest_files:
        try:
            with manifest_file.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                return payload
        except (OSError, json.JSONDecodeError):
            continue

    return None


def _copy_source_files(source_files: Sequence[Path], target_dir: Path) -> list[str]:
    copied: list[str] = []
    for source_file in source_files:
        destination = target_dir / source_file.name
        shutil.copy2(source_file, destination)
        copied.append(str(destination))
    return copied


def _load_records(
    source_files: Sequence[Path],
    source_offsets: dict[Path, int] | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    offsets = {
        Path(path).resolve(): int(offset)
        for path, offset in (source_offsets or {}).items()
    }
    for source_file in source_files:
        source_path = Path(source_file).resolve()
        start_offset = max(0, offsets.get(source_path, 0))
        if source_file.suffix.lower() == ".jsonl":
            records.extend(_load_jsonl(source_file, start_offset=start_offset))
        elif source_file.suffix.lower() == ".json":
            records.extend(_load_json(source_file))
    return records


def _load_jsonl(source_file: Path, start_offset: int = 0) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with source_file.open("rb") as handle:
        if start_offset:
            try:
                handle.seek(start_offset)
            except OSError:
                handle.seek(0)

        for line in handle:
            try:
                line = line.decode("utf-8", errors="ignore").strip()
            except Exception:
                continue
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
    return records


def _load_json(source_file: Path) -> list[dict[str, Any]]:
    try:
        with source_file.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _group_records_by_keyword(raw_records: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for record in raw_records:
        keyword = str(record.get("source_keyword") or "未标注").strip() or "未标注"
        buckets.setdefault(keyword, []).append(record)
    return buckets


def _fill_missing_source_keyword(raw_records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched_records: list[dict[str, Any]] = []
    keyword_by_video_id: dict[str, str] = {}
    keyword_by_user_id: dict[str, str] = {}

    for record in raw_records:
        source_keyword = str(record.get("source_keyword", "")).strip()
        if not source_keyword:
            continue

        video_id = str(record.get("video_id", "")).strip()
        if video_id and video_id not in keyword_by_video_id:
            keyword_by_video_id[video_id] = source_keyword

        user_id = str(record.get("user_id", "")).strip()
        if user_id and user_id not in keyword_by_user_id:
            keyword_by_user_id[user_id] = source_keyword

    configured_keywords = [part.strip() for part in str(getattr(config, "KEYWORDS", "")).split(",") if part.strip()]
    fallback_keyword = configured_keywords[0] if len(configured_keywords) == 1 else ""

    for record in raw_records:
        current_keyword = str(record.get("source_keyword", "")).strip()
        if current_keyword:
            enriched_records.append(record)
            continue

        inferred_keyword = ""
        video_id = str(record.get("video_id", "")).strip()
        user_id = str(record.get("user_id", "")).strip()

        if video_id:
            inferred_keyword = keyword_by_video_id.get(video_id, "")
        if not inferred_keyword and user_id:
            inferred_keyword = keyword_by_user_id.get(user_id, "")
        if not inferred_keyword:
            inferred_keyword = fallback_keyword

        if inferred_keyword:
            enriched_record = dict(record)
            enriched_record["source_keyword"] = inferred_keyword
            enriched_records.append(enriched_record)
        else:
            enriched_records.append(record)

    return enriched_records


def _clean_records(raw_records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    cleaned: list[dict[str, Any]] = []

    for raw_record in raw_records:
        normalized = _normalize_record(raw_record)
        dedupe_key = normalized["dedupe_key"]
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        cleaned.append(normalized)

    cleaned.sort(key=lambda item: (item.get("score", 0), item.get("publish_time", 0)), reverse=True)
    return cleaned


def _slugify_keyword(keyword: str) -> str:
    slug = re.sub(r"[\\/:*?\"<>|]+", "_", keyword.strip())
    slug = re.sub(r"\s+", "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug[:80] or "keyword"


def _normalize_record(raw_record: dict[str, Any]) -> dict[str, Any]:
    if "video_id" in raw_record:
        entry_type = "content"
        record_id = str(raw_record.get("video_id", ""))
        text = " ".join(part for part in [raw_record.get("title", ""), raw_record.get("desc", "")] if part)
        author_id = str(raw_record.get("user_id", ""))
        author_name = raw_record.get("nickname", "")
        author_sign = raw_record.get("author_sign", "")
        publish_time = _safe_int(raw_record.get("create_time"))
        metrics = {
            "liked_count": _safe_int(raw_record.get("liked_count")),
            "comment_count": _safe_int(raw_record.get("video_comment")),
            "share_count": _safe_int(raw_record.get("video_share_count")),
            "favorite_count": _safe_int(raw_record.get("video_favorite_count")),
            "coin_count": _safe_int(raw_record.get("video_coin_count")),
            "danmaku_count": _safe_int(raw_record.get("video_danmaku")),
            "play_count": _safe_int(raw_record.get("video_play_count")),
        }
    elif "comment_id" in raw_record:
        entry_type = "comment"
        record_id = str(raw_record.get("comment_id", ""))
        text = str(raw_record.get("content", ""))
        author_id = str(raw_record.get("user_id", ""))
        author_name = raw_record.get("nickname", "")
        author_sign = raw_record.get("sign", "")
        publish_time = _safe_int(raw_record.get("create_time"))
        metrics = {
            "liked_count": _safe_int(raw_record.get("like_count")),
            "comment_count": _safe_int(raw_record.get("sub_comment_count")),
        }
    elif "user_id" in raw_record:
        entry_type = "creator"
        record_id = str(raw_record.get("user_id", ""))
        text = str(raw_record.get("sign", ""))
        author_id = str(raw_record.get("user_id", ""))
        author_name = raw_record.get("nickname", "")
        author_sign = raw_record.get("sign", "")
        publish_time = _safe_int(raw_record.get("last_modify_ts"))
        metrics = {
            "fans": _safe_int(raw_record.get("total_fans")),
            "liked_count": _safe_int(raw_record.get("total_liked")),
            "rank": _safe_int(raw_record.get("user_rank")),
        }
    else:
        entry_type = "unknown"
        record_id = hashlib.sha1(json.dumps(raw_record, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        text = json.dumps(raw_record, ensure_ascii=False)
        author_id = ""
        author_name = ""
        author_sign = ""
        publish_time = 0
        metrics = {}

    source_keyword = str(raw_record.get("source_keyword", "")).strip()
    title = str(raw_record.get("title", "")).strip()
    text = _normalize_text(text)
    author_text = _normalize_text(" ".join(part for part in [author_name, author_sign] if part))
    combined_text = _normalize_text(" ".join(part for part in [title, text, author_text, source_keyword] if part))
    teacher_hits = _count_hits(combined_text, TEACHER_TERMS)
    pain_hits = _count_hits(combined_text, PAIN_TERMS)
    request_hits = _count_hits(combined_text, REQUEST_TERMS)
    wuhan_hits = _count_hits(combined_text, WUHAN_TERMS)
    ad_hits = _count_hits(combined_text, AD_TERMS)
    categories = _classify_categories(combined_text)
    author_is_teacher = bool(re.search(r"(教师|老师|班主任|中学|初中|高中|教育工作者)", author_text))

    engagement = _sum_positive(metrics.values())
    score = _calculate_score(engagement, teacher_hits, pain_hits, request_hits, wuhan_hits)
    content_hash = hashlib.sha1(combined_text.encode("utf-8")).hexdigest()

    return {
        "platform": "bili",
        "entry_type": entry_type,
        "record_id": record_id,
        "source_keyword": source_keyword,
        "title": title,
        "text": text,
        "author_id": author_id,
        "author_name": author_name,
        "author_sign": author_sign,
        "author_is_teacher": author_is_teacher,
        "publish_time": publish_time,
        "publish_time_iso": _format_timestamp(publish_time),
        "metrics": metrics,
        "combined_text": combined_text,
        "content_hash": content_hash,
        "signals": {
            "teacher_hits": teacher_hits,
            "pain_hits": pain_hits,
            "request_hits": request_hits,
            "wuhan_hits": wuhan_hits,
            "ad_hits": ad_hits,
            "categories": categories,
        },
        "score": score,
        "dedupe_key": f"bili:{entry_type}:{record_id}:{content_hash}",
    }


def _filter_records(clean_records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for record in clean_records:
        signals = record["signals"]
        teacher_hits = signals["teacher_hits"]
        pain_hits = signals["pain_hits"]
        request_hits = signals["request_hits"]
        wuhan_hits = signals["wuhan_hits"]
        ad_hits = signals["ad_hits"]

        keep_reason: list[str] = []
        if teacher_hits >= 2:
            keep_reason.append("teacher_terms>=2")
        if record.get("author_is_teacher"):
            keep_reason.append("author_profile_teacher")
        if pain_hits > 0:
            keep_reason.append("pain_terms")
        if request_hits > 0:
            keep_reason.append("need_expression")
        if wuhan_hits > 0:
            keep_reason.append("wuhan_related")

        if ad_hits > 0 and not keep_reason:
            continue
        if not keep_reason:
            continue

        filtered_record = dict(record)
        filtered_record["keep_reason"] = keep_reason
        filtered_record["score"] = _calculate_score(
            _sum_positive(record.get("metrics", {}).values()),
            teacher_hits,
            pain_hits,
            request_hits,
            wuhan_hits,
        )
        filtered.append(filtered_record)

    filtered.sort(key=lambda item: (item.get("score", 0), item.get("publish_time", 0)), reverse=True)
    return filtered


def _build_analysis(clean_records: Sequence[dict[str, Any]], filtered_records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    category_counter: Counter[str] = Counter()
    source_counter: Counter[str] = Counter()
    entry_type_counter: Counter[str] = Counter()
    wuhan_count = 0
    teacher_count = 0

    for record in filtered_records:
        entry_type_counter.update([record.get("entry_type", "unknown")])
        source_keyword = record.get("source_keyword") or "未标注"
        source_counter.update([source_keyword])
        signals = record.get("signals", {})
        categories = signals.get("categories", [])
        category_counter.update(categories)
        teacher_count += int(bool(record.get("author_is_teacher")) or signals.get("teacher_hits", 0) >= 2)
        wuhan_count += int(signals.get("wuhan_hits", 0) > 0)

    top_records = [
        {
            "rank": index + 1,
            "entry_type": record.get("entry_type"),
            "record_id": record.get("record_id"),
            "score": round(float(record.get("score", 0)), 3),
            "source_keyword": record.get("source_keyword", ""),
            "categories": record.get("signals", {}).get("categories", []),
            "snippet": _snippet(record.get("combined_text", "")),
            "keep_reason": record.get("keep_reason", []),
        }
        for index, record in enumerate(filtered_records[:20])
    ]

    return {
        "generated_at": utils.get_current_date(),
        "platform": "bili",
        "clean_record_count": len(clean_records),
        "filtered_record_count": len(filtered_records),
        "teacher_related_count": teacher_count,
        "wuhan_related_count": wuhan_count,
        "entry_type_distribution": dict(entry_type_counter),
        "top_source_keywords": source_counter.most_common(10),
        "top_pain_categories": category_counter.most_common(10),
        "top_records": top_records,
    }


def _render_analysis_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Bilibili Search 分析汇总",
        "",
        f"- 生成时间：{summary.get('generated_at', '')}",
        f"- 清洗后条目数：{summary.get('clean_record_count', 0)}",
        f"- 筛选后条目数：{summary.get('filtered_record_count', 0)}",
        f"- 教师相关条目数：{summary.get('teacher_related_count', 0)}",
        f"- 武汉相关条目数：{summary.get('wuhan_related_count', 0)}",
        "",
        "## 主要痛点类别",
    ]

    for category, count in summary.get("top_pain_categories", []):
        lines.append(f"- {category}：{count}")

    lines.extend([
        "",
        "## Top 20 样本",
        "| 排名 | 类型 | 分数 | 来源关键词 | 代表文本 |",
        "| --- | --- | ---: | --- | --- |",
    ])

    for item in summary.get("top_records", []):
        lines.append(
            "| {rank} | {entry_type} | {score} | {source_keyword} | {snippet} |".format(
                rank=item.get("rank", ""),
                entry_type=item.get("entry_type", ""),
                score=item.get("score", 0),
                source_keyword=_escape_markdown(str(item.get("source_keyword", ""))),
                snippet=_escape_markdown(str(item.get("snippet", ""))),
            )
        )

    return "\n".join(lines) + "\n"


def _classify_categories(text: str) -> list[str]:
    matched: list[str] = []
    for category, keywords in CATEGORY_RULES.items():
        if any(keyword in text for keyword in keywords):
            matched.append(category)
    return matched


def _calculate_score(engagement: int, teacher_hits: int, pain_hits: int, request_hits: int, wuhan_hits: int) -> float:
    heat_score = math.sqrt(max(engagement, 0) + 1)
    heat_norm = min(1.0, heat_score / 25.0)
    pain_score = min(2, teacher_hits + pain_hits)
    request_score = min(2, request_hits)
    region_bonus = 0.2 if wuhan_hits > 0 else 0.0
    return round(0.3 * heat_norm + 0.4 * pain_score + 0.3 * request_score + region_bonus, 6)


def _count_hits(text: str, terms: Iterable[str]) -> int:
    return sum(1 for term in terms if term and term in text)


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _sum_positive(values: Iterable[Any]) -> int:
    total = 0
    for value in values:
        total += max(_safe_int(value), 0)
    return total


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _format_timestamp(timestamp: int) -> str:
    if not timestamp:
        return ""
    try:
        return utils.get_time_str_from_unix_time(timestamp)
    except Exception:
        return ""


def _snippet(text: str, length: int = 120) -> str:
    cleaned = re.sub(r"https?://\S+", "", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:length]


def _escape_markdown(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _write_jsonl(file_path: Path, items: Sequence[dict[str, Any]]) -> None:
    with file_path.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def _write_json(file_path: Path, payload: dict[str, Any]) -> None:
    with file_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def _write_text(file_path: Path, text: str) -> None:
    with file_path.open("w", encoding="utf-8") as handle:
        handle.write(text)
