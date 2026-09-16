"""园区编号修正与植株编号级联规则。

园区编号修正必须在单个事务内同时完成：园区本身、当前园区内所有以旧园区编号
为前缀的植株，以及用于解释历史编号的别名轨迹。任何阻断条件都会先被收集到
``blockers`` 中并整体拒绝，因此不会出现部分植株使用新编号、部分保留旧编号的
中间状态。

历史比较与简报保存的是生成当时的编号（比较的 ``left_label`` / ``right_label``、
简报中冻结的植株编号），编号修正不会改写这些记录；植株与园区上的
``code_aliases`` 轨迹让历史编号仍可解释。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..errors import ConflictError, PreconditionError, ValidationError
from .plot_rules import now_iso, verify_revision
from .value_checks import (
    clean_plot_code,
    clean_text,
    clean_tree_code,
)


PLOT_CODE_CORRECTION_FIELDS = {"code", "reason", "revision", "fingerprint"}
TREE_CODE_CORRECTION_FIELDS = {"code", "reason", "revision"}

_TREE_SUFFIX_PATTERN = "-T"


def build_plot_code_plan(
    state: dict[str, Any],
    plot_id: str,
    new_code: str | None,
) -> dict[str, Any]:
    """生成编号修正计划（只读）。

    ``new_code`` 为空时只报告园区当前的身份问题（用于未决植株检查）；提供新
    编号时同时计算需要级联的植株。计划本身不修改任何对象。
    """
    plot = state["plots"].get(plot_id)
    if plot is None:
        raise KeyError(plot_id)

    target_code: str | None = None
    code_errors: list[dict[str, str]] = []
    if new_code is not None and str(new_code).strip():
        try:
            target_code = clean_plot_code(new_code)
        except ValidationError as exc:
            code_errors = [{"field": "code", "message": exc.message}]
            target_code = str(new_code).strip().upper() or None

    blockers: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []

    if plot["status"] != "draft":
        blockers.append(
            {
                "kind": "plot_not_editable",
                "message": "已确认园区不能修正编号，如需修订请新建下一版园区记录",
                "plot_id": plot_id,
            }
        )

    if target_code is not None:
        for message in code_errors:
            blockers.append(
                {
                    "kind": "invalid_plot_code",
                    "message": message["message"],
                    "plot_id": plot_id,
                }
            )
        if target_code == plot["code"]:
            blockers.append(
                {
                    "kind": "plot_code_unchanged",
                    "message": "新园区编号与当前编号一致，无需修正",
                    "plot_id": plot_id,
                    "code": target_code,
                }
            )
        for other in state["plots"].values():
            if other["id"] != plot_id and other["code"] == target_code:
                blockers.append(
                    {
                        "kind": "plot_code_exists",
                        "message": "园区编号已被其他园区使用",
                        "plot_id": other["id"],
                        "code": target_code,
                    }
                )

    # 按植株编号排序，保证计划与错误信息是确定性的。
    plot_trees = sorted(
        (tree for tree in state["trees"].values() if tree["plot_id"] == plot_id),
        key=lambda item: item["code"],
    )

    # 未决植株：编号前缀不属于当前园区，级联时无法机械替换，必须先人工处理，
    # 不能悄悄跳过。
    pending: list[dict[str, Any]] = []
    for tree in plot_trees:
        prefix = tree_code_plot_prefix(tree["code"])
        if prefix is None or prefix != plot["code"]:
            pending.append(
                {
                    "kind": "tree_code_unresolved",
                    "message": (
                        f"植株 {tree['code']} 的编号前缀与园区编号 "
                        f"{plot['code']} 不一致，需先修正该植株编号"
                    ),
                    "tree_id": tree["id"],
                    "tree_code": tree["code"],
                    "expected_prefix": plot["code"],
                    "tree_revision": int(tree["revision"]),
                }
            )
    blockers.extend(pending)

    # 级联后同园区内编号冲突（包含已退休/已遗失植株，它们仍保留历史）。
    if target_code is not None and not code_errors:
        # 只有当前前缀与园区一致的植株才参与机械级联；未决植株只列入
        # pending，必须先经单独的归位修正，不能在变化清单里被默默改名。
        resolvable = [
            tree
            for tree in plot_trees
            if tree_code_plot_prefix(tree["code"]) == plot["code"]
        ]
        projected: dict[str, str] = {}
        for tree in resolvable:
            suffix = tree_code_suffix(tree["code"])
            if suffix is None:
                continue
            next_code = f"{target_code}{suffix}"
            owner = projected.get(next_code)
            if owner is not None:
                blockers.append(
                    {
                        "kind": "tree_code_duplicate",
                        "message": (
                            f"级联后植株编号 {next_code} 同时对应 "
                            f"{owner} 与 {tree['id']}，请先人工区分序号"
                        ),
                        "tree_id": tree["id"],
                        "tree_code": tree["code"],
                        "conflicting_code": next_code,
                        "conflicting_tree_id": owner,
                    }
                )
            else:
                projected[next_code] = tree["id"]
        for tree in resolvable:
            suffix = tree_code_suffix(tree["code"])
            if suffix is None:
                continue
            next_code = f"{target_code}{suffix}"
            changes.append(
                {
                    "tree_id": tree["id"],
                    "tree_code": tree["code"],
                    "next_code": next_code,
                    "tree_revision": int(tree["revision"]),
                    "status": tree["status"],
                }
            )

    plan: dict[str, Any] = {
        "plot_id": plot_id,
        "plot_code": plot["code"],
        "plot_revision": int(plot["revision"]),
        "new_code": target_code,
        "status": plot["status"],
        "changes": changes,
        "pending": pending,
        "blockers": blockers,
        "applicable": not blockers and target_code is not None,
    }
    plan["fingerprint"] = plan_fingerprint(plan)
    return plan


def apply_plot_code_correction(
    state: dict[str, Any],
    plot_id: str,
    payload: dict[str, Any],
    *,
    actor_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """在事务工作副本上执行园区编号级联修正。

    返回更新后的园区记录与修正报告。阻断时抛出
    :class:`PreconditionError`（业务阻断）或 :class:`ConflictError`（并发修
    订、编号占用），由调用方在同一事务内整体回滚。
    """
    unknown = sorted(set(payload) - PLOT_CODE_CORRECTION_FIELDS)
    if unknown:
        raise ValidationError(
            "园区编号修正包含不支持的字段",
            details={"unknown_fields": unknown},
        )
    if "revision" not in payload:
        raise ValidationError("编号修正需要 revision", field_name="revision")

    plot = state["plots"].get(plot_id)
    if plot is None:
        raise KeyError(plot_id)
    verify_revision(plot, payload["revision"])

    plan = build_plot_code_plan(state, plot_id, payload.get("code"))
    _raise_for_blockers(plan)

    expected_fingerprint = payload.get("fingerprint")
    if expected_fingerprint is not None and expected_fingerprint != plan["fingerprint"]:
        raise ConflictError(
            "correction_plan_stale",
            "编号修正计划已过期，档案在此期间被修改，请重新预览后再执行",
            expected=expected_fingerprint,
            actual=plan["fingerprint"],
            pending=plan["pending"],
        )

    reason = clean_text(
        payload.get("reason", ""),
        "reason",
        maximum=200,
        required=False,
    ) or "园区编号修正"
    timestamp = now_iso()
    new_code = plan["new_code"]
    assert new_code is not None

    tree_results: list[dict[str, Any]] = []
    for change in plan["changes"]:
        tree = state["trees"][change["tree_id"]]
        previous_code = tree["code"]
        updated = {
            **tree,
            "code": change["next_code"],
            "code_aliases": [
                *tree.get("code_aliases", []),
                {
                    "code": previous_code,
                    "changed_to": change["next_code"],
                    "reason": reason,
                    "actor_id": actor_id,
                    "changed_at": timestamp,
                    "cause": "plot_code_correction",
                },
            ],
            "revision": int(tree["revision"]) + 1,
            "updated_at": timestamp,
        }
        state["trees"][tree["id"]] = updated
        tree_results.append(
            {
                "tree_id": updated["id"],
                "old_code": previous_code,
                "new_code": updated["code"],
                "revision": updated["revision"],
                "status": updated["status"],
            }
        )

    updated_plot = {
        **plot,
        "code": new_code,
        "code_aliases": [
            *plot.get("code_aliases", []),
            {
                "code": plan["plot_code"],
                "changed_to": new_code,
                "reason": reason,
                "actor_id": actor_id,
                "changed_at": timestamp,
                "cause": "plot_code_correction",
            },
        ],
        "revision": int(plot["revision"]) + 1,
        "updated_at": timestamp,
    }
    state["plots"][plot_id] = updated_plot

    report = {
        "plot_id": plot_id,
        "old_code": plan["plot_code"],
        "new_code": new_code,
        "reason": reason,
        "revision": updated_plot["revision"],
        "trees": tree_results,
        "unchanged_objects": [],
        "historical_objects": [
            {
                "kind": "comparison",
                "frozen_fields": ["left_label", "right_label", "summary.sentence"],
            },
            {
                "kind": "brief",
                "frozen_fields": ["plot.code", "trees[].code", "observations[].tree_code"],
            },
        ],
        "applied_at": timestamp,
    }
    return updated_plot, report


def repair_tree_code(
    state: dict[str, Any],
    tree_id: str,
    payload: dict[str, Any],
    *,
    actor_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """单独修正一株未决植株的编号，使其重新归属到所属园区的前缀。"""
    unknown = sorted(set(payload) - TREE_CODE_CORRECTION_FIELDS)
    if unknown:
        raise ValidationError(
            "植株编号修正包含不支持的字段",
            details={"unknown_fields": unknown},
        )
    if "revision" not in payload:
        raise ValidationError("编号修正需要 revision", field_name="revision")

    tree = state["trees"].get(tree_id)
    if tree is None:
        raise KeyError(tree_id)
    verify_revision(tree, payload["revision"])
    plot = state["plots"].get(tree["plot_id"])
    if plot is None:
        raise PreconditionError(
            "plot_missing",
            "植株所属园区不存在，无法修正编号",
            tree_id=tree_id,
        )
    if plot["status"] != "draft":
        raise PreconditionError(
            "plot_not_editable",
            "所属园区已确认，不能修正植株编号",
        )
    next_code = clean_tree_code(payload.get("code"))
    if next_code == tree["code"]:
        raise ValidationError("新植株编号与当前编号一致，无需修正", field_name="code")
    expected_prefix = f"{plot['code']}-T"
    if not next_code.startswith(expected_prefix):
        raise ValidationError(
            f"植株编号必须以所属园区编号 {plot['code']}-T 开头",
            field_name="code",
            details={"expected_prefix": plot["code"]},
        )
    for other in state["trees"].values():
        if (
            other["id"] != tree_id
            and other["plot_id"] == tree["plot_id"]
            and other["code"] == next_code
        ):
            raise ConflictError(
                "tree_code_exists",
                "该园区内植株编号已存在",
                existing_tree_id=other["id"],
                conflicting_code=next_code,
            )

    reason = clean_text(
        payload.get("reason", ""),
        "reason",
        maximum=200,
        required=False,
    ) or "植株编号修正"
    timestamp = now_iso()
    updated = {
        **tree,
        "code": next_code,
        "code_aliases": [
            *tree.get("code_aliases", []),
            {
                "code": tree["code"],
                "changed_to": next_code,
                "reason": reason,
                "actor_id": actor_id,
                "changed_at": timestamp,
                "cause": "tree_code_repair",
            },
        ],
        "revision": int(tree["revision"]) + 1,
        "updated_at": timestamp,
    }
    state["trees"][tree_id] = updated
    report = {
        "tree_id": tree_id,
        "old_code": tree["code"],
        "new_code": next_code,
        "reason": reason,
        "revision": updated["revision"],
        "applied_at": timestamp,
    }
    return updated, report


def build_identity_report(state: dict[str, Any]) -> dict[str, Any]:
    """跨园区、植株与历史结果核对身份唯一性，并给出可解释依据。"""
    plot_codes: dict[str, list[str]] = {}
    for plot in state["plots"].values():
        plot_codes.setdefault(plot["code"], []).append(plot["id"])

    tree_codes: dict[str, list[str]] = {}
    tree_prefix_findings: list[dict[str, Any]] = []
    for tree in state["trees"].values():
        scope_key = f"{tree['plot_id']}::{tree['code']}"
        tree_codes.setdefault(scope_key, []).append(tree["id"])
        plot = state["plots"].get(tree["plot_id"])
        prefix = tree_code_plot_prefix(tree["code"])
        if plot is None or prefix is None or prefix != plot["code"]:
            tree_prefix_findings.append(
                {
                    "tree_id": tree["id"],
                    "tree_code": tree["code"],
                    "plot_id": tree["plot_id"],
                    "plot_code": plot["code"] if plot else None,
                    "status": "unresolved",
                }
            )

    duplicate_plot_codes = [
        {"code": code, "plot_ids": ids}
        for code, ids in sorted(plot_codes.items())
        if len(ids) > 1
    ]
    duplicate_tree_codes = [
        {"plot_id": key.split("::", 1)[0], "code": key.split("::", 1)[1], "tree_ids": ids}
        for key, ids in sorted(tree_codes.items())
        if len(ids) > 1
    ]

    historical: list[dict[str, Any]] = []
    for comparison in state["comparisons"].values():
        historical.append(
            {
                "kind": "comparison",
                "id": comparison["id"],
                "left_tree_id": comparison["left_tree_id"],
                "right_tree_id": comparison["right_tree_id"],
                "left_label": comparison["left_label"],
                "right_label": comparison["right_label"],
                "frozen_at": comparison["created_at"],
                "explainable": _comparison_codes_explainable(state, comparison),
            }
        )
    for brief in state["briefs"].values():
        historical.append(
            {
                "kind": "brief",
                "id": brief["id"],
                "plot_id": brief["plot"]["id"],
                "plot_code": brief["plot"]["code"],
                "frozen_at": brief["created_at"],
                "state_revision": brief["state_revision"],
                "explainable": _brief_codes_explainable(state, brief),
            }
        )

    unique = (
        not duplicate_plot_codes
        and not duplicate_tree_codes
        and not tree_prefix_findings
    )
    return {
        "unique": unique,
        "duplicate_plot_codes": duplicate_plot_codes,
        "duplicate_tree_codes": duplicate_tree_codes,
        "unresolved_trees": tree_prefix_findings,
        "historical_objects": historical,
        "plot_count": len(state["plots"]),
        "tree_count": len(state["trees"]),
        "comparison_count": len(state["comparisons"]),
        "brief_count": len(state["briefs"]),
    }


def plan_fingerprint(plan: dict[str, Any]) -> str:
    payload = {
        "plot_id": plan["plot_id"],
        "plot_code": plan["plot_code"],
        "plot_revision": plan["plot_revision"],
        "new_code": plan["new_code"],
        "changes": [
            {
                "tree_id": item["tree_id"],
                "tree_code": item["tree_code"],
                "next_code": item["next_code"],
                "tree_revision": item["tree_revision"],
            }
            for item in plan["changes"]
        ],
        "pending": [
            {"tree_id": item["tree_id"], "tree_revision": item["tree_revision"]}
            for item in plan["pending"]
        ],
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return f"plan_{digest.hexdigest()[:24]}"


def tree_code_plot_prefix(tree_code: str) -> str | None:
    """提取植株编号中的园区片段，无法识别时返回 ``None``。"""
    marker = tree_code.rfind(_TREE_SUFFIX_PATTERN)
    if marker <= 0:
        return None
    prefix = tree_code[:marker]
    suffix = tree_code[marker + len(_TREE_SUFFIX_PATTERN):]
    if not suffix.isdigit() or not prefix:
        return None
    return prefix


def tree_code_suffix(tree_code: str) -> str | None:
    marker = tree_code.rfind(_TREE_SUFFIX_PATTERN)
    if marker <= 0:
        return None
    suffix = tree_code[marker:]
    if not suffix[2:].isdigit():
        return None
    return suffix


def _raise_for_blockers(plan: dict[str, Any]) -> None:
    if not plan["blockers"]:
        return
    # 编号占用属于 409；其余为前置条件不满足（未决植株、格式、已确认等）。
    conflict_kinds = {"plot_code_exists", "tree_code_duplicate"}
    conflicts = [
        item for item in plan["blockers"] if item["kind"] in conflict_kinds
    ]
    if conflicts:
        first = conflicts[0]
        raise ConflictError(
            first["kind"],
            first["message"],
            blockers=plan["blockers"],
            pending=plan["pending"],
        )
    first = plan["blockers"][0]
    raise PreconditionError(
        first["kind"],
        "编号修正存在尚未处理的对象，未写入任何新编号",
        blockers=plan["blockers"],
        pending=plan["pending"],
    )


def _label_code(label: str) -> str:
    return str(label).split(" · ", 1)[0].strip()


def _comparison_codes_explainable(
    state: dict[str, Any],
    comparison: dict[str, Any],
) -> bool:
    pairs = (
        (comparison.get("left_tree_id"), comparison.get("left_label")),
        (comparison.get("right_tree_id"), comparison.get("right_label")),
    )
    return all(
        _frozen_tree_code_explainable(state, tree_id, _label_code(label or ""))
        for tree_id, label in pairs
    )


def _brief_codes_explainable(
    state: dict[str, Any],
    brief: dict[str, Any],
) -> bool:
    plot = state["plots"].get(brief["plot"]["id"])
    plot_code = brief["plot"]["code"]
    if plot is None:
        return False
    if plot["code"] != plot_code and not _alias_covers(plot, plot_code):
        return False
    for frozen_tree in brief.get("trees", []):
        tree = state["trees"].get(frozen_tree["id"])
        if tree is None:
            return False
        if tree["code"] != frozen_tree["code"] and not _alias_covers(
            tree,
            frozen_tree["code"],
        ):
            return False
    return True


def _frozen_tree_code_explainable(
    state: dict[str, Any],
    tree_id: str | None,
    frozen_code: str,
) -> bool:
    tree = state["trees"].get(tree_id or "")
    if tree is None:
        return False
    if tree["code"] == frozen_code:
        return True
    return _alias_covers(tree, frozen_code)


def _alias_covers(record: dict[str, Any], code: str) -> bool:
    return any(alias.get("code") == code for alias in record.get("code_aliases", []))
