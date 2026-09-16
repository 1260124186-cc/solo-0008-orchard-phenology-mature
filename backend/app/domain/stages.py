"""固定物候阶段定义。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..errors import ValidationError


@dataclass(frozen=True, slots=True)
class StageDefinition:
    key: str
    label: str
    rank: int
    required_for_completion: bool


@dataclass(frozen=True, slots=True)
class AbsenceReason:
    key: str
    label: str
    description: str
    # 只有“当年不适用”且附带依据时，才能作为必需阶段的完成依据；
    # “未观察到”和“仍在核实”始终表示工作尚未结束，不能绕过必需阶段。
    resolves_completion: bool


STAGES: tuple[StageDefinition, ...] = (
    StageDefinition("bud_swell", "芽膨大期", 10, False),
    StageDefinition("bud_burst", "萌芽期", 20, True),
    StageDefinition("first_bloom", "初花期", 30, False),
    StageDefinition("full_bloom", "盛花期", 40, True),
    StageDefinition("petal_fall", "落瓣期", 50, False),
    StageDefinition("fruit_set", "坐果期", 60, True),
    StageDefinition("fruit_growth", "果实膨大期", 70, False),
    StageDefinition("harvest", "采收期", 80, True),
    StageDefinition("leaf_fall", "落叶期", 90, False),
)

ABSENCE_REASONS: tuple[AbsenceReason, ...] = (
    AbsenceReason(
        "unobserved",
        "未观察到",
        "本季没有观察到该阶段，不代表阶段未发生",
        False,
    ),
    AbsenceReason(
        "not_applicable",
        "当年不适用",
        "有依据表明该阶段本季确实不发生，可作为完成依据",
        True,
    ),
    AbsenceReason(
        "pending_verification",
        "仍在核实",
        "观察结果尚待核实，完成前必须转为记录或给出结论",
        False,
    ),
)

ABSENCE_REASON_BY_KEY = {reason.key: reason for reason in ABSENCE_REASONS}

# “不适用”要替代一条真实观察成为完成依据，依据文字必须达到最低长度，
# 避免把标记当作绕过必需阶段的开关。
ABSENCE_BASIS_MINIMUM = 10
ABSENCE_BASIS_MAXIMUM = 300

STAGE_BY_KEY = {stage.key: stage for stage in STAGES}


def stage_definition(key: str) -> StageDefinition:
    normalized = str(key or "").strip().lower()
    try:
        return STAGE_BY_KEY[normalized]
    except KeyError as exc:
        raise ValidationError(
            "未知的物候阶段",
            field_name="stage",
            details={"stage": key, "allowed": list(STAGE_BY_KEY)},
        ) from exc


def sort_stage_entries(entries: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(
        entries,
        key=lambda item: (
            STAGE_BY_KEY.get(str(item.get("stage")), STAGES[-1]).rank,
            str(item.get("observed_on", "")),
        ),
    )


def required_stage_keys() -> set[str]:
    return {stage.key for stage in STAGES if stage.required_for_completion}


def stage_labels() -> dict[str, str]:
    return {stage.key: stage.label for stage in STAGES}


def absence_reason_definition(key: str) -> AbsenceReason:
    normalized = str(key or "").strip().lower()
    try:
        return ABSENCE_REASON_BY_KEY[normalized]
    except KeyError as exc:
        raise ValidationError(
            "未知的缺失原因",
            field_name="reason",
            details={"reason": key, "allowed": list(ABSENCE_REASON_BY_KEY)},
        ) from exc


def absence_reason_catalog() -> list[dict[str, object]]:
    return [
        {
            "key": reason.key,
            "label": reason.label,
            "description": reason.description,
            "resolves_completion": reason.resolves_completion,
        }
        for reason in ABSENCE_REASONS
    ]


def sort_absence_markers(markers: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(
        markers,
        key=lambda item: (
            STAGE_BY_KEY.get(str(item.get("stage")), STAGES[-1]).rank,
            str(item.get("created_at", "")),
        ),
    )
