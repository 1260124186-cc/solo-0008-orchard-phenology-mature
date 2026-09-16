"""季节志对齐用例。"""

from __future__ import annotations

from typing import Any

from ..domain.comparison_rules import (
    comparison_summary,
    create_comparison_record,
    lineage_root,
    same_basis,
)
from ..domain.correction_rules import current_correction_id
from ..domain.plot_rules import now_iso
from ..errors import ConflictError, NotFoundError, ValidationError
from ..persistence import Repository


class ComparisonService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def list_comparisons(self) -> dict[str, Any]:
        state = self.repository.read()
        corrections = state.get("corrections", {})
        superseded_map = self._superseded_by_map(state["comparisons"])
        items = [
            comparison_summary(
                record,
                corrections=corrections,
                superseded_by_id=superseded_map.get(record["id"]),
            )
            for record in state["comparisons"].values()
        ]
        items.sort(key=lambda item: item["created_at"], reverse=True)
        return {"items": items, "total": len(items)}

    def get_comparison(self, comparison_id: str) -> dict[str, Any]:
        state = self.repository.read()
        record = state["comparisons"].get(comparison_id)
        if record is None:
            raise NotFoundError("对比图谱", comparison_id)
        superseded_map = self._superseded_by_map(state["comparisons"])
        return comparison_summary(
            record,
            corrections=state.get("corrections", {}),
            superseded_by_id=superseded_map.get(record["id"]),
        )

    def create_comparison(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "title",
            "left_observation_id",
            "right_observation_id",
            "supersedes_comparison_id",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValidationError(
                "生成对比图谱包含不支持的字段",
                details={"unknown_fields": unknown},
            )

        def action(state: dict[str, Any]) -> dict[str, Any]:
            left_id = str(payload.get("left_observation_id") or "")
            right_id = str(payload.get("right_observation_id") or "")
            left = state["observations"].get(left_id)
            if left is None:
                raise NotFoundError("左侧季节志", left_id)
            right = state["observations"].get(right_id)
            if right is None:
                raise NotFoundError("右侧季节志", right_id)
            left_tree = state["trees"].get(left["tree_id"])
            right_tree = state["trees"].get(right["tree_id"])
            corrections = state.setdefault("corrections", {})
            left_correction_id = current_correction_id(corrections, left_id)
            right_correction_id = current_correction_id(corrections, right_id)

            pair_records = [
                record
                for record in state["comparisons"].values()
                if {record["left_observation_id"], record["right_observation_id"]}
                == {left_id, right_id}
            ]
            matching = [
                record
                for record in pair_records
                if same_basis(record, left_correction_id, right_correction_id)
            ]
            if matching:
                # 相同勘误世代的重复请求复用首次结果，不产生两套“当前”图谱。
                return matching[0]

            supersedes_id = (
                str(payload.get("supersedes_comparison_id") or "").strip() or None
            )
            root_pair = pair_records
            if not root_pair:
                if supersedes_id is not None:
                    raise ValidationError(
                        "没有可接续的历史对比图谱，不应提供 supersedes_comparison_id",
                        field_name="supersedes_comparison_id",
                    )
            else:
                if supersedes_id is None:
                    latest = max(root_pair, key=lambda item: item["created_at"])
                    raise ConflictError(
                        "comparison_basis_superseded",
                        "源季节志已采用勘误，既有图谱基于旧事实冻结；"
                        "旧图谱不会被改写，如需当前结论请显式生成新版图谱",
                        existing_comparison_id=latest["id"],
                    )
                predecessor = state["comparisons"].get(supersedes_id)
                if predecessor is None:
                    raise NotFoundError("历史对比图谱", supersedes_id)
                if {
                    predecessor["left_observation_id"],
                    predecessor["right_observation_id"],
                } != {left_id, right_id}:
                    raise ValidationError(
                        "新版图谱接续的不是同一对季节志",
                        field_name="supersedes_comparison_id",
                    )
                if same_basis(
                    predecessor,
                    left_correction_id,
                    right_correction_id,
                ):
                    raise ConflictError(
                        "comparison_basis_unchanged",
                        "源季节志事实没有新的勘误，现有图谱仍然有效",
                        existing_comparison_id=predecessor["id"],
                    )
                current_root = lineage_root(state["comparisons"], root_pair[0])
                requested_root = lineage_root(state["comparisons"], predecessor)
                if requested_root != current_root:
                    raise ConflictError(
                        "comparison_lineage_conflict",
                        "只能接续同一谱系中最新的历史图谱",
                    )

            record = create_comparison_record(
                payload,
                left,
                right,
                left_tree,
                right_tree,
                corrections,
                now_iso(),
            )
            if supersedes_id is None:
                record["lineage_root_id"] = record["id"]
            state["comparisons"][record["id"]] = record
            return record

        record = self.repository.atomic_update(action)
        return self.get_comparison(record["id"])

    @staticmethod
    def _superseded_by_map(
        comparisons: dict[str, dict[str, Any]],
    ) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for record in comparisons.values():
            previous = record.get("supersedes_comparison_id")
            if previous:
                mapping[previous] = record["id"]
        return mapping
