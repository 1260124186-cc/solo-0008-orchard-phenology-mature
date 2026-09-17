"""同一植株跨年份的多年比较规则。

多年比较把范围年份区分为纳入、排除和缺失三种处置，
阶段平均与年际变化只基于纳入年份计算，缺失年份不按零值处理，
不可比年份不参与平均。
"""

from __future__ import annotations

from datetime import date
from statistics import mean
from typing import Any

from ..errors import PreconditionError, ValidationError
from .plot_rules import new_identifier
from .stages import STAGES, sort_stage_entries
from .value_checks import clean_season, clean_text, reject_unknown_fields


SERIES_FIELDS = {"title", "tree_id", "season_from", "season_to"}
MAX_SERIES_SPAN = 30
MIN_INCLUDED_YEARS = 2

DISPOSITION_INCLUDED = "included"
DISPOSITION_EXCLUDED = "excluded"
DISPOSITION_MISSING = "missing"

INCLUSION_CRITERIA: tuple[str, ...] = (
    "仅纳入状态为已完成的季节志年份",
    "季节志尚未完成的年份标记为排除，不参与任何统计",
    "没有季节志记录的年份标记为缺失，不按零值处理",
    "纳入年份中缺少的阶段按阶段剔除，不补零、不推断日期",
    "阶段平均与年际变化只在纳入年份之间计算",
)


def create_series_record(
    payload: dict[str, Any],
    tree: dict[str, Any],
    observations: list[dict[str, Any]],
    timestamp: str,
) -> dict[str, Any]:
    reject_unknown_fields(payload, SERIES_FIELDS, label="多年比较信息")
    title = clean_text(payload.get("title"), "title", maximum=100)
    tree_id = clean_text(payload.get("tree_id"), "tree_id", minimum=5, maximum=40)
    if tree_id != tree["id"]:
        raise ValidationError("多年比较植株标识不匹配", field_name="tree_id")
    season_from = clean_season(payload.get("season_from"))
    season_to = clean_season(payload.get("season_to"))
    if int(season_from) > int(season_to):
        raise ValidationError(
            "起始年份不能晚于结束年份",
            field_name="season_from",
            details={"season_from": season_from, "season_to": season_to},
        )
    span = int(season_to) - int(season_from) + 1
    if span > MAX_SERIES_SPAN:
        raise ValidationError(
            f"多年比较的年份跨度最多 {MAX_SERIES_SPAN} 年",
            field_name="season_to",
            details={"span": span, "maximum": MAX_SERIES_SPAN},
        )
    years = build_year_dispositions(tree, observations, season_from, season_to)
    included = [item for item in years if item["disposition"] == DISPOSITION_INCLUDED]
    if len(included) < MIN_INCLUDED_YEARS:
        raise PreconditionError(
            "insufficient_included_years",
            "范围内可比较的已完成年份不足两个",
            included=len(included),
            minimum=MIN_INCLUDED_YEARS,
        )
    stage_series = build_stage_series(years, observations)
    summary = build_series_summary(title, season_from, season_to, years)
    return {
        "id": new_identifier("series"),
        "schema_version": 1,
        "title": title,
        "tree_id": tree["id"],
        "plot_id": tree["plot_id"],
        "tree_label": f"{tree['code']} · {tree['cultivar']}",
        "season_from": season_from,
        "season_to": season_to,
        "criteria": list(INCLUSION_CRITERIA),
        "years": years,
        "stage_series": stage_series,
        "summary": summary,
        "created_at": timestamp,
    }


def build_year_dispositions(
    tree: dict[str, Any],
    observations: list[dict[str, Any]],
    season_from: str,
    season_to: str,
) -> list[dict[str, Any]]:
    by_season = {
        item["season"]: item
        for item in observations
        if item["tree_id"] == tree["id"]
    }
    years: list[dict[str, Any]] = []
    for value in range(int(season_from), int(season_to) + 1):
        season = str(value)
        observation = by_season.get(season)
        if observation is None:
            years.append(
                {
                    "season": season,
                    "disposition": DISPOSITION_MISSING,
                    "reason_code": "season_absent",
                    "reason": "该年份没有季节志记录，不按零值处理",
                    "observation_id": None,
                    "stage_count": None,
                    "missing_stages": None,
                }
            )
            continue
        if observation["status"] != "completed":
            years.append(
                {
                    "season": season,
                    "disposition": DISPOSITION_EXCLUDED,
                    "reason_code": "season_not_completed",
                    "reason": "季节志尚未完成，不参与比较",
                    "observation_id": observation["id"],
                    "stage_count": len(observation.get("entries", [])),
                    "missing_stages": None,
                }
            )
            continue
        recorded = {entry["stage"] for entry in observation.get("entries", [])}
        missing_stages = [
            stage.key for stage in STAGES if stage.key not in recorded
        ]
        years.append(
            {
                "season": season,
                "disposition": DISPOSITION_INCLUDED,
                "reason_code": "completed_season",
                "reason": "已完成季节志，纳入比较",
                "observation_id": observation["id"],
                "stage_count": len(recorded),
                "missing_stages": missing_stages,
            }
        )
    return years


def build_stage_series(
    years: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    observation_by_id = {item["id"]: item for item in observations}
    included_observations: list[tuple[str, dict[str, Any]]] = []
    for year in years:
        if year["disposition"] != DISPOSITION_INCLUDED:
            continue
        observation = observation_by_id.get(str(year["observation_id"]))
        if observation is not None:
            included_observations.append((year["season"], observation))
    rows: list[dict[str, Any]] = []
    for stage in STAGES:
        points: list[dict[str, Any]] = []
        covered: list[str] = []
        missing: list[str] = []
        for season, observation in included_observations:
            entries = {
                entry["stage"]: entry
                for entry in sort_stage_entries(observation.get("entries", []))
            }
            entry = entries.get(stage.key)
            if entry is None:
                missing.append(season)
                continue
            observed = date.fromisoformat(entry["observed_on"])
            points.append(
                {
                    "season": season,
                    "observed_on": observed.isoformat(),
                    "day_of_year": observed.timetuple().tm_yday,
                }
            )
            covered.append(season)
        day_values = [int(point["day_of_year"]) for point in points]
        rows.append(
            {
                "stage": stage.key,
                "label": stage.label,
                "rank": stage.rank,
                "points": points,
                "covered_seasons": covered,
                "missing_seasons": missing,
                "average_day_of_year": (
                    round(mean(day_values), 1) if day_values else None
                ),
                "span_days": (
                    max(day_values) - min(day_values)
                    if len(day_values) >= 2
                    else None
                ),
                "first_season": covered[0] if covered else None,
                "last_season": covered[-1] if covered else None,
                "shift_days": (
                    day_values[-1] - day_values[0]
                    if len(day_values) >= 2
                    else None
                ),
            }
        )
    return rows


def build_series_summary(
    title: str,
    season_from: str,
    season_to: str,
    years: list[dict[str, Any]],
) -> dict[str, Any]:
    included = [
        item["season"] for item in years if item["disposition"] == DISPOSITION_INCLUDED
    ]
    excluded = [
        item["season"] for item in years if item["disposition"] == DISPOSITION_EXCLUDED
    ]
    missing = [
        item["season"] for item in years if item["disposition"] == DISPOSITION_MISSING
    ]
    sentence = (
        f"{season_from}–{season_to} 年共评估 {len(years)} 个年份："
        f"纳入 {len(included)} 个（{_season_text(included)}），"
        f"排除 {len(excluded)} 个（{_season_text(excluded)}），"
        f"缺失 {len(missing)} 个（{_season_text(missing)}）。"
        "阶段平均与年际变化只基于纳入年份计算，排除与缺失年份不按零值处理。"
    )
    return {
        "title": title,
        "evaluated_year_count": len(years),
        "included_count": len(included),
        "excluded_count": len(excluded),
        "missing_count": len(missing),
        "included_seasons": included,
        "excluded_seasons": excluded,
        "missing_seasons": missing,
        "sentence": sentence,
    }


def series_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "title": record["title"],
        "tree_id": record["tree_id"],
        "plot_id": record["plot_id"],
        "tree_label": record["tree_label"],
        "season_from": record["season_from"],
        "season_to": record["season_to"],
        "criteria": record["criteria"],
        "years": record["years"],
        "stage_series": record["stage_series"],
        "summary": record["summary"],
        "created_at": record["created_at"],
    }


def _season_text(seasons: list[str]) -> str:
    return "、".join(seasons) if seasons else "无"
