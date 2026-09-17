"""多年比较队列用例。"""

from __future__ import annotations

from typing import Any

from ..domain.cohort_rules import (
    build_cohort_export,
    cohort_summary,
    create_cohort_record,
)
from ..domain.plot_rules import now_iso
from ..errors import NotFoundError, ValidationError
from ..persistence import Repository


class CohortService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def list_cohorts(self, *, tree_id: str | None = None) -> dict[str, Any]:
        state = self.repository.read()
        items = [
            cohort_summary(record)
            for record in state["cohorts"].values()
            if tree_id is None or record["tree_id"] == tree_id
        ]
        items.sort(key=lambda item: item["created_at"], reverse=True)
        return {"items": items, "total": len(items)}

    def get_cohort(self, cohort_id: str) -> dict[str, Any]:
        state = self.repository.read()
        record = state["cohorts"].get(cohort_id)
        if record is None:
            raise NotFoundError("多年队列", cohort_id)
        return cohort_summary(record)

    def create_cohort(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"title", "tree_id", "season_start", "season_end"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValidationError(
                "生成多年队列包含不支持的字段",
                details={"unknown_fields": unknown},
            )

        def action(state: dict[str, Any]) -> dict[str, Any]:
            tree_id = str(payload.get("tree_id") or "")
            tree = state["trees"].get(tree_id)
            if tree is None:
                raise NotFoundError("植株", tree_id)
            record = create_cohort_record(
                payload,
                tree,
                state["observations"].values(),
                now_iso(),
            )
            duplicate = self._find_duplicate(
                state["cohorts"],
                record["tree_id"],
                record["season_start"],
                record["season_end"],
            )
            if duplicate is not None:
                return duplicate
            state["cohorts"][record["id"]] = record
            return record

        record = self.repository.atomic_update(action)
        return cohort_summary(record)

    def export_cohort(self, cohort_id: str) -> dict[str, Any]:
        state = self.repository.read()
        record = state["cohorts"].get(cohort_id)
        if record is None:
            raise NotFoundError("多年队列", cohort_id)
        return build_cohort_export(record)

    @staticmethod
    def _find_duplicate(
        cohorts: dict[str, dict[str, Any]],
        tree_id: str,
        season_start: str,
        season_end: str,
    ) -> dict[str, Any] | None:
        for record in cohorts.values():
            if (
                record["tree_id"] == tree_id
                and record["season_start"] == season_start
                and record["season_end"] == season_end
            ):
                return record
        return None
