"""已完成季节志的受控勘误规则。

勘误（correction）是独立实体，绝不改写季节志本体：原始条目永远保留在
``observation['entries']`` 中，作为完成时冻结的历史事实。勘误经过提议、采纳
后形成线性勘误链，链上的变更逐层叠加，派生出处方时点之后采用的“当前生效事
实”。拒绝与撤回只影响勘误自身，不改变历史结论。

勘误变更有两种受控形式：

- ``modify``：修正某个当前阶段的日期、置信度或说明，阶段身份不变；
- ``replace``：把误录阶段整体替换为正确阶段。误录阶段从当前事实中移除，
  正确阶段以同一现场观察的身份进入当前轨道，且只出现一次；原始阶段与其日
  期仍完整保留在冻结事实与谱系中。
"""

from __future__ import annotations

from typing import Any

from ..errors import PreconditionError, ValidationError
from .observation_rules import validate_date_window, validate_stage_sequence
from .plot_rules import new_identifier, now_iso
from .stages import (
    STAGE_BY_KEY,
    required_stage_keys,
    sort_stage_entries,
    stage_definition,
)
from .value_checks import (
    clean_confidence,
    clean_date,
    clean_text,
    reject_unknown_fields,
)

CORRECTION_STATUSES = ("proposed", "adopted", "rejected", "withdrawn")
ADOPTED = "adopted"
PROPOSED = "proposed"

MODIFY = "modify"
REPLACE = "replace"
CHANGE_TYPES = (MODIFY, REPLACE)

CREATE_FIELDS = {"observation_id", "reason", "changes"}
MODIFY_FIELDS = {"change_type", "stage", "observed_on", "confidence", "note"}
REPLACE_FIELDS = {
    "change_type",
    "stage",
    "correct_stage",
    "observed_on",
    "confidence",
    "note",
}
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


def _derived_entry_id(correction_id: str, stage: str) -> str:
    return f"entryr_{correction_id[-10:]}_{stage}"


def _apply_modify(entry: dict[str, Any], change: dict[str, Any]) -> None:
    if "observed_on" in change:
        entry["observed_on"] = change["observed_on"]
    if "confidence" in change:
        entry["confidence"] = change["confidence"]
    if "note" in change:
        entry["note"] = change["note"]


def _apply_change_set(
    entries: dict[str, dict[str, Any]],
    changes: list[dict[str, Any]],
    *,
    correction_id: str,
    correction_timestamp: str,
    adoption_seq: int,
) -> list[dict[str, Any]]:
    """把一条勘误的全部变更叠加到工作集，返回本次产生的替换跳。

    每个替换跳记录移出阶段、进入阶段、进入日期、勘误标识与其在勘误链中的
    序号；连续替换时中间阶段既是某次的“进入”，也是下一次的“移出”。
    """
    hops: list[dict[str, Any]] = []
    for change in changes:
        if change.get("change_type", MODIFY) == MODIFY:
            if change["stage"] not in entries:
                raise PreconditionError(
                    "correction_change_stale",
                    "勘误针对的阶段已被其他勘误替换，请基于当前事实重新提议",
                    stage=change["stage"],
                )
            _apply_modify(entries[change["stage"]], change)
            continue

        source_key = change["stage"]
        target_key = change["correct_stage"]
        if source_key not in entries:
            raise PreconditionError(
                "correction_change_stale",
                "待替换的误录阶段已不在当前事实中，请基于当前事实重新提议",
                stage=source_key,
            )
        if target_key in entries:
            raise PreconditionError(
                "correction_target_stage_exists",
                "正确阶段在当前事实中已经存在，不能形成同一阶段的两条记录",
                stage=target_key,
            )
        source = entries.pop(source_key)
        entries[target_key] = {
            "id": _derived_entry_id(correction_id, target_key),
            "stage": target_key,
            "observed_on": change["observed_on"],
            "confidence": change.get("confidence", source["confidence"]),
            "note": change.get("note", source.get("note", "")),
            "created_at": correction_timestamp,
        }
        hops.append(
            {
                "from_stage": source_key,
                "to_stage": target_key,
                "observed_on": change["observed_on"],
                "confidence": entries[target_key]["confidence"],
                "note": entries[target_key].get("note", ""),
                "correction_id": correction_id,
                "adoption_seq": adoption_seq,
                "adopted_at": correction_timestamp,
            }
        )
    return hops


def replay(
    observation: dict[str, Any],
    corrections: dict[str, dict[str, Any]],
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从冻结事实回放勘误链，得到当前生效事实与完整谱系信息。

    返回：
    - ``entries``：当前阶段条目；
    - ``hops``：按采纳顺序排列的全部阶段替换跳（含中间阶段的进出）；
    - ``replaced_to`` / ``replaced_from``：阶段去向/来源映射；
    - ``last_change``：每个当前阶段最近一次修改它的勘误。
    """
    entries = {item["stage"]: dict(item) for item in frozen_entries(observation)}
    hops: list[dict[str, Any]] = []
    replaced_to: dict[str, str] = {}
    replaced_from: dict[str, str] = {}
    last_change: dict[str, str] = {}
    for correction in adopted_chain(corrections, observation["id"]):
        new_hops = _apply_change_set(
            entries,
            correction["changes"],
            correction_id=correction["id"],
            correction_timestamp=correction.get("adopted_at")
            or correction["proposed_at"],
            adoption_seq=int(correction.get("adoption_seq") or 0),
        )
        hops.extend(new_hops)
        for hop in new_hops:
            replaced_to[hop["from_stage"]] = hop["to_stage"]
            replaced_from[hop["to_stage"]] = hop["from_stage"]
        for change in correction["changes"]:
            if change.get("change_type", MODIFY) == REPLACE:
                last_change[change["correct_stage"]] = correction["id"]
            else:
                last_change[change["stage"]] = correction["id"]
    if extra is not None:
        extra_seq = 1 + max(
            (int(item.get("adoption_seq") or 0) for item in hops),
            default=0,
        )
        _apply_change_set(
            entries,
            extra["changes"],
            correction_id=extra["id"],
            correction_timestamp=extra.get("proposed_at") or now_iso(),
            adoption_seq=extra_seq,
        )
    return {
        "entries": sort_stage_entries(list(entries.values())),
        "hops": hops,
        "replaced_to": replaced_to,
        "replaced_from": replaced_from,
        "last_change": last_change,
    }


def effective_entries(
    observation: dict[str, Any],
    corrections: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """原始条目叠加全部已采纳勘误，得到当前生效事实。"""
    return replay(observation, corrections)["entries"]


def build_correction(
    payload: dict[str, Any],
    observation: dict[str, Any],
    corrections: dict[str, dict[str, Any]],
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

    # 提议针对“当前事实”（冻结事实叠加既有勘误链），而不是只针对冻结事实。
    baseline = replay(observation, corrections)
    baseline_index = {item["stage"]: item for item in baseline["entries"]}
    changes: list[dict[str, Any]] = []
    sources_seen: set[str] = set()
    targets_seen: set[str] = set()

    for raw_change in raw_changes:
        if not isinstance(raw_change, dict):
            raise ValidationError("勘误变更必须是对象", field_name="changes")
        change_type = str(raw_change.get("change_type", MODIFY))
        if change_type not in CHANGE_TYPES:
            raise ValidationError(
                "勘误类型只能是 modify 或 replace",
                field_name="change_type",
            )
        allowed = REPLACE_FIELDS if change_type == REPLACE else MODIFY_FIELDS
        reject_unknown_fields(raw_change, allowed, label="勘误变更")
        source = stage_definition(str(raw_change.get("stage", "")))
        if source.key in sources_seen:
            raise ValidationError(
                "同一误录阶段在一次勘误中只能处理一次",
                field_name="stage",
                details={"stage": source.key},
            )
        sources_seen.add(source.key)

        if change_type == MODIFY:
            changes.append(
                _parse_modify(raw_change, source.key, baseline_index, observation)
            )
            targets_seen.add(source.key)
            continue

        target_definition = stage_definition(
            str(raw_change.get("correct_stage", ""))
        )
        if target_definition.key == source.key:
            raise ValidationError(
                "替换后的正确阶段必须与误录阶段不同",
                field_name="correct_stage",
            )
        if target_definition.key in targets_seen:
            raise ValidationError(
                "同一正确阶段在一次勘误中只能出现一次",
                field_name="correct_stage",
                details={"stage": target_definition.key},
            )
        targets_seen.add(target_definition.key)
        if source.key not in baseline_index:
            raise PreconditionError(
                "correction_stage_not_recorded",
                "勘误只能修正当前事实中存在的阶段",
                stages=[source.key],
            )
        if target_definition.key in baseline_index:
            raise PreconditionError(
                "correction_target_stage_exists",
                "正确阶段在当前事实中已经存在，不能形成同一阶段的两条记录",
                stages=[target_definition.key],
            )
        observed_on = clean_date(raw_change.get("observed_on"), "observed_on")
        validate_date_window(observed_on, observation["season"])
        change: dict[str, Any] = {
            "change_type": REPLACE,
            "stage": source.key,
            "correct_stage": target_definition.key,
            "observed_on": observed_on,
        }
        if "confidence" in raw_change:
            change["confidence"] = clean_confidence(raw_change["confidence"])
        if "note" in raw_change:
            change["note"] = clean_text(
                raw_change["note"],
                "note",
                maximum=300,
                required=False,
            )
        changes.append(change)

    projected = _trial_projection(observation, corrections, {
        "id": new_identifier("corr"),
        "proposed_at": timestamp,
        "changes": changes,
    })
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


def _parse_modify(
    raw_change: dict[str, Any],
    stage: str,
    baseline_index: dict[str, dict[str, Any]],
    observation: dict[str, Any],
) -> dict[str, Any]:
    if stage not in baseline_index:
        raise PreconditionError(
            "correction_stage_not_recorded",
            "勘误只能修正当前事实中存在的阶段",
            stages=[stage],
        )
    change: dict[str, Any] = {"change_type": MODIFY, "stage": stage}
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
    if len(change) == 2:
        raise ValidationError(
            "每条勘误变更至少修改日期、置信度或说明中的一项",
            field_name="changes",
            details={"stage": stage},
        )
    original = baseline_index[stage]
    if all(
        change.get(key) == original.get(key)
        for key in ("observed_on", "confidence", "note")
        if key in change
    ):
        raise ValidationError(
            "勘误内容与当前事实完全相同，没有需要修正的项目",
            field_name="changes",
            details={"stage": stage},
        )
    return change


def _trial_projection(
    observation: dict[str, Any],
    corrections: dict[str, dict[str, Any]],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    result = replay(observation, corrections, extra=candidate)
    projected = result["entries"]
    present = {item["stage"] for item in projected}
    missing_required = sorted(
        required_stage_keys() - present,
        key=lambda key: STAGE_BY_KEY[key].rank,
    )
    if missing_required:
        raise PreconditionError(
            "correction_required_stage_removed",
            "替换阶段不能移除完成季节志所必需的阶段，请选择同样属于必需阶段的正确阶段",
            stages=missing_required,
        )
    validate_stage_sequence(projected)
    return result


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

    # 在最新事实上重新回放并校验，防止旧提议与已采纳勘误冲突时被悄悄套用。
    _trial_projection(observation, all_corrections, correction)

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
