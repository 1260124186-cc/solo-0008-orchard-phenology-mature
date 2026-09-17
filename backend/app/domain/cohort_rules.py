"""同一植株跨年份的多年比较队列规则。

纳入口径：
- 纳入：年份内存在已完成的季节志，参与跨年统计。
- 排除：年份内存在未完成的季节志，记录不可用，不参与平均。
- 缺失：年份内没有任何季节志记录，不按零值计入。
阶段缺失与上述年份级原因分开标注，互不混用。
"""

from __future__ import annotations

from datetime import date
from statistics import mean
from typing import Any, Iterable

from ..errors import PreconditionError, ValidationError
from .plot_rules import new_identifier
from .stages import STAGES
from .value_checks import clean_season, clean_text, reject_unknown_fields


COHORT_FIELDS = {"title", "tree_id", "season_start", "season_end"}
MAX_SEASON_SPAN = 40

YEAR_STATUS_LABELS = {
    "included": "纳入",
    "excluded": "排除",
    "missing": "缺失",
}

REASON_TEXTS = {
    "included": "季节志已完成，纳入跨年统计",
    "record_incomplete": "季节志尚未完成，记录不可用，排除出统计",
    "missing_year": "该年份没有季节志记录",
    "missing_stage": "该年已纳入，但未记录此阶段",
}

TREND_LABELS = {
    "stable": "基本稳定",
    "earlier": "整体提前",
    "later": "整体推迟",
    "insufficient": "数据不足",
}


def create_cohort_record(
    payload: dict[str, Any],
    tree: dict[str, Any],
    observations: Iterable[dict[str, Any]],
    timestamp: str,
) -> dict[str, Any]:
    reject_unknown_fields(payload, COHORT_FIELDS, label="多年队列")
    title = clean_text(payload.get("title"), "title", maximum=100)
    tree_id = clean_text(payload.get("tree_id"), "tree_id", minimum=5, maximum=40)
    if tree_id != tree["id"]:
        raise ValidationError("多年队列植株标识不匹配", field_name="tree_id")
    season_start = clean_season(payload.get("season_start"))
    season_end = clean_season(payload.get("season_end"))
    if int(season_start) > int(season_end):
        raise ValidationError(
            "起始年份不能晚于结束年份",
            field_name="season_start",
            details={"season_start": season_start, "season_end": season_end},
        )
    span = int(season_end) - int(season_start) + 1
    if span > MAX_SEASON_SPAN:
        raise ValidationError(
            f"多年队列年份跨度不能超过 {MAX_SEASON_SPAN} 年",
            field_name="season_end",
            details={"maximum_span": MAX_SEASON_SPAN, "actual_span": span},
        )

    tree_observations = {
        item["season"]: item
        for item in observations
        if item["tree_id"] == tree["id"]
    }
    years = [
        classify_year(season, tree_observations.get(season))
        for season in (
            str(year) for year in range(int(season_start), int(season_end) + 1)
        )
    ]
    if not any(item["status"] == "included" for item in years):
        raise PreconditionError(
            "no_included_year",
            "该年份范围内没有已完成的季节志，无法建立多年队列",
        )
    stage_series = build_stage_series(years, tree_observations)
    tree_label = f"{tree['code']} · {tree['cultivar']}"
    summary = build_summary(
        title,
        tree_label,
        season_start,
        season_end,
        years,
        stage_series,
    )
    return {
        "id": new_identifier("cohort"),
        "schema_version": 1,
        "title": title,
        "tree_id": tree["id"],
        "tree_code": tree["code"],
        "cultivar": tree["cultivar"],
        "tree_label": tree_label,
        "season_start": season_start,
        "season_end": season_end,
        "years": years,
        "stage_series": stage_series,
        "summary": summary,
        "created_at": timestamp,
    }


def classify_year(
    season: str,
    observation: dict[str, Any] | None,
) -> dict[str, Any]:
    if observation is None:
        return {
            "season": season,
            "status": "missing",
            "reason_code": "missing_year",
            "reason": REASON_TEXTS["missing_year"],
            "observation_id": None,
            "stage_count": 0,
        }
    if observation["status"] != "completed":
        return {
            "season": season,
            "status": "excluded",
            "reason_code": "record_incomplete",
            "reason": REASON_TEXTS["record_incomplete"],
            "observation_id": observation["id"],
            "stage_count": len(observation.get("entries", [])),
        }
    return {
        "season": season,
        "status": "included",
        "reason_code": "included",
        "reason": REASON_TEXTS["included"],
        "observation_id": observation["id"],
        "stage_count": len(observation.get("entries", [])),
    }


def build_stage_series(
    years: list[dict[str, Any]],
    tree_observations: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    included_seasons = [
        item["season"] for item in years if item["status"] == "included"
    ]
    series: list[dict[str, Any]] = []
    for definition in STAGES:
        points: list[dict[str, Any]] = []
        missing_seasons: list[str] = []
        for season in included_seasons:
            observation = tree_observations[season]
            entry = next(
                (
                    item
                    for item in observation.get("entries", [])
                    if item["stage"] == definition.key
                ),
                None,
            )
            if entry is None:
                missing_seasons.append(season)
                continue
            points.append(
                {
                    "season": season,
                    "observed_on": entry["observed_on"],
                    "season_day": season_day(season, entry["observed_on"]),
                    "confidence": int(entry["confidence"]),
                }
            )
        comparable = len(points) >= 2
        series.append(
            {
                "stage": definition.key,
                "label": definition.label,
                "rank": definition.rank,
                "points": points,
                "missing_seasons": missing_seasons,
                "comparable": comparable,
                "average_season_day": (
                    round(mean(point["season_day"] for point in points), 1)
                    if comparable
                    else None
                ),
                "min_season_day": (
                    min((point["season_day"] for point in points), default=None)
                    if points
                    else None
                ),
                "max_season_day": (
                    max((point["season_day"] for point in points), default=None)
                    if points
                    else None
                ),
                "deltas": build_deltas(points),
                "trend": trend_for(points),
            }
        )
    return series


def build_deltas(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deltas: list[dict[str, Any]] = []
    for previous, current in zip(points, points[1:]):
        deltas.append(
            {
                "from_season": previous["season"],
                "to_season": current["season"],
                "span_years": int(current["season"]) - int(previous["season"]),
                "days": int(current["season_day"]) - int(previous["season_day"]),
            }
        )
    return deltas


def trend_for(points: list[dict[str, Any]]) -> str:
    if len(points) < 2:
        return TREND_LABELS["insufficient"]
    shift = int(points[-1]["season_day"]) - int(points[0]["season_day"])
    if abs(shift) <= 3:
        return TREND_LABELS["stable"]
    if shift < 0:
        return TREND_LABELS["earlier"]
    return TREND_LABELS["later"]


def season_day(season: str, observed_on: str) -> int:
    """观察日期相对季节年 1 月 1 日的第几天，可为负或超过 365。"""
    observed = date.fromisoformat(observed_on)
    return (observed - date(int(season), 1, 1)).days + 1


def build_summary(
    title: str,
    tree_label: str,
    season_start: str,
    season_end: str,
    years: list[dict[str, Any]],
    stage_series: list[dict[str, Any]],
) -> dict[str, Any]:
    included = sum(1 for item in years if item["status"] == "included")
    excluded = sum(1 for item in years if item["status"] == "excluded")
    missing = sum(1 for item in years if item["status"] == "missing")
    comparable = sum(1 for item in stage_series if item["comparable"])
    sentence = (
        f"{tree_label} 在 {season_start}—{season_end} 年共 {len(years)} 个年份："
        f"纳入 {included} 年、排除 {excluded} 年、缺失 {missing} 年。"
        "缺失年份不按零值计入，排除年份不参与平均；"
        f"{len(stage_series)} 个阶段中 {comparable} 个具备跨年可比性。"
    )
    return {
        "title": title,
        "season_start": season_start,
        "season_end": season_end,
        "included_count": included,
        "excluded_count": excluded,
        "missing_count": missing,
        "comparable_stage_count": comparable,
        "sentence": sentence,
    }


def cohort_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "title": record["title"],
        "tree_id": record["tree_id"],
        "tree_code": record["tree_code"],
        "cultivar": record["cultivar"],
        "tree_label": record["tree_label"],
        "season_start": record["season_start"],
        "season_end": record["season_end"],
        "years": record["years"],
        "stage_series": record["stage_series"],
        "summary": record["summary"],
        "created_at": record["created_at"],
    }


def build_cohort_export(record: dict[str, Any]) -> dict[str, Any]:
    summary = record["summary"]
    lines = [
        record["title"],
        "",
        f"对象：{record['tree_label']}",
        f"年份范围：{record['season_start']}—{record['season_end']}",
        f"生成时间：{record['created_at']}",
        "",
        "纳入口径",
        f"- 纳入 {summary['included_count']} 年：季节志已完成，参与跨年统计。",
        f"- 排除 {summary['excluded_count']} 年：季节志未完成，记录不可用，不参与平均。",
        f"- 缺失 {summary['missing_count']} 年：该年份没有季节志记录，不按零值计入。",
        "- 不足两个可比年份的阶段不计算均值，不做强行平均。",
        "",
        "年度明细",
    ]
    for year in record["years"]:
        status = YEAR_STATUS_LABELS[year["status"]]
        lines.append(f"{year['season']}｜{status}｜{year['reason']}")
    lines.extend(["", "阶段序列（仅统计纳入年份）"])
    for stage in record["stage_series"]:
        if stage["comparable"]:
            lines.append(
                f"{stage['label']}｜可比 {len(stage['points'])} 年｜"
                f"平均季节年第 {stage['average_season_day']} 天｜{stage['trend']}"
            )
        else:
            lines.append(
                f"{stage['label']}｜数据不足（{len(stage['points'])} 个可比年份），"
                "不计算均值"
            )
        for point in stage["points"]:
            lines.append(
                f"  {point['season']}：{point['observed_on']}"
                f"（季节年第 {point['season_day']} 天，置信 {point['confidence']}/5）"
            )
        for season in stage["missing_seasons"]:
            lines.append(f"  {season} 年：{REASON_TEXTS['missing_stage']}")
    return {
        "filename": (
            f"{record['tree_code']}-{record['season_start']}"
            f"-{record['season_end']}-多年队列.txt"
        ),
        "media_type": "text/plain;charset=utf-8",
        "content": "\n".join(lines) + "\n",
    }
