"""两份季节志的确定性对齐规则。"""

from __future__ import annotations

from statistics import mean
from typing import Any

from ..errors import PreconditionError, ValidationError
from .date_precision import (
    observed_date_from_entry,
    offset_between,
)
from .plot_rules import new_identifier, now_iso
from .stages import STAGE_BY_KEY, sort_stage_entries


def create_comparison_record(
    payload: dict[str, Any],
    left: dict[str, Any],
    right: dict[str, Any],
    left_tree: dict[str, Any] | None,
    right_tree: dict[str, Any] | None,
    timestamp: str,
) -> dict[str, Any]:
    left_id = str(payload.get("left_observation_id") or "").strip()
    right_id = str(payload.get("right_observation_id") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValidationError("请填写对比图谱标题", field_name="title")
    if len(title) > 100:
        raise ValidationError("对比图谱标题最多 100 个字符", field_name="title")
    if left_id == right_id:
        raise ValidationError("请选择两份不同的季节志", field_name="right_observation_id")
    ensure_comparable(left, right)
    offsets = calculate_stage_offsets(left, right)
    if not offsets:
        raise PreconditionError(
            "no_common_stage",
            "两份季节志没有可比较的共同阶段",
        )
    summary = build_summary(title, left, right, left_tree, right_tree, offsets)
    return {
        "id": new_identifier("atlas"),
        "schema_version": 1,
        "title": title,
        "season": left["season"],
        "left_observation_id": left["id"],
        "right_observation_id": right["id"],
        "left_tree_id": left["tree_id"],
        "right_tree_id": right["tree_id"],
        "left_label": label_for(left_tree),
        "right_label": label_for(right_tree),
        "stage_offsets": offsets,
        "summary": summary,
        "created_at": timestamp,
    }


def ensure_comparable(left: dict[str, Any], right: dict[str, Any]) -> None:
    if left["status"] != "completed" or right["status"] != "completed":
        raise PreconditionError(
            "season_not_completed",
            "只有已完成的季节志可以生成对比图谱",
        )
    if left["season"] != right["season"]:
        raise ValidationError(
            "两份季节志必须属于同一年份",
            field_name="season",
            details={"left": left["season"], "right": right["season"]},
        )


def calculate_stage_offsets(
    left: dict[str, Any],
    right: dict[str, Any],
) -> list[dict[str, Any]]:
    left_entries = {
        item["stage"]: item for item in sort_stage_entries(left.get("entries", []))
    }
    right_entries = {
        item["stage"]: item for item in sort_stage_entries(right.get("entries", []))
    }
    common = set(left_entries) & set(right_entries)
    ordered_common = sorted(common, key=lambda key: STAGE_BY_KEY[key].rank)
    result: list[dict[str, Any]] = []
    for key in ordered_common:
        left_entry = left_entries[key]
        right_entry = right_entries[key]
        left_observed = observed_date_from_entry(left_entry)
        right_observed = observed_date_from_entry(right_entry)
        offset_range = offset_between(left_observed, right_observed)
        row: dict[str, Any] = {
            "stage": key,
            "label": STAGE_BY_KEY[key].label,
            "rank": STAGE_BY_KEY[key].rank,
            "left_date": left_entry["observed_on"],
            "right_date": right_entry["observed_on"],
            "left_precision": left_observed.precision,
            "right_precision": right_observed.precision,
            "confidence_gap": abs(
                int(left_entry["confidence"]) - int(right_entry["confidence"])
            ),
        }
        if left_observed.precision == "range":
            row["left_end_date"] = left_entry["observed_end_on"]
        if right_observed.precision == "range":
            row["right_end_date"] = right_entry["observed_end_on"]
        if offset_range.is_exact:
            # 只有双方都能确定到同一偏移时才给出精确天数，绝不取区间中点。
            row["offset_days"] = offset_range.minimum
            row["offset_min_days"] = offset_range.minimum
            row["offset_max_days"] = offset_range.maximum
            row["offset_exact"] = True
        else:
            row["offset_min_days"] = offset_range.minimum
            row["offset_max_days"] = offset_range.maximum
            row["offset_exact"] = False
        result.append(row)
    return result


def build_summary(
    title: str,
    left: dict[str, Any],
    right: dict[str, Any],
    left_tree: dict[str, Any] | None,
    right_tree: dict[str, Any] | None,
    offsets: list[dict[str, Any]],
) -> dict[str, Any]:
    exact_values = [
        int(item["offset_days"]) for item in offsets if item.get("offset_exact")
    ]
    all_exact = len(exact_values) == len(offsets)
    lower_bounds = [
        item["offset_min_days"] for item in offsets if item["offset_min_days"] is not None
    ]
    upper_bounds = [
        item["offset_max_days"] for item in offsets if item["offset_max_days"] is not None
    ]
    all_lower_bounded = len(lower_bounds) == len(offsets)
    all_upper_bounded = len(upper_bounds) == len(offsets)
    # 只有每行在该侧都有界时，总体端点才可确定；任一行为开放区间则保留 None。
    range_minimum = min(lower_bounds) if all_lower_bounded else None
    range_maximum = max(upper_bounds) if all_upper_bounded else None

    earliest = _extreme_stage(offsets, side="minimum")
    latest = _extreme_stage(offsets, side="maximum")

    if all_exact:
        average: float | None = round(mean(exact_values), 1)
        direction = _exact_direction(average)
        consistent = max(exact_values) - min(exact_values) <= 10
        stability = (
            "阶段偏移较为集中" if consistent else "阶段偏移差异较大，建议复核记录"
        )
        offset_clause = f"平均偏移 {average:+.1f} 天，{direction}"
    else:
        average = None
        direction = _range_direction(
            range_minimum,
            range_maximum,
            all_lower_bounded=all_lower_bounded,
            all_upper_bounded=all_upper_bounded,
        )
        # 存在同名阶段两边同方向开放（都“不早于”或都“不晚于”），
        # 使整体偏移既无法给出下界也无法给出上界：方向待定，不虚构任何数字。
        fully_open = range_minimum is None and range_maximum is None
        if fully_open:
            stability = "存在同方向开放边界，偏移方向无法确认，建议补充观察"
            offset_clause = f"整体偏移方向待定，{direction}"
        else:
            stable_bounds = (
                range_minimum is not None
                and range_maximum is not None
                and range_maximum - range_minimum <= 10
            )
            if stable_bounds:
                stability = "阶段偏移区间较为集中"
            else:
                stability = "存在日期区间或边界记录，偏移范围较宽，建议复核记录"
            offset_clause = (
                f"整体偏移 {_format_offset_span(range_minimum, range_maximum)}，{direction}"
            )

    sentence = (
        f"{left_tree_label(left_tree)} 与 {right_tree_label(right_tree)} 在 "
        f"{left['season']} 年共有 {len(offsets)} 个阶段可比较，"
        f"{offset_clause}；{stability}。"
    )
    return {
        "title": title,
        "common_stage_count": len(offsets),
        "all_dates_exact": all_exact,
        "exact_stage_count": len(exact_values),
        "average_offset_days": average,
        "minimum_offset_days": min(exact_values) if all_exact else range_minimum,
        "maximum_offset_days": max(exact_values) if all_exact else range_maximum,
        "earliest_stage": earliest,
        "latest_stage": latest,
        "direction": direction,
        "stability": stability,
        "sentence": sentence,
    }


def _extreme_stage(offsets: list[dict[str, Any]], *, side: str) -> str:
    """在不虚构精确值的前提下命名最早 / 最晚阶段。

    候选阶段必须在相应方向有界，且其边界不被任何其他阶段的可能范围越过：
    最早阶段取最小上界，并要求不大于其余各行的下界；最晚阶段反之。
    所有行都精确时，这与历史的最小 / 最大偏移阶段一致。
    """

    if not offsets:
        return ""
    if side == "minimum":
        bounded = [item for item in offsets if item["offset_max_days"] is not None]
        if len(bounded) != len(offsets):
            return ""
        candidate = min(bounded, key=lambda item: item["offset_max_days"])
        target = int(candidate["offset_max_days"])
        for item in offsets:
            bound = item["offset_min_days"]
            if bound is None or int(bound) < target:
                return ""
        return str(candidate["label"])

    bounded = [item for item in offsets if item["offset_min_days"] is not None]
    if len(bounded) != len(offsets):
        return ""
    candidate = max(bounded, key=lambda item: item["offset_min_days"])
    target = int(candidate["offset_min_days"])
    for item in offsets:
        bound = item["offset_max_days"]
        if bound is None or int(bound) > target:
            return ""
    return str(candidate["label"])


def _exact_direction(average: float) -> str:
    if average <= -5:
        return "右侧植株整体偏早"
    if average >= 5:
        return "右侧植株整体偏晚"
    return "接近同步"


def _range_direction(
    range_minimum: int | None,
    range_maximum: int | None,
    *,
    all_lower_bounded: bool,
    all_upper_bounded: bool,
) -> str:
    if all_lower_bounded and range_minimum is not None and range_minimum >= 5:
        return "右侧植株整体偏晚"
    if all_upper_bounded and range_maximum is not None and range_maximum <= -5:
        return "右侧植株整体偏早"
    if all_lower_bounded and range_minimum is not None and range_minimum > 0:
        return "右侧植株可能偏晚"
    if all_upper_bounded and range_maximum is not None and range_maximum < 0:
        return "右侧植株可能偏早"
    return "早晚方向尚不能确定"


def _format_offset_span(minimum: int | None, maximum: int | None) -> str:
    def signed(value: int) -> str:
        if value == 0:
            return "0"
        return f"{value:+d}"

    if minimum is not None and maximum is not None:
        if minimum == maximum:
            return f"{signed(minimum)} 天"
        return f"{signed(minimum)} 到 {signed(maximum)} 天"
    if minimum is not None:
        return f"不小于 {signed(minimum)} 天"
    if maximum is not None:
        return f"不大于 {signed(maximum)} 天"
    return "无法界定"


def label_for(tree: dict[str, Any] | None) -> str:
    if tree is None:
        return "已移除植株"
    return f"{tree['code']} · {tree['cultivar']}"


def left_tree_label(tree: dict[str, Any] | None) -> str:
    return label_for(tree)


def right_tree_label(tree: dict[str, Any] | None) -> str:
    return label_for(tree)


def comparison_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "title": record["title"],
        "season": record["season"],
        "left_observation_id": record["left_observation_id"],
        "right_observation_id": record["right_observation_id"],
        "left_label": record["left_label"],
        "right_label": record["right_label"],
        "stage_offsets": record["stage_offsets"],
        "summary": record["summary"],
        "created_at": record["created_at"],
    }
