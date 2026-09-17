"""同一植株多年比较用例。"""

from __future__ import annotations

from typing import Any

from ..domain.plot_rules import now_iso
from ..domain.series_rules import create_series_record, series_summary
from ..errors import NotFoundError
from ..persistence import Repository


class SeriesService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def list_series(self) -> dict[str, Any]:
        state = self.repository.read()
        items = [
            series_summary(record)
            for record in state["series"].values()
        ]
        items.sort(key=lambda item: item["created_at"], reverse=True)
        return {"items": items, "total": len(items)}

    def get_series(self, series_id: str) -> dict[str, Any]:
        state = self.repository.read()
        record = state["series"].get(series_id)
        if record is None:
            raise NotFoundError("多年比较", series_id)
        return series_summary(record)

    def create_series(self, payload: dict[str, Any]) -> dict[str, Any]:
        def action(state: dict[str, Any]) -> dict[str, Any]:
            tree_id = str(payload.get("tree_id") or "")
            tree = state["trees"].get(tree_id)
            if tree is None:
                raise NotFoundError("植株", tree_id)
            observations = [
                item
                for item in state["observations"].values()
                if item["tree_id"] == tree["id"]
            ]
            record = create_series_record(
                payload,
                tree,
                observations,
                now_iso(),
            )
            duplicate = self._find_duplicate(
                state["series"],
                record["tree_id"],
                record["season_from"],
                record["season_to"],
            )
            if duplicate is not None:
                return duplicate
            state["series"][record["id"]] = record
            return record

        record = self.repository.atomic_update(action)
        return series_summary(record)

    @staticmethod
    def _find_duplicate(
        series: dict[str, dict[str, Any]],
        tree_id: str,
        season_from: str,
        season_to: str,
    ) -> dict[str, Any] | None:
        for record in series.values():
            if (
                record["tree_id"] == tree_id
                and record["season_from"] == season_from
                and record["season_to"] == season_to
            ):
                return record
        return None
