"""季节志与物候条目规则。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ..errors import ConflictError, PreconditionError, ValidationError
from .plot_rules import new_identifier, now_iso, verify_revision
from .stages import (
    ABSENCE_BASIS_MAXIMUM,
    ABSENCE_BASIS_MINIMUM,
    ABSENCE_REASONS,
    STAGES,
    STAGE_BY_KEY,
    absence_reason_definition,
    sort_absence_markers,
    sort_stage_entries,
    stage_definition,
)
from .value_checks import (
    clean_confidence,
    clean_date,
    clean_season,
    clean_text,
    reject_unknown_fields,
)


START_FIELDS = {"tree_id", "season", "observer", "note"}
STAGE_FIELDS = {"stage", "observed_on", "confidence", "note"}
ABSENCE_FIELDS = {"stage", "reason", "basis", "revision"}
COMPLETE_FIELDS = {"revision"}

# 任何缺失标记都必须写明依据；只有“不适用”的依据需要足以支撑完成结论。
ABSENCE_BASIS_REASON_MINIMUM = {
    reason.key: ABSENCE_BASIS_MINIMUM if reason.resolves_completion else 4
    for reason in ABSENCE_REASONS
}


def create_observation_record(
    payload: dict[str, Any],
    tree: dict[str, Any],
    timestamp: str,
) -> dict[str, Any]:
    reject_unknown_fields(payload, START_FIELDS, label="季节志信息")
    tree_id = clean_text(
        payload.get("tree_id"),
        "tree_id",
        minimum=5,
        maximum=40,
    )
    if tree_id != tree["id"]:
        raise ValidationError("季节志植株标识不匹配", field_name="tree_id")
    if tree["status"] != "active":
        raise PreconditionError(
            "tree_not_active",
            "只能为在册植株建立季节志",
        )
    return {
        "id": new_identifier("season"),
        "schema_version": 1,
        "tree_id": tree_id,
        "plot_id": tree["plot_id"],
        "season": clean_season(payload.get("season")),
        "observer": clean_text(
            payload.get("observer"),
            "observer",
            maximum=80,
        ),
        "note": clean_text(
            payload.get("note", ""),
            "note",
            maximum=500,
            required=False,
        ),
        "status": "open",
        "entries": [],
        "absence_markers": [],
        "completion_basis": None,
        "revision": 1,
        "created_at": timestamp,
        "updated_at": timestamp,
        "completed_at": None,
    }


def ensure_unique_open_observation(
    observations: dict[str, dict[str, Any]],
    tree_id: str,
    season: str,
    *,
    excluded_id: str | None = None,
) -> None:
    for observation in observations.values():
        if (
            observation["tree_id"] == tree_id
            and observation["season"] == season
            and observation["id"] != excluded_id
        ):
            raise ConflictError(
                "season_exists",
                "该植株在这一季节已有记录",
                existing_id=observation["id"],
                status=observation["status"],
            )


def add_stage_entry(
    observation: dict[str, Any],
    payload: dict[str, Any],
    *,
    expected_revision: int,
) -> dict[str, Any]:
    reject_unknown_fields(payload, STAGE_FIELDS, label="物候条目")
    ensure_observation_open(observation)
    verify_revision(observation, expected_revision)
    definition = stage_definition(str(payload.get("stage", "")))
    observed_on = clean_date(payload.get("observed_on"), "observed_on")
    validate_date_window(observed_on, observation["season"])
    confidence = clean_confidence(payload.get("confidence"))
    note = clean_text(
        payload.get("note", ""),
        "note",
        maximum=300,
        required=False,
    )
    existing = observation.get("entries", [])
    if any(item["stage"] == definition.key for item in existing):
        raise ConflictError(
            "stage_exists",
            "该阶段已经记录，请先移除原条目",
            stage=definition.key,
        )
    candidate_entries = [
        *existing,
        {
            "id": new_identifier("entry"),
            "stage": definition.key,
            "observed_on": observed_on,
            "confidence": confidence,
            "note": note,
            "created_at": now_iso(),
        },
    ]
    validate_stage_sequence(candidate_entries)
    # 一旦补录了真实观察，同阶段的缺失说明立即失效。
    remaining_markers = [
        marker
        for marker in observation.get("absence_markers", [])
        if marker["stage"] != definition.key
    ]
    return {
        **observation,
        "entries": sort_stage_entries(candidate_entries),
        "absence_markers": sort_absence_markers(remaining_markers),
        "revision": int(observation["revision"]) + 1,
        "updated_at": now_iso(),
    }


def put_absence_marker(
    observation: dict[str, Any],
    payload: dict[str, Any],
    *,
    expected_revision: int,
) -> dict[str, Any]:
    reject_unknown_fields(payload, ABSENCE_FIELDS, label="缺失说明")
    ensure_observation_open(observation)
    verify_revision(observation, expected_revision)
    definition = stage_definition(str(payload.get("stage", "")))
    reason = absence_reason_definition(str(payload.get("reason", "")))
    basis = clean_text(
        payload.get("basis", ""),
        "basis",
        minimum=ABSENCE_BASIS_REASON_MINIMUM[reason.key],
        maximum=ABSENCE_BASIS_MAXIMUM,
    )
    if any(item["stage"] == definition.key for item in observation.get("entries", [])):
        raise ConflictError(
            "stage_observed",
            "该阶段已有观察记录，无需登记缺失说明",
            stage=definition.key,
        )
    timestamp = now_iso()
    markers = [
        marker
        for marker in observation.get("absence_markers", [])
        if marker["stage"] != definition.key
    ]
    markers.append(
        {
            "id": new_identifier("absence"),
            "stage": definition.key,
            "reason": reason.key,
            "basis": basis,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
    )
    return {
        **observation,
        "absence_markers": sort_absence_markers(markers),
        "revision": int(observation["revision"]) + 1,
        "updated_at": timestamp,
    }


def remove_absence_marker(
    observation: dict[str, Any],
    stage: str,
    *,
    expected_revision: int,
) -> dict[str, Any]:
    ensure_observation_open(observation)
    verify_revision(observation, expected_revision)
    definition = stage_definition(stage)
    markers = observation.get("absence_markers", [])
    remaining = [item for item in markers if item["stage"] != definition.key]
    if len(remaining) == len(markers):
        raise ValidationError("季节志中没有该阶段的缺失说明", field_name="stage")
    return {
        **observation,
        "absence_markers": sort_absence_markers(remaining),
        "revision": int(observation["revision"]) + 1,
        "updated_at": now_iso(),
    }


def remove_stage_entry(
    observation: dict[str, Any],
    stage: str,
    *,
    expected_revision: int,
) -> dict[str, Any]:
    ensure_observation_open(observation)
    verify_revision(observation, expected_revision)
    definition = stage_definition(stage)
    entries = observation.get("entries", [])
    remaining = [item for item in entries if item["stage"] != definition.key]
    if len(remaining) == len(entries):
        raise ValidationError("季节志中没有该阶段", field_name="stage")
    return {
        **observation,
        "entries": sort_stage_entries(remaining),
        "revision": int(observation["revision"]) + 1,
        "updated_at": now_iso(),
    }


def completion_readiness(observation: dict[str, Any]) -> dict[str, Any]:
    """按缺失原因的真实含义计算完成就绪状态。

    observed：已有真实日期条目；
    not_applicable：有充分依据表明当年不发生，可作为必需阶段依据；
    unobserved / pending_verification：工作未结束，不能完成；
    unrecorded：既无条目也无说明。
    """
    entries = observation.get("entries", [])
    present = {item["stage"] for item in entries}
    markers = {
        item["stage"]: item for item in observation.get("absence_markers", [])
    }
    stages: list[dict[str, Any]] = []
    unresolved: list[dict[str, str]] = []
    resolved_by_absence = 0
    for definition in STAGES:
        if definition.key in present:
            state = "observed"
        elif definition.key in markers:
            marker = markers[definition.key]
            reason_key = marker["reason"]
            reason = absence_reason_definition(reason_key)
            if (
                reason.resolves_completion
                and len(str(marker.get("basis", "")).strip()) >= ABSENCE_BASIS_MINIMUM
            ):
                state = "not_applicable"
            elif reason.resolves_completion:
                # 依据不充分的“不适用”不能成为绕过必需阶段的开关。
                state = "basis_incomplete"
            else:
                state = reason_key
        else:
            state = "unrecorded"
        stages.append(
            {
                "stage": definition.key,
                "label": definition.label,
                "required": definition.required_for_completion,
                "state": state,
            }
        )
        if not definition.required_for_completion:
            continue
        if state == "observed":
            continue
        if state == "not_applicable":
            resolved_by_absence += 1
            continue
        unresolved.append({"stage": definition.key, "label": definition.label, "state": state})
    return {
        "stages": stages,
        "unresolved": unresolved,
        "resolved_by_absence": resolved_by_absence,
        "ready": not unresolved,
    }


def complete_observation_record(
    observation: dict[str, Any],
    *,
    expected_revision: int,
) -> dict[str, Any]:
    ensure_observation_open(observation)
    verify_revision(observation, expected_revision)
    entries = observation.get("entries", [])
    validate_stage_sequence(entries)
    readiness = completion_readiness(observation)
    if readiness["unresolved"]:
        blocking = readiness["unresolved"]
        labels = [
            f"{item['label']}（{_UNRESOLVED_LABELS[item['state']]}）" for item in blocking
        ]
        raise PreconditionError(
            "required_stages_missing",
            "缺少完成季节志所需的阶段",
            missing=[item["stage"] for item in blocking],
            labels=labels,
            unresolved=blocking,
        )
    timestamp = now_iso()
    basis = build_completion_basis(observation, readiness, timestamp)
    return {
        **observation,
        "entries": sort_stage_entries(entries),
        "status": "completed",
        "completion_basis": basis,
        "revision": int(observation["revision"]) + 1,
        "updated_at": timestamp,
        "completed_at": timestamp,
    }


_UNRESOLVED_LABELS = {
    "unrecorded": "尚未说明",
    "unobserved": "未观察到",
    "pending_verification": "仍在核实",
    "basis_incomplete": "不适用依据不足",
}


def build_completion_basis(
    observation: dict[str, Any],
    readiness: dict[str, Any],
    timestamp: str,
) -> dict[str, Any]:
    entries = {item["stage"]: item for item in observation.get("entries", [])}
    markers = {
        item["stage"]: item for item in observation.get("absence_markers", [])
    }
    stage_basis: list[dict[str, Any]] = []
    observed_count = 0
    not_applicable_count = 0
    for item in readiness["stages"]:
        key = item["stage"]
        resolution = {
            "stage": key,
            "label": item["label"],
            "required": item["required"],
            "state": item["state"],
        }
        if key in entries:
            resolution["observed_on"] = entries[key]["observed_on"]
            resolution["confidence"] = entries[key]["confidence"]
            if item["required"]:
                observed_count += 1
        elif key in markers:
            marker = markers[key]
            resolution["reason"] = marker["reason"]
            resolution["basis"] = marker["basis"]
            resolution["recorded_at"] = marker["created_at"]
            if item["state"] == "not_applicable" and item["required"]:
                not_applicable_count += 1
        stage_basis.append(resolution)
    required_total = sum(1 for stage in STAGES if stage.required_for_completion)
    if not_applicable_count:
        sentence = (
            f"完成依据：{required_total} 个必需阶段中 {observed_count} 个有实际观察，"
            f"{not_applicable_count} 个经依据确认当年不适用；"
            "未观察到或仍在核实的阶段不能替代观察。"
        )
    else:
        sentence = (
            f"完成依据：{required_total} 个必需阶段均有实际观察，"
            "缺失说明仅用于记录未观察阶段，不参与完成判定。"
        )
    return {
        "rule_version": 2,
        "legacy": False,
        "basis_text": sentence,
        "observed_required_count": observed_count,
        "not_applicable_required_count": not_applicable_count,
        "required_total": required_total,
        "stages": stage_basis,
        "frozen_at": timestamp,
    }


def ensure_observation_open(observation: dict[str, Any]) -> None:
    if observation["status"] != "open":
        raise PreconditionError(
            "season_not_editable",
            "已完成的季节志不可修改",
        )


def validate_date_window(observed_on: str, season: str) -> None:
    season_year = int(season)
    observed = date.fromisoformat(observed_on)
    start = date(season_year, 1, 1) - timedelta(days=90)
    end = date(season_year, 12, 31) + timedelta(days=90)
    if observed < start or observed > end:
        raise ValidationError(
            "观察日期超出该季节允许窗口",
            field_name="observed_on",
            details={
                "minimum": start.isoformat(),
                "maximum": end.isoformat(),
            },
        )


def validate_stage_sequence(entries: list[dict[str, Any]]) -> None:
    ordered = sort_stage_entries(entries)
    previous_rank = -1
    previous_date: date | None = None
    previous_label = ""
    for entry in ordered:
        definition = stage_definition(str(entry.get("stage", "")))
        observed = date.fromisoformat(clean_date(entry.get("observed_on"), "observed_on"))
        if definition.rank < previous_rank:
            raise ValidationError(
                "物候阶段顺序不合法",
                details={"previous": previous_label, "current": definition.label},
            )
        if previous_date is not None and observed < previous_date:
            raise ValidationError(
                "后一物候阶段的日期不能早于前一阶段",
                details={
                    "previous": previous_label,
                    "previous_date": previous_date.isoformat(),
                    "current": definition.label,
                    "current_date": observed.isoformat(),
                },
            )
        previous_rank = definition.rank
        previous_date = observed
        previous_label = definition.label


def observation_summary(
    observation: dict[str, Any],
    tree: dict[str, Any] | None,
) -> dict[str, Any]:
    entries = sort_stage_entries(observation.get("entries", []))
    markers = sort_absence_markers(observation.get("absence_markers", []))
    completed = observation["status"] == "completed"
    completion_basis = _completion_basis_view(observation, entries)
    readiness = None if completed else completion_readiness(observation)
    return {
        "id": observation["id"],
        "tree_id": observation["tree_id"],
        "plot_id": observation["plot_id"],
        "tree_code": tree["code"] if tree else "已移除",
        "cultivar": tree["cultivar"] if tree else "未知品种",
        "season": observation["season"],
        "observer": observation["observer"],
        "note": observation["note"],
        "status": observation["status"],
        "revision": observation["revision"],
        "created_at": observation["created_at"],
        "updated_at": observation["updated_at"],
        "completed_at": observation["completed_at"],
        "stage_count": len(entries),
        "entry_map": {
            item["stage"]: {
                "observed_on": item["observed_on"],
                "confidence": item["confidence"],
                "note": item["note"],
            }
            for item in entries
        },
        "entries": entries,
        "absence_markers": markers,
        "completion_readiness": readiness,
        "completion_basis": completion_basis,
    }


def _completion_basis_view(
    observation: dict[str, Any],
    entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """已完成季节志返回冻结的完成依据。

    特性上线前完成的旧季节志没有 completion_basis 快照；按“继续使用当时
    判断”的要求，不重新套用新规则，只标记为旧口径并原样解释，避免历史
    结论被改写。
    """
    if observation["status"] != "completed":
        return None
    frozen = observation.get("completion_basis")
    if frozen:
        return frozen
    present = sorted(
        (key for key in (item.get("stage") for item in entries) if key in STAGE_BY_KEY),
        key=lambda key: STAGE_BY_KEY[key].rank,
    )
    observed_required = sum(
        1 for key in present if STAGE_BY_KEY[key].required_for_completion
    )
    required_total = sum(1 for stage in STAGES if stage.required_for_completion)
    return {
        "rule_version": 1,
        "legacy": True,
        "basis_text": (
            "该季节志在缺失原因特性上线前完成，沿用当时的完成判断："
            f"萌芽期、盛花期、坐果期与采收期均有实际观察记录（{observed_required}/"
            f"{required_total}）；不对历史记录重新解释。"
        ),
        "observed_required_count": observed_required,
        "not_applicable_required_count": 0,
        "required_total": required_total,
        "stages": [
            {
                "stage": stage.key,
                "label": stage.label,
                "required": stage.required_for_completion,
                "state": "observed" if stage.key in set(present) else "unrecorded",
            }
            for stage in STAGES
        ],
        "frozen_at": observation.get("completed_at") or observation["updated_at"],
    }


def update_observation_note(
    observation: dict[str, Any],
    *,
    note: str,
    expected_revision: int,
) -> dict[str, Any]:
    ensure_observation_open(observation)
    verify_revision(observation, expected_revision)
    return {
        **observation,
        "note": clean_text(note, "note", maximum=500, required=False),
        "revision": int(observation["revision"]) + 1,
        "updated_at": now_iso(),
    }
