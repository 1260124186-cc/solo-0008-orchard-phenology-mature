"""园区与植株实体规则。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from ..errors import ConflictError, PreconditionError, ValidationError
from .value_checks import (
    clean_plot_code,
    clean_text,
    clean_tree_code,
    clean_year,
    reject_unknown_fields,
)


PLOT_FIELDS = {
    "code",
    "name",
    "locality",
    "cultivar_focus",
    "steward",
    "planting_year",
    "note",
}
TREE_FIELDS = {
    "plot_id",
    "code",
    "cultivar",
    "rootstock",
    "planting_year",
    "status",
    "note",
}
TREE_STATUSES = {"active", "retired", "lost"}
TREE_CLOSING_STATUSES = {"retired", "lost"}
TREE_STATUS_CHANGE_FIELDS = {"status", "reason", "evidence", "note", "revision"}
TREE_STATUS_LABELS = {
    "active": "在册",
    "retired": "已退休",
    "lost": "已遗失",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def normalize_plot_payload(payload: dict[str, Any]) -> dict[str, Any]:
    reject_unknown_fields(payload, PLOT_FIELDS, label="园区信息")
    planting_year = clean_year(payload.get("planting_year"), "planting_year")
    return {
        "code": clean_plot_code(payload.get("code")),
        "name": clean_text(payload.get("name"), "name", maximum=80),
        "locality": clean_text(payload.get("locality"), "locality", maximum=160),
        "cultivar_focus": clean_text(
            payload.get("cultivar_focus"),
            "cultivar_focus",
            maximum=80,
        ),
        "steward": clean_text(payload.get("steward"), "steward", maximum=80),
        "planting_year": planting_year,
        "note": clean_text(
            payload.get("note", ""),
            "note",
            maximum=500,
            required=False,
        ),
    }


def create_plot_record(payload: dict[str, Any], timestamp: str) -> dict[str, Any]:
    normalized = normalize_plot_payload(payload)
    return {
        "id": new_identifier("plot"),
        "schema_version": 1,
        **normalized,
        "status": "draft",
        "revision": 1,
        "created_at": timestamp,
        "updated_at": timestamp,
        "confirmed_at": None,
    }


def ensure_unique_plot_code(
    plots: dict[str, dict[str, Any]],
    code: str,
    *,
    excluded_id: str | None = None,
) -> None:
    for plot in plots.values():
        if plot["code"] == code and plot["id"] != excluded_id:
            raise ConflictError(
                "plot_code_exists",
                "园区编号已存在",
                code=code,
            )


def update_plot_record(
    plot: dict[str, Any],
    payload: dict[str, Any],
    *,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    if plot["status"] != "draft":
        raise PreconditionError(
            "plot_not_editable",
            "已确认园区不能直接修改",
        )
    if expected_revision is not None:
        verify_revision(plot, expected_revision)
    merged = {
        "code": payload.get("code", plot["code"]),
        "name": payload.get("name", plot["name"]),
        "locality": payload.get("locality", plot["locality"]),
        "cultivar_focus": payload.get(
            "cultivar_focus",
            plot["cultivar_focus"],
        ),
        "steward": payload.get("steward", plot["steward"]),
        "planting_year": payload.get("planting_year", plot["planting_year"]),
        "note": payload.get("note", plot["note"]),
    }
    normalized = normalize_plot_payload(merged)
    return {
        **plot,
        **normalized,
        "revision": int(plot["revision"]) + 1,
        "updated_at": now_iso(),
    }


def confirm_plot_record(
    plot: dict[str, Any],
    trees: dict[str, dict[str, Any]],
    *,
    expected_revision: int | None,
) -> dict[str, Any]:
    if plot["status"] != "draft":
        raise PreconditionError("plot_already_confirmed", "园区已经确认")
    if expected_revision is not None:
        verify_revision(plot, expected_revision)
    active_trees = [
        item
        for item in trees.values()
        if item["plot_id"] == plot["id"] and item["status"] == "active"
    ]
    if not active_trees:
        raise PreconditionError(
            "plot_requires_tree",
            "确认前至少需要一株在册植株",
        )
    timestamp = now_iso()
    return {
        **plot,
        "status": "confirmed",
        "revision": int(plot["revision"]) + 1,
        "updated_at": timestamp,
        "confirmed_at": timestamp,
    }


def normalize_tree_payload(
    payload: dict[str, Any],
    *,
    allow_status: bool,
) -> dict[str, Any]:
    reject_unknown_fields(payload, TREE_FIELDS, label="植株信息")
    status = str(payload.get("status", "active")).strip().lower()
    if not allow_status and status != "active":
        raise ValidationError("新植株状态必须是 active", field_name="status")
    if status not in TREE_STATUSES:
        raise ValidationError(
            "植株状态不合法",
            field_name="status",
            details={"allowed": sorted(TREE_STATUSES)},
        )
    return {
        "plot_id": clean_text(
            payload.get("plot_id"),
            "plot_id",
            minimum=5,
            maximum=40,
        ),
        "code": clean_tree_code(payload.get("code")),
        "cultivar": clean_text(payload.get("cultivar"), "cultivar", maximum=80),
        "rootstock": clean_text(
            payload.get("rootstock", "未记录"),
            "rootstock",
            maximum=80,
            required=False,
        )
        or "未记录",
        "planting_year": clean_year(payload.get("planting_year"), "planting_year"),
        "status": status,
        "note": clean_text(
            payload.get("note", ""),
            "note",
            maximum=500,
            required=False,
        ),
    }


def create_tree_record(
    payload: dict[str, Any],
    plot: dict[str, Any],
    timestamp: str,
    *,
    actor_id: str = "",
) -> dict[str, Any]:
    normalized = normalize_tree_payload(payload, allow_status=False)
    if normalized["plot_id"] != plot["id"]:
        raise ValidationError("植株所属园区不匹配", field_name="plot_id")
    if plot["status"] != "draft":
        raise PreconditionError(
            "plot_not_editable",
            "只能向草稿园区添加植株",
        )
    if normalized["planting_year"] < int(plot["planting_year"]):
        raise ValidationError(
            "植株定植年份不能早于园区起始种植年份",
            field_name="planting_year",
        )
    history = [
        build_status_event(
            sequence=1,
            previous_status=None,
            status="active",
            reason="建株入册",
            evidence="建档登记",
            actor_id=actor_id or "建档",
            timestamp=timestamp,
        )
    ]
    return {
        "id": new_identifier("tree"),
        "schema_version": 1,
        **normalized,
        "status_history": history,
        "revision": 1,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def ensure_unique_tree_code(
    trees: dict[str, dict[str, Any]],
    plot_id: str,
    code: str,
    *,
    excluded_id: str | None = None,
) -> None:
    for tree in trees.values():
        if (
            tree["plot_id"] == plot_id
            and tree["code"] == code
            and tree["id"] != excluded_id
        ):
            raise ConflictError(
                "tree_code_exists",
                "该园区内植株编号已存在",
                code=code,
            )


def ensure_tree_belongs_to_plot(
    tree: dict[str, Any] | None,
    plot_id: str,
) -> dict[str, Any]:
    if tree is None or tree["plot_id"] != plot_id:
        raise ValidationError("园区内不存在该植株", field_name="tree_id")
    return tree


def normalize_tree_status_change(
    payload: dict[str, Any],
) -> dict[str, Any]:
    reject_unknown_fields(payload, TREE_STATUS_CHANGE_FIELDS, label="植株状态变更")
    return {
        "status": str(payload.get("status") or "").strip().lower(),
        "reason": clean_text(
            payload.get("reason"),
            "reason",
            minimum=2,
            maximum=300,
        ),
        "evidence": clean_text(
            payload.get("evidence"),
            "evidence",
            minimum=2,
            maximum=300,
        ),
        "note": clean_text(
            payload.get("note", ""),
            "note",
            maximum=500,
            required=False,
        ),
        "revision": payload.get("revision"),
    }


def change_tree_status_record(
    tree: dict[str, Any],
    payload: dict[str, Any],
    *,
    actor_id: str,
    timestamp: str,
) -> dict[str, Any]:
    """在同一植株记录上推进状态，并追加不可变的状态变化条目。"""
    normalized = normalize_tree_status_change(payload)
    verify_revision(tree, normalized["revision"])
    target_status = normalized["status"]
    if target_status not in TREE_STATUSES:
        raise ValidationError(
            "植株状态不合法",
            field_name="status",
            details={"allowed": sorted(TREE_STATUSES)},
        )
    current_status = str(tree.get("status") or "")
    if target_status == current_status:
        raise PreconditionError(
            "tree_status_unchanged",
            f"植株已经是{TREE_STATUS_LABELS.get(target_status, target_status)}状态，"
            "不能重复记录同一状态",
            current=current_status,
        )
    history = ensure_tree_status_history(tree)
    event = build_status_event(
        sequence=len(history) + 1,
        previous_status=current_status,
        status=target_status,
        reason=normalized["reason"],
        evidence=normalized["evidence"],
        actor_id=actor_id or "anonymous",
        timestamp=timestamp,
    )
    history.append(event)
    return {
        **tree,
        "status": target_status,
        "note": normalized["note"],
        "status_history": history,
        "revision": int(tree["revision"]) + 1,
        "updated_at": timestamp,
    }


def build_status_event(
    *,
    sequence: int,
    previous_status: str | None,
    status: str,
    reason: str,
    evidence: str,
    actor_id: str,
    timestamp: str,
) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "previous_status": previous_status,
        "status": status,
        "reason": reason,
        "evidence": evidence,
        "actor_id": actor_id,
        "changed_at": timestamp,
    }


def ensure_tree_status_history(tree: dict[str, Any]) -> list[dict[str, Any]]:
    """返回植株状态变化序列，兼容建档时尚未保存状态序列的旧记录。"""
    history = tree.get("status_history")
    if isinstance(history, list) and history:
        return history
    timestamp = str(tree.get("created_at") or now_iso())
    return [
        build_status_event(
            sequence=1,
            previous_status=None,
            status=str(tree.get("status") or "active"),
            reason="建株入册",
            evidence="建档登记",
            actor_id="建档",
            timestamp=timestamp,
        )
    ]


def verify_revision(
    record: dict[str, Any],
    expected_revision: Any,
) -> None:
    if isinstance(expected_revision, bool):
        raise ValidationError("修订号必须是整数", field_name="revision")
    try:
        expected = int(expected_revision)
    except (TypeError, ValueError) as exc:
        raise ValidationError("修订号必须是整数", field_name="revision") from exc
    actual = int(record.get("revision", 0))
    if expected != actual:
        raise ConflictError(
            "revision_conflict",
            "记录已被其他请求修改，请重新读取",
            expected=expected,
            actual=actual,
        )


def plot_summary(plot: dict[str, Any], tree_count: int) -> dict[str, Any]:
    return {
        "id": plot["id"],
        "code": plot["code"],
        "name": plot["name"],
        "locality": plot["locality"],
        "cultivar_focus": plot["cultivar_focus"],
        "steward": plot["steward"],
        "planting_year": plot["planting_year"],
        "note": plot["note"],
        "status": plot["status"],
        "revision": plot["revision"],
        "created_at": plot["created_at"],
        "updated_at": plot["updated_at"],
        "confirmed_at": plot["confirmed_at"],
        "tree_count": tree_count,
    }
