# -*- coding: utf-8 -*-
"""Bilibili 阶段1原始抓取数据的增强痛点分析脚本。

支持三种模式：
1) 单目录：--run-folder xxx
2) 全部目录分别输出：--all-sep
3) 全部目录综合输出：--all

示例：
    python tools/bili_pain_insight.py --run-folder 新手教师_崩溃_经历_20260426_214211
    python tools/bili_pain_insight.py --all-sep
    python tools/bili_pain_insight.py --all
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    HAS_MATPLOTLIB = True
except Exception:
    HAS_MATPLOTLIB = False


STAGE1_DIR = "阶段1_原始抓取"
STAGE5_DIR = "阶段5_需求洞察增强"
GLOBAL_STAGE5_DIR = "阶段5_综合结论"

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


@dataclass
class NormalizedRow:
    text: str
    source: str
    likes: int
    source_keyword: str
    run_folder: str


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


def _normalize_rows(rows: list[dict[str, Any]], run_folder: str) -> list[NormalizedRow]:
    cleaned_rows: list[NormalizedRow] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for row in rows:
        text, source, likes = _extract_text_and_source(row)
        normalized_text = _normalize_text(text)
        if not normalized_text:
            continue
        source_keyword = str(row.get("source_keyword", "")).strip()
        dedupe_key = (source, normalized_text, source_keyword)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        cleaned_rows.append(
            NormalizedRow(
                text=normalized_text,
                source=source,
                likes=likes,
                source_keyword=source_keyword,
                run_folder=run_folder,
            )
        )
    return cleaned_rows


def build_insight(rows: list[dict[str, Any]], run_folder: str) -> dict[str, Any]:
    cleaned_rows = _normalize_rows(rows, run_folder)

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

    source_counter: Counter[str] = Counter()
    folder_counter: Counter[str] = Counter()
    for item in cleaned_rows:
        text = item.text
        source_keyword = item.source_keyword
        teacher_hits = _match_terms(text, TEACHER_TERMS)
        keyword_teacher_hits = _match_terms(source_keyword, TEACHER_TERMS)
        demand_hits = _match_terms(text, DEMAND_TERMS)
        is_teacher_context = bool(teacher_hits or keyword_teacher_hits)
        source_counter[item.source] += 1
        folder_counter[item.run_folder] += 1
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
                    source=item.source,
                    source_keyword=item.source_keyword or "未标注",
                    text=text[:160],
                    likes=item.likes,
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
        "source_distribution": dict(source_counter),
        "run_distribution": dict(folder_counter),
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


def _list_run_folders(data_root: Path) -> list[Path]:
    run_dirs: list[Path] = []
    for path in sorted(data_root.iterdir()):
        if not path.is_dir():
            continue
        if path.name == "jsonl":
            continue
        if (path / STAGE1_DIR).exists():
            run_dirs.append(path)
    return run_dirs


def _save_result_files(output_dir: Path, result: dict[str, Any], clean_old: bool = True) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"pain_insight_{timestamp}.json"
    md_path = output_dir / f"pain_insight_{timestamp}.md"
    with json_path.open("w", encoding="utf-8") as jf:
        json.dump(result, jf, ensure_ascii=False, indent=2)
    with md_path.open("w", encoding="utf-8") as mf:
        mf.write(render_markdown(result))
    if clean_old:
        _cleanup_old_outputs(output_dir=output_dir, keep_json=json_path, keep_md=md_path)
    return json_path, md_path


def _save_csv(file_path: Path, headers: list[str], rows: list[list[Any]]) -> None:
    with file_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


def _generate_charts(output_dir: Path, result: dict[str, Any]) -> list[Path]:
    if not HAS_MATPLOTLIB:
        return []

    chart_paths: list[Path] = []
    categories = result.get("pain_categories", [])
    if categories:
        top_categories = categories[:10]
        labels = [f"C{i + 1}" for i in range(len(top_categories))]
        values = [int(x.get("count", 0)) for x in top_categories]

        plt.figure(figsize=(12, 6))
        plt.bar(labels, values, color="#4C78A8")
        plt.title("Pain Categories Hit Count Top10")
        plt.xlabel("Category Code")
        plt.ylabel("Hit Count")
        plt.xticks(rotation=0, ha="center")
        plt.tight_layout()
        bar_path = output_dir / "chart_pain_category_bar.png"
        plt.savefig(bar_path, dpi=180)
        plt.close()
        chart_paths.append(bar_path)
        _save_csv(
            output_dir / "table_chart_category_code_map.csv",
            ["code", "pain_category", "count"],
            [[f"C{i + 1}", str(item.get("category", "")), int(item.get("count", 0))] for i, item in enumerate(top_categories)],
        )

    source_dist = result.get("source_distribution", {})
    if source_dist:
        labels = list(source_dist.keys())
        values = [int(source_dist[k]) for k in labels]
        plt.figure(figsize=(8, 8))
        plt.pie(values, labels=labels, autopct="%1.1f%%", startangle=120)
        plt.title("Source Distribution")
        plt.tight_layout()
        pie_path = output_dir / "chart_source_pie.png"
        plt.savefig(pie_path, dpi=180)
        plt.close()
        chart_paths.append(pie_path)

    run_dist = result.get("run_distribution", {})
    if run_dist and len(run_dist) > 1:
        sorted_runs = sorted(run_dist.items(), key=lambda x: x[1], reverse=True)[:12]
        labels = [f"R{i + 1}" for i in range(len(sorted_runs))]
        values = [int(v) for _, v in sorted_runs]
        plt.figure(figsize=(13, 6))
        plt.bar(labels, values, color="#72B7B2")
        plt.title("Run Folder Volume Comparison")
        plt.xlabel("Run Code")
        plt.ylabel("Text Count")
        plt.xticks(rotation=0, ha="center")
        plt.tight_layout()
        run_bar_path = output_dir / "chart_run_volume_bar.png"
        plt.savefig(run_bar_path, dpi=180)
        plt.close()
        chart_paths.append(run_bar_path)
        _save_csv(
            output_dir / "table_chart_run_code_map.csv",
            ["code", "run_folder", "count"],
            [[f"R{i + 1}", run_name, int(count)] for i, (run_name, count) in enumerate(sorted_runs)],
        )

    return chart_paths


def _process_single_run(run_dir: Path) -> tuple[dict[str, Any], Path, Path]:
    stage1_dir = run_dir / STAGE1_DIR
    if not stage1_dir.exists():
        raise FileNotFoundError(f"未找到阶段1目录: {stage1_dir}")
    rows = _iter_stage1_rows(stage1_dir)
    result = build_insight(rows, run_dir.name)
    json_path, md_path = _save_result_files(run_dir / STAGE5_DIR, result, clean_old=True)
    _generate_charts(run_dir / STAGE5_DIR, result)
    _save_csv(
        run_dir / STAGE5_DIR / "table_pain_categories.csv",
        ["pain_category", "count", "coverage_in_texts", "coverage_in_pain_mentions"],
        [
            [
                item.get("category", ""),
                item.get("count", 0),
                item.get("coverage_in_texts", 0),
                item.get("coverage_in_pain_mentions", 0),
            ]
            for item in result.get("pain_categories", [])
        ],
    )
    _save_csv(
        run_dir / STAGE5_DIR / "table_source_distribution.csv",
        ["source", "count"],
        [[k, v] for k, v in result.get("source_distribution", {}).items()],
    )
    return result, json_path, md_path


def _process_all_separate(data_root: Path) -> None:
    run_dirs = _list_run_folders(data_root)
    if not run_dirs:
        raise FileNotFoundError(f"未找到可处理目录: {data_root}")
    for run_dir in run_dirs:
        _, json_path, md_path = _process_single_run(run_dir)
        print(f"[pain_insight][all-sep] 已完成: {run_dir.name}")
        print(f"[pain_insight][all-sep] 输出JSON: {json_path}")
        print(f"[pain_insight][all-sep] 输出Markdown: {md_path}")


def _process_all_global(data_root: Path) -> None:
    run_dirs = _list_run_folders(data_root)
    if not run_dirs:
        raise FileNotFoundError(f"未找到可处理目录: {data_root}")

    all_rows: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        rows = _iter_stage1_rows(run_dir / STAGE1_DIR)
        for row in rows:
            wrapped = dict(row)
            wrapped["__run_folder__"] = run_dir.name
            all_rows.append(wrapped)

    normalized_rows: list[NormalizedRow] = []
    seen_global_keys: set[tuple[str, str, str]] = set()
    for row in all_rows:
        run_folder = str(row.get("__run_folder__", "unknown"))
        text, source, likes = _extract_text_and_source(row)
        normalized_text = _normalize_text(text)
        if not normalized_text:
            continue
        source_keyword = str(row.get("source_keyword", "")).strip()
        dedupe_key = (source, normalized_text, source_keyword)
        if dedupe_key in seen_global_keys:
            continue
        seen_global_keys.add(dedupe_key)
        normalized_rows.append(
            NormalizedRow(
                text=normalized_text,
                source=source,
                likes=likes,
                source_keyword=source_keyword,
                run_folder=run_folder,
            )
        )

    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "run_folder": "ALL_GLOBAL",
        "total_rows": 0,
        "teacher_related_rows": 0,
        "teacher_related_ratio": 0.0,
        "demand_expression_rows": 0,
        "demand_expression_ratio": 0.0,
        "pain_mentions_total": 0,
        "source_distribution": {},
        "run_distribution": {},
        "pain_categories": [],
    }
    if normalized_rows:
        category_counter: Counter[str] = Counter()
        evidence_pool: dict[str, list[Evidence]] = defaultdict(list)
        teacher_related = 0
        demand_related = 0
        source_counter: Counter[str] = Counter()
        run_counter: Counter[str] = Counter()

        for item in normalized_rows:
            source_counter[item.source] += 1
            run_counter[item.run_folder] += 1
            teacher_hits = _match_terms(item.text, TEACHER_TERMS)
            keyword_teacher_hits = _match_terms(item.source_keyword, TEACHER_TERMS)
            demand_hits = _match_terms(item.text, DEMAND_TERMS)
            is_teacher_context = bool(teacher_hits or keyword_teacher_hits)
            if is_teacher_context:
                teacher_related += 1
            if is_teacher_context and demand_hits:
                demand_related += 1

            for category, terms in PAIN_CATEGORY_TERMS.items():
                if not is_teacher_context:
                    continue
                matched = _match_terms(item.text, terms)
                if not matched:
                    continue
                category_counter[category] += 1
                evidence_pool[category].append(
                    Evidence(
                        source=item.source,
                        source_keyword=item.source_keyword or "未标注",
                        text=f"[{item.run_folder}] {item.text[:140]}",
                        likes=item.likes,
                        matched_terms=matched[:6],
                    )
                )

        pain_row_total = sum(category_counter.values())
        category_details: list[dict[str, Any]] = []
        for category, count in category_counter.most_common():
            deduped_evidences: list[Evidence] = []
            seen_texts: set[str] = set()
            for evidence in sorted(evidence_pool.get(category, []), key=lambda x: x.likes, reverse=True):
                if evidence.text in seen_texts:
                    continue
                seen_texts.add(evidence.text)
                deduped_evidences.append(evidence)
                if len(deduped_evidences) >= 10:
                    break
            category_details.append(
                {
                    "category": category,
                    "count": count,
                    "coverage_in_texts": round(count / len(normalized_rows), 4),
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

        result = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "run_folder": "ALL_GLOBAL",
            "total_rows": len(normalized_rows),
            "teacher_related_rows": teacher_related,
            "teacher_related_ratio": round(teacher_related / len(normalized_rows), 4),
            "demand_expression_rows": demand_related,
            "demand_expression_ratio": round(demand_related / len(normalized_rows), 4),
            "pain_mentions_total": pain_row_total,
            "source_distribution": dict(source_counter),
            "run_distribution": dict(run_counter),
            "pain_categories": category_details,
        }

    output_dir = data_root / GLOBAL_STAGE5_DIR
    json_path, md_path = _save_result_files(output_dir, result, clean_old=False)
    chart_paths = _generate_charts(output_dir, result)
    _save_csv(
        output_dir / "table_global_pain_categories.csv",
        ["pain_category", "count", "coverage_in_texts", "coverage_in_pain_mentions"],
        [
            [
                item.get("category", ""),
                item.get("count", 0),
                item.get("coverage_in_texts", 0),
                item.get("coverage_in_pain_mentions", 0),
            ]
            for item in result.get("pain_categories", [])
        ],
    )
    _save_csv(
        output_dir / "table_global_run_distribution.csv",
        ["run_folder", "count"],
        sorted([[k, v] for k, v in result.get("run_distribution", {}).items()], key=lambda x: x[1], reverse=True),
    )
    _save_csv(
        output_dir / "table_global_source_distribution.csv",
        ["source", "count"],
        [[k, v] for k, v in result.get("source_distribution", {}).items()],
    )

    print(f"[pain_insight][all] 综合分析完成，目录数: {len(run_dirs)}")
    print(f"[pain_insight][all] 输出JSON: {json_path}")
    print(f"[pain_insight][all] 输出Markdown: {md_path}")
    for chart in chart_paths:
        print(f"[pain_insight][all] 图表: {chart}")


def main() -> None:
    parser = argparse.ArgumentParser(description="对 data/bili/<任务目录>/阶段1_原始抓取 进行增强痛点分析")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--run-folder",
        help="要处理的一层爬虫结果文件夹名字，例如：新手教师_崩溃_经历_20260426_214211",
    )
    group.add_argument("--all-sep", action="store_true", help="处理 data/bili 下所有目录，并各自生成阶段5")
    group.add_argument("--all", action="store_true", help="综合所有目录，生成全局阶段5综合结论")
    parser.add_argument(
        "--data-root",
        default="data/bili",
        help="bili 数据根目录，默认 data/bili",
    )
    args = parser.parse_args()
    data_root = Path(args.data_root)

    if args.all_sep:
        _process_all_separate(data_root)
        return
    if args.all:
        _process_all_global(data_root)
        return
    if args.run_folder:
        run_dir = data_root / args.run_folder
        result, json_path, md_path = _process_single_run(run_dir)
        print(f"[pain_insight] 分析完成: {args.run_folder}")
        print(f"[pain_insight] 输出JSON: {json_path}")
        print(f"[pain_insight] 输出Markdown: {md_path}")
        if HAS_MATPLOTLIB:
            print(f"[pain_insight] 图表目录: {run_dir / STAGE5_DIR}")
        else:
            print("[pain_insight] 未检测到 matplotlib，图表未生成。请先安装 matplotlib。")
        return
    raise ValueError("请指定 --run-folder 或 --all-sep 或 --all")


if __name__ == "__main__":
    main()
