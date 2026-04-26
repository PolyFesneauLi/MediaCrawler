# -*- coding: utf-8 -*-
"""Bilibili 阶段1原始抓取数据的增强痛点分析脚本。

用法示例:
    python tools/bili_pain_insight.py --run-folder 新手教师_崩溃_经历_20260426_214211
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


STAGE1_DIR = "阶段1_原始抓取"
STAGE5_DIR = "阶段5_需求洞察增强"

PAIN_CATEGORY_TERMS: dict[str, list[str]] = {
    "备课与资源准备耗时": ["备课", "教案", "课件", "找题", "找素材", "PPT", "公开课", "磨课"],
    "作业与批改负担重": ["作业", "批改", "改卷", "作文", "试卷", "判题", "错题"],
    "课堂管理与纪律压力": ["课堂管理", "纪律", "管不住", "吵", "捣乱", "班级管理"],
    "行政事务与表格负担": ["填表", "台账", "报表", "迎检", "材料", "打卡", "流程"],
    "家校沟通成本高": ["家长", "家校", "家访", "家长群", "沟通", "回复消息"],
    "教研培训低效": ["教研", "听评课", "形式主义", "培训", "2.0", "公开课", "教研活动"],
    "职业情绪与心理压力": ["崩溃", "焦虑", "压力", "心累", "疲惫", "劝退", "撑不住", "加班"],
    "教学效率工具诉求": ["AI", "工具", "软件", "系统", "自动", "提效", "效率", "推荐"],
}

TEACHER_TERMS = [
    "教师",
    "老师",
    "班主任",
    "中学",
    "初中",
    "高中",
    "教学",
    "备课",
    "教研",
    "作业",
]

DEMAND_TERMS = [
    "求推荐",
    "推荐一下",
    "有没有",
    "怎么",
    "如何",
    "想要",
    "需要",
    "能不能",
    "有没有好用",
    "求个",
]

AI_DIRECTION_HINTS: dict[str, list[str]] = {
    "备课与资源准备耗时": ["教案初稿生成", "课件提纲自动化", "多版本教学素材推荐"],
    "作业与批改负担重": ["主观题初评与错因归类", "分层作业自动生成", "批改反馈模板"],
    "课堂管理与纪律压力": ["课堂事件记录与复盘", "班级行为预警标签", "课堂活动脚本建议"],
    "行政事务与表格负担": ["台账自动汇总", "材料填报助手", "多系统信息复用"],
    "家校沟通成本高": ["家校消息模板", "个性化学情简报", "沟通语气优化建议"],
    "教研培训低效": ["教研主题自动归纳", "听评课记录结构化", "培训内容实用度打分"],
    "职业情绪与心理压力": ["情绪压力监测问卷", "减负动作清单推荐", "教师互助知识库检索"],
    "教学效率工具诉求": ["工具选型对比助手", "场景化工作流编排", "跨工具自动化连接"],
}


@dataclass
class Evidence:
    source: str
    source_keyword: str
    text: str
    likes: int
    matched_terms: list[str]


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _iter_stage1_rows(stage1_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    file_patterns = [
        "raw_records_*.jsonl",
        "search_contents_*.jsonl",
        "search_comments_*.jsonl",
        "search_creators_*.jsonl",
    ]
    for pattern in file_patterns:
        for file_path in sorted(stage1_dir.glob(pattern)):
            rows.extend(_load_jsonl(file_path))
    return rows


def _extract_text_and_source(row: dict[str, Any]) -> tuple[str, str, int]:
    if "comment_id" in row:
        text = str(row.get("content", ""))
        likes = _safe_int(row.get("like_count"))
        return text, "comment", likes
    if "video_id" in row:
        title = str(row.get("title", ""))
        desc = str(row.get("desc", ""))
        likes = _safe_int(row.get("liked_count"))
        return " ".join([title, desc]).strip(), "content", likes
    if "user_id" in row:
        text = str(row.get("sign", ""))
        likes = _safe_int(row.get("total_liked"))
        return text, "creator", likes
    return json.dumps(row, ensure_ascii=False), "unknown", 0


def _match_terms(text: str, terms: list[str]) -> list[str]:
    return [term for term in terms if term and term in text]


def build_insight(rows: list[dict[str, Any]], run_folder: str) -> dict[str, Any]:
    cleaned_rows: list[dict[str, Any]] = []
    for row in rows:
        text, source, likes = _extract_text_and_source(row)
        normalized_text = _normalize_text(text)
        if not normalized_text:
            continue
        cleaned_rows.append(
            {
                "text": normalized_text,
                "source": source,
                "likes": likes,
                "source_keyword": str(row.get("source_keyword", "")).strip(),
            }
        )

    total_rows = len(cleaned_rows)
    if total_rows == 0:
        return {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "run_folder": run_folder,
            "total_rows": 0,
            "message": "未在阶段1找到可分析文本。",
            "pain_categories": [],
        }

    category_counter: Counter[str] = Counter()
    evidence_pool: dict[str, list[Evidence]] = defaultdict(list)
    teacher_related = 0
    demand_related = 0

    for item in cleaned_rows:
        text = item["text"]
        source_keyword = item["source_keyword"]
        teacher_hits = _match_terms(text, TEACHER_TERMS)
        keyword_teacher_hits = _match_terms(source_keyword, TEACHER_TERMS)
        demand_hits = _match_terms(text, DEMAND_TERMS)
        is_teacher_context = bool(teacher_hits or keyword_teacher_hits)
        if is_teacher_context:
            teacher_related += 1
        if is_teacher_context and demand_hits:
            demand_related += 1

        for category, terms in PAIN_CATEGORY_TERMS.items():
            if not is_teacher_context:
                continue
            matched = _match_terms(text, terms)
            if not matched:
                continue
            category_counter[category] += 1
            evidence_pool[category].append(
                Evidence(
                    source=item["source"],
                    source_keyword=item["source_keyword"] or "未标注",
                    text=text[:160],
                    likes=item["likes"],
                    matched_terms=matched[:6],
                )
            )

    ranked_categories = category_counter.most_common()
    pain_row_total = sum(category_counter.values())

    category_details: list[dict[str, Any]] = []
    for category, count in ranked_categories:
        deduped_evidences: list[Evidence] = []
        seen_texts: set[str] = set()
        for evidence in sorted(evidence_pool.get(category, []), key=lambda x: x.likes, reverse=True):
            if evidence.text in seen_texts:
                continue
            seen_texts.add(evidence.text)
            deduped_evidences.append(evidence)
            if len(deduped_evidences) >= 8:
                break

        category_details.append(
            {
                "category": category,
                "count": count,
                "coverage_in_texts": round(count / total_rows, 4),
                "coverage_in_pain_mentions": round(count / pain_row_total, 4) if pain_row_total else 0.0,
                "suggested_ai_directions": AI_DIRECTION_HINTS.get(category, []),
                "evidence_samples": [
                    {
                        "source": e.source,
                        "source_keyword": e.source_keyword,
                        "likes": e.likes,
                        "matched_terms": e.matched_terms,
                        "text": e.text,
                    }
                    for e in deduped_evidences
                ],
            }
        )

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "run_folder": run_folder,
        "total_rows": total_rows,
        "teacher_related_rows": teacher_related,
        "teacher_related_ratio": round(teacher_related / total_rows, 4),
        "demand_expression_rows": demand_related,
        "demand_expression_ratio": round(demand_related / total_rows, 4),
        "pain_mentions_total": pain_row_total,
        "pain_categories": category_details,
    }


def render_markdown(result: dict[str, Any]) -> str:
    if result.get("total_rows", 0) == 0:
        return "# 阶段5：需求洞察增强\n\n未找到可分析文本。\n"

    lines = [
        "# 阶段5：需求洞察增强",
        "",
        f"- 生成时间：{result.get('generated_at', '')}",
        f"- 任务目录：{result.get('run_folder', '')}",
        f"- 可分析文本总量：{result.get('total_rows', 0)}",
        f"- 教师相关文本：{result.get('teacher_related_rows', 0)}（占比 {result.get('teacher_related_ratio', 0):.2%}）",
        f"- 需求表达文本：{result.get('demand_expression_rows', 0)}（占比 {result.get('demand_expression_ratio', 0):.2%}）",
        f"- 痛点命中总次数：{result.get('pain_mentions_total', 0)}",
        "",
        "## 痛点类别强度（按命中次数）",
        "| 痛点类别 | 命中次数 | 在全部文本中的覆盖率 | 在痛点命中中的占比 |",
        "| --- | ---: | ---: | ---: |",
    ]

    categories = result.get("pain_categories", [])
    for item in categories:
        lines.append(
            "| {category} | {count} | {all_cov:.2%} | {pain_cov:.2%} |".format(
                category=item.get("category", ""),
                count=item.get("count", 0),
                all_cov=float(item.get("coverage_in_texts", 0)),
                pain_cov=float(item.get("coverage_in_pain_mentions", 0)),
            )
        )

    for item in categories[:5]:
        lines.extend(
            [
                "",
                f"## {item.get('category', '')}",
                f"- 推荐AI方向：{'；'.join(item.get('suggested_ai_directions', [])) or '待补充'}",
                "- 代表证据：",
            ]
        )
        evidences = item.get("evidence_samples", [])[:3]
        if not evidences:
            lines.append("  - 暂无样例")
            continue
        for e in evidences:
            lines.append(
                "  - [{source}] 👍{likes} | 命中词: {terms} | {text}".format(
                    source=e.get("source", "unknown"),
                    likes=e.get("likes", 0),
                    terms="、".join(e.get("matched_terms", [])),
                    text=str(e.get("text", "")).replace("\n", " "),
                )
            )

    lines.append("")
    return "\n".join(lines)


def _cleanup_old_outputs(output_dir: Path, keep_json: Path, keep_md: Path) -> None:
    """Remove historical pain insight outputs and keep only the newest pair."""

    for file_path in output_dir.glob("pain_insight_*.json"):
        if file_path.resolve() == keep_json.resolve():
            continue
        try:
            file_path.unlink()
        except OSError:
            pass

    for file_path in output_dir.glob("pain_insight_*.md"):
        if file_path.resolve() == keep_md.resolve():
            continue
        try:
            file_path.unlink()
        except OSError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="对 data/bili/<任务目录>/阶段1_原始抓取 进行增强痛点分析")
    parser.add_argument(
        "--run-folder",
        required=True,
        help="要处理的一层爬虫结果文件夹名字，例如：新手教师_崩溃_经历_20260426_214211",
    )
    parser.add_argument(
        "--data-root",
        default="data/bili",
        help="bili 数据根目录，默认 data/bili",
    )
    args = parser.parse_args()

    run_dir = Path(args.data_root) / args.run_folder
    stage1_dir = run_dir / STAGE1_DIR
    if not stage1_dir.exists():
        raise FileNotFoundError(f"未找到阶段1目录: {stage1_dir}")

    rows = _iter_stage1_rows(stage1_dir)
    result = build_insight(rows, args.run_folder)

    output_dir = run_dir / STAGE5_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"pain_insight_{timestamp}.json"
    md_path = output_dir / f"pain_insight_{timestamp}.md"

    with json_path.open("w", encoding="utf-8") as jf:
        json.dump(result, jf, ensure_ascii=False, indent=2)
    with md_path.open("w", encoding="utf-8") as mf:
        mf.write(render_markdown(result))

    _cleanup_old_outputs(output_dir=output_dir, keep_json=json_path, keep_md=md_path)

    print(f"[pain_insight] 分析完成: {args.run_folder}")
    print(f"[pain_insight] 输出JSON: {json_path}")
    print(f"[pain_insight] 输出Markdown: {md_path}")


if __name__ == "__main__":
    main()
