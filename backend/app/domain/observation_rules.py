"""季节志与物候条目规则。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ..errors import ConflictError, PreconditionError, ValidationError
from .plot_rules import new_identifier, now_iso, verify_revision
from .stages import (
    STAGES,
    STAGE_BY_KEY,
    required_stage_keys,
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
COMPLETE_FIELDS = {"revision"}


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
    return {
        **observation,
        "entries": sort_stage_entries(candidate_entries),
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


def complete_observation_record(
    observation: dict[str, Any],
    *,
    expected_revision: int,
) -> dict[str, Any]:
    ensure_observation_open(observation)
    verify_revision(observation, expected_revision)
    entries = observation.get("entries", [])
    present = {item["stage"] for item in entries}
    missing = sorted(
        required_stage_keys() - present,
        key=lambda key: STAGE_BY_KEY[key].rank,
    )
    if missing:
        labels = [STAGE_BY_KEY[key].label for key in missing]
        raise PreconditionError(
            "required_stages_missing",
            "缺少完成季节志所需的阶段",
            missing=missing,
            labels=labels,
        )
    validate_stage_sequence(entries)
    timestamp = now_iso()
    return {
        **observation,
        "entries": sort_stage_entries(entries),
        "status": "completed",
        "revision": int(observation["revision"]) + 1,
        "updated_at": timestamp,
        "completed_at": timestamp,
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


def _entry_lineage(
    observation: dict[str, Any],
    corrections: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    from .correction_rules import replay

    frozen = sort_stage_entries(observation.get("entries", []))
    frozen_index = {item["stage"]: item for item in frozen}
    result = replay(observation, corrections)
    effective_index = {item["stage"]: item for item in result["entries"]}
    hops = result["hops"]
    replaced_to = result["replaced_to"]
    replaced_from = result["replaced_from"]
    last_change = result["last_change"]

    def snapshot(entry: dict[str, Any] | None) -> dict[str, Any] | None:
        if entry is None:
            return None
        return {
            "observed_on": entry["observed_on"],
            "confidence": entry["confidence"],
            "note": entry.get("note", ""),
        }

    # 每个阶段最后一次“进入当前事实”的跳（连续替换/环回时以最后一跳为准）。
    introduced_at: dict[str, dict[str, Any]] = {}
    touched_by_replacement: set[str] = set()
    for hop in hops:
        introduced_at[hop["to_stage"]] = hop
        touched_by_replacement.add(hop["from_stage"])
        touched_by_replacement.add(hop["to_stage"])

    # 所有出现过的阶段身份：冻结阶段 ∪ 替换进入阶段。按身份聚合，每阶段一行。
    stage_identities = set(frozen_index) | {hop["to_stage"] for hop in hops}

    lineage: list[dict[str, Any]] = []
    for stage in stage_identities:
        frozen_entry = frozen_index.get(stage)
        current = effective_index.get(stage)
        was_replaced_out = stage in replaced_to
        introduced = introduced_at.get(stage)

        if current is not None:
            # 当前轨道中存在该阶段。
            if frozen_entry is not None and stage in replaced_from:
                # 先被移出过、又由后续替换回到本阶段（环回）。
                status = "restored"
                revised = (
                    current["observed_on"] != frozen_entry["observed_on"]
                    or int(current["confidence"]) != int(frozen_entry["confidence"])
                    or current.get("note", "") != frozen_entry.get("note", "")
                )
            elif frozen_entry is not None and last_change.get(stage):
                status = "revised"
                revised = (
                    current["observed_on"] != frozen_entry["observed_on"]
                    or int(current["confidence"]) != int(frozen_entry["confidence"])
                    or current.get("note", "") != frozen_entry.get("note", "")
                )
            elif introduced is not None:
                status = "replaced_in"
                revised = True
            else:
                status = "unchanged"
                revised = False
            lineage.append(
                {
                    "stage": stage,
                    "status": status,
                    # 当前阶段没有“后续去向”。
                    "replacement_stage": None,
                    "replaced_from_stage": replaced_from.get(stage),
                    "frozen": snapshot(frozen_entry),
                    "current": snapshot(current),
                    "revised": revised,
                    "correction_id": (introduced or {}).get("correction_id")
                    or last_change.get(stage),
                }
            )
        else:
            # 当前轨道已无该阶段：中途替换进入又移出（transit），或起点被移出。
            is_transit = frozen_entry is None or stage in replaced_from
            lineage.append(
                {
                    "stage": stage,
                    "status": "replaced_transit" if is_transit else "replaced_out",
                    "replacement_stage": replaced_to.get(stage),
                    "replaced_from_stage": replaced_from.get(stage),
                    "frozen": snapshot(frozen_entry),
                    "current": None,
                    "revised": False,
                    "correction_id": (introduced or {}).get("correction_id"),
                }
            )

    return sorted(
        lineage,
        key=lambda item: (
            STAGE_BY_KEY.get(item["stage"], STAGES[-1]).rank,
            0 if item["current"] is not None else 1,
        ),
    )


def _replacement_chain(
    observation: dict[str, Any],
    corrections: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """按采纳顺序返回每次替换的“移出 → 进入”两跳视图。"""
    from .correction_rules import replay

    result = replay(observation, corrections)
    current_stages = {item["stage"] for item in result["entries"]}
    chain: list[dict[str, Any]] = []
    for index, hop in enumerate(result["hops"], start=1):
        chain.append(
            {
                "seq": index,
                "from_stage": hop["from_stage"],
                "to_stage": hop["to_stage"],
                "observed_on": hop["observed_on"],
                "confidence": hop["confidence"],
                "note": hop.get("note", ""),
                "correction_id": hop["correction_id"],
                "adoption_seq": hop["adoption_seq"],
                "adopted_at": hop["adopted_at"],
                # 是否仍在当前轨道：以最终事实集为准，环回到已出现阶段也算当前。
                "to_still_current": hop["to_stage"] in current_stages,
            }
        )
    return chain


def observation_summary(
    observation: dict[str, Any],
    tree: dict[str, Any] | None,
    corrections: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from .correction_rules import (
        adopted_chain,
        current_correction_id,
        effective_entries,
    )

    ledger = corrections or {}
    frozen = sort_stage_entries(observation.get("entries", []))
    effective = effective_entries(observation, ledger)
    chain = adopted_chain(ledger, observation["id"])
    active_correction_id = current_correction_id(ledger, observation["id"])
    replaced_stage_count = sum(
        1
        for correction in chain
        for change in correction["changes"]
        if change.get("change_type") == "replace"
    )
    proposed = [
        item
        for item in ledger.values()
        if item["observation_id"] == observation["id"]
        and item["status"] == "proposed"
    ]
    resolved = [
        item
        for item in ledger.values()
        if item["observation_id"] == observation["id"]
        and item["status"] in {"rejected", "withdrawn"}
    ]
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
        "stage_count": len(effective),
        "entry_map": {
            item["stage"]: {
                "observed_on": item["observed_on"],
                "confidence": item["confidence"],
                "note": item["note"],
            }
            for item in effective
        },
        "entries": effective,
        "frozen_entries": frozen,
        "current_entries": effective,
        "current_correction_id": active_correction_id,
        "current_correction_seq": len(chain),
        "replaced_stage_count": replaced_stage_count,
        "has_corrections": bool(chain),
        "proposed_correction_count": len(proposed),
        "resolved_correction_count": len(resolved),
        "entry_lineage": _entry_lineage(observation, ledger),
        "replacement_chain": _replacement_chain(observation, ledger),
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
