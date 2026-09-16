"""已完成季节志的受控勘误规则。

勘误（correction）是独立实体，绝不改写季节志本体：原始条目永远保留在
``observation['entries']`` 中，作为完成时冻结的历史事实。勘误经过提议、采纳
后形成线性勘误链，链上的变更逐层叠加，派生出处方时点之后采用的“当前生效事
实”。拒绝与撤回只影响勘误自身，不改变历史结论。
"""

from __future__ import annotations

from typing import Any

from ..errors import PreconditionError, ValidationError
from .observation_rules import validate_date_window, validate_stage_sequence
from .plot_rules import new_identifier, now_iso
from .stages import STAGE_BY_KEY, sort_stage_entries, stage_definition
from .value_checks import (
    clean_confidence,
    clean_date,
    clean_text,
    reject_unknown_fields,
)

CORRECTION_STATUSES = ("proposed", "adopted", "rejected", "withdrawn")
ADOPTED = "adopted"
PROPOSED = "proposed"

CREATE_FIELDS = {"observation_id", "reason", "changes"}
CHANGE_FIELDS = {"stage", "observed_on", "confidence", "note"}
ADOPT_FIELDS = {"revision"}
DECIDE_FIELDS = {"revision", "note"}
WITHDRAW_FIELDS = {"revision"}


def frozen_entries(observation: dict[str, Any]) -> list[dict[str, Any]]:
    """季节志完成时冻结的原始事实。"""
    return [dict(item) for item in sort_stage_entries(observation.get("entries", []))]


def adopted_chain(
    corrections: dict[str, dict[str, Any]],
    observation_id: str,
) -> list[dict[str, Any]]:
    """按采纳顺序返回某季节志已采纳的勘误链。"""
    adopted = [
        item
        for item in corrections.values()
        if item["observation_id"] == observation_id and item["status"] == ADOPTED
    ]
    return sorted(
        adopted,
        key=lambda item: (
            int(item.get("adoption_seq") or 0),
            item["adopted_at"],
            item["id"],
        ),
    )


def current_correction_id(
    corrections: dict[str, dict[str, Any]],
    observation_id: str,
) -> str | None:
    chain = adopted_chain(corrections, observation_id)
    return chain[-1]["id"] if chain else None


def _apply_change(entry: dict[str, Any], change: dict[str, Any]) -> None:
    if "observed_on" in change:
        entry["observed_on"] = change["observed_on"]
    if "confidence" in change:
        entry["confidence"] = change["confidence"]
    if "note" in change:
        entry["note"] = change["note"]


def effective_entries(
    observation: dict[str, Any],
    corrections: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """原始条目叠加全部已采纳勘误，得到当前生效事实。"""
    entries = frozen_entries(observation)
    for correction in adopted_chain(corrections, observation["id"]):
        indexed = {item["stage"]: item for item in entries}
        for change in correction["changes"]:
            target = indexed.get(change["stage"])
            if target is not None:
                _apply_change(target, change)
    return sort_stage_entries(entries)


def build_correction(
    payload: dict[str, Any],
    observation: dict[str, Any],
    *,
    actor_id: str,
    timestamp: str,
) -> dict[str, Any]:
    reject_unknown_fields(payload, CREATE_FIELDS, label="勘误信息")
    if observation["status"] != "completed":
        raise PreconditionError(
            "season_not_completed",
            "只有已完成并冻结的季节志需要提交受控勘误",
        )
    observation_id = clean_text(
        payload.get("observation_id"),
        "observation_id",
        minimum=5,
        maximum=40,
    )
    if observation_id != observation["id"]:
        raise ValidationError("勘误季节志标识不匹配", field_name="observation_id")
    reason = clean_text(payload.get("reason"), "reason", minimum=4, maximum=300)
    raw_changes = payload.get("changes")
    if not isinstance(raw_changes, list) or not raw_changes:
        raise ValidationError(
            "勘误至少包含一条阶段变更",
            field_name="changes",
        )
    if len(raw_changes) > 9:
        raise ValidationError("单次勘误最多覆盖九个阶段", field_name="changes")

    stages_seen: set[str] = set()
    changes: list[dict[str, Any]] = []
    for raw_change in raw_changes:
        if not isinstance(raw_change, dict):
            raise ValidationError("勘误变更必须是对象", field_name="changes")
        reject_unknown_fields(raw_change, CHANGE_FIELDS, label="勘误变更")
        definition = stage_definition(str(raw_change.get("stage", "")))
        if definition.key in stages_seen:
            raise ValidationError(
                "同一阶段在一次勘误中只能出现一次",
                field_name="stage",
                details={"stage": definition.key},
            )
        stages_seen.add(definition.key)
        change: dict[str, Any] = {"stage": definition.key}
        if "observed_on" in raw_change:
            observed_on = clean_date(raw_change["observed_on"], "observed_on")
            validate_date_window(observed_on, observation["season"])
            change["observed_on"] = observed_on
        if "confidence" in raw_change:
            change["confidence"] = clean_confidence(raw_change["confidence"])
        if "note" in raw_change:
            change["note"] = clean_text(
                raw_change["note"],
                "note",
                maximum=300,
                required=False,
            )
        if len(change) == 1:
            raise ValidationError(
                "每条勘误变更至少修改日期、置信度或说明中的一项",
                field_name="changes",
                details={"stage": definition.key},
            )
        changes.append(change)

    # 勘误必须落在原始冻结阶段之上：不允许借勘误增删阶段，
    # 避免“两套同时有效的阶段”。
    frozen_index = {item["stage"]: item for item in frozen_entries(observation)}
    missing = sorted(stages_seen - set(frozen_index), key=lambda key: STAGE_BY_KEY[key].rank)
    if missing:
        raise PreconditionError(
            "correction_stage_not_recorded",
            "勘误只能修正季节志中已经冻结的阶段",
            stages=missing,
        )
    _ensure_changes_differ(changes, frozen_index)
    projected = _project_entries(observation, changes)
    validate_stage_sequence(projected)

    return {
        "id": new_identifier("corr"),
        "schema_version": 1,
        "observation_id": observation["id"],
        "season": observation["season"],
        "tree_id": observation["tree_id"],
        "plot_id": observation["plot_id"],
        "reason": reason,
        "changes": changes,
        "status": PROPOSED,
        "revision": 1,
        "proposed_by": actor_id,
        "proposed_at": timestamp,
        "supersedes_correction_id": None,
        "decided_by": None,
        "decided_at": None,
        "adopted_at": None,
        "decision_note": "",
        "adoption_seq": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _project_entries(
    observation: dict[str, Any],
    changes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    entries = frozen_entries(observation)
    indexed = {item["stage"]: item for item in entries}
    for change in changes:
        _apply_change(indexed[change["stage"]], change)
    return sort_stage_entries(entries)


def _ensure_changes_differ(
    changes: list[dict[str, Any]],
    frozen_index: dict[str, dict[str, Any]],
) -> None:
    for change in changes:
        original = frozen_index[change["stage"]]
        identical = all(
            change.get(key) == original.get(key)
            for key in ("observed_on", "confidence", "note")
            if key in change
        )
        if identical:
            raise ValidationError(
                "勘误内容与冻结事实完全相同，没有需要修正的项目",
                field_name="changes",
                details={"stage": change["stage"]},
            )


def adopt_correction(
    correction: dict[str, Any],
    observation: dict[str, Any],
    all_corrections: dict[str, dict[str, Any]],
    payload: dict[str, Any],
    *,
    actor_id: str,
    timestamp: str,
) -> dict[str, Any]:
    reject_unknown_fields(payload, ADOPT_FIELDS, label="采纳勘误")
    _verify_correction_revision(correction, payload)
    _require_status(correction, PROPOSED, "只有待决勘误可以采纳")
    previous_id = current_correction_id(all_corrections, observation["id"])

    # 在最新事实上重新校验，防止旧提议与已采纳勘误冲突时被悄悄套用。
    projected = frozen_entries(observation)
    for item in adopted_chain(all_corrections, observation["id"]):
        indexed = {entry["stage"]: entry for entry in projected}
        for change in item["changes"]:
            if change["stage"] in indexed:
                _apply_change(indexed[change["stage"]], change)
    frozen_index = {entry["stage"]: entry for entry in projected}
    _ensure_changes_differ(correction["changes"], frozen_index)
    indexed = {entry["stage"]: entry for entry in projected}
    for change in correction["changes"]:
        _apply_change(indexed[change["stage"]], change)
    validate_stage_sequence(sort_stage_entries(projected))

    return {
        **correction,
        "status": ADOPTED,
        "revision": int(correction["revision"]) + 1,
        "supersedes_correction_id": previous_id,
        "adoption_seq": len(
            [
                item
                for item in all_corrections.values()
                if item["observation_id"] == observation["id"]
                and item["status"] == ADOPTED
            ]
        )
        + 1,
        "decided_by": actor_id,
        "decided_at": timestamp,
        "adopted_at": timestamp,
        "updated_at": timestamp,
    }


def reject_correction(
    correction: dict[str, Any],
    payload: dict[str, Any],
    *,
    actor_id: str,
    timestamp: str,
) -> dict[str, Any]:
    reject_unknown_fields(payload, DECIDE_FIELDS, label="拒绝勘误")
    _verify_correction_revision(correction, payload)
    _require_status(correction, PROPOSED, "只有待决勘误可以拒绝")
    note = clean_text(
        payload.get("note", ""),
        "note",
        minimum=2,
        maximum=300,
    )
    return {
        **correction,
        "status": "rejected",
        "revision": int(correction["revision"]) + 1,
        "decided_by": actor_id,
        "decided_at": timestamp,
        "decision_note": note,
        "updated_at": timestamp,
    }


def withdraw_correction(
    correction: dict[str, Any],
    payload: dict[str, Any],
    *,
    actor_id: str,
    timestamp: str,
) -> dict[str, Any]:
    reject_unknown_fields(payload, WITHDRAW_FIELDS, label="撤回勘误")
    _verify_correction_revision(correction, payload)
    _require_status(correction, PROPOSED, "只有待决勘误可以撤回")
    return {
        **correction,
        "status": "withdrawn",
        "revision": int(correction["revision"]) + 1,
        "decided_by": actor_id,
        "decided_at": timestamp,
        "updated_at": timestamp,
    }


def _require_status(correction: dict[str, Any], expected: str, message: str) -> None:
    if correction["status"] != expected:
        raise PreconditionError(
            "correction_not_pending",
            message,
            correction_id=correction["id"],
            status=correction["status"],
        )


def _verify_correction_revision(
    correction: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    if "revision" not in payload:
        raise ValidationError("勘误操作需要 revision", field_name="revision")
    expected = payload["revision"]
    if isinstance(expected, bool):
        raise ValidationError("修订号必须是整数", field_name="revision")
    try:
        expected_int = int(expected)
    except (TypeError, ValueError) as exc:
        raise ValidationError("修订号必须是整数", field_name="revision") from exc
    if expected_int != int(correction["revision"]):
        raise PreconditionError(
            "revision_conflict",
            "勘误已被其他操作更新，请刷新后重试",
            current=correction["revision"],
            provided=expected_int,
        )


def correction_summary(
    correction: dict[str, Any],
    tree: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": correction["id"],
        "observation_id": correction["observation_id"],
        "season": correction["season"],
        "tree_id": correction["tree_id"],
        "plot_id": correction["plot_id"],
        "tree_code": tree["code"] if tree else "已移除",
        "cultivar": tree["cultivar"] if tree else "未知品种",
        "reason": correction["reason"],
        "changes": [dict(change) for change in correction["changes"]],
        "status": correction["status"],
        "revision": correction["revision"],
        "proposed_by": correction["proposed_by"],
        "proposed_at": correction["proposed_at"],
        "supersedes_correction_id": correction.get("supersedes_correction_id"),
        "decided_by": correction.get("decided_by"),
        "decided_at": correction.get("decided_at"),
        "adopted_at": correction.get("adopted_at"),
        "adoption_seq": correction.get("adoption_seq"),
        "decision_note": correction.get("decision_note", ""),
        "created_at": correction["created_at"],
        "updated_at": correction["updated_at"],
    }
