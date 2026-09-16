"""季节志受控勘误用例。"""

from __future__ import annotations

from typing import Any

from ..domain.correction_rules import (
    adopt_correction,
    build_correction,
    correction_summary,
    reject_correction,
    withdraw_correction,
)
from ..domain.plot_rules import now_iso
from ..errors import NotFoundError
from ..persistence import Repository
from ..security.context import current_request_context


class CorrectionService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def list_corrections(
        self,
        *,
        observation_id: str | None = None,
        tree_id: str | None = None,
        plot_id: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        state = self.repository.read()
        items: list[dict[str, Any]] = []
        for correction in state.get("corrections", {}).values():
            if observation_id and correction["observation_id"] != observation_id:
                continue
            if tree_id and correction["tree_id"] != tree_id:
                continue
            if plot_id and correction["plot_id"] != plot_id:
                continue
            if status and correction["status"] != status:
                continue
            tree = state["trees"].get(correction["tree_id"])
            items.append(correction_summary(correction, tree))
        items.sort(key=lambda item: item["proposed_at"], reverse=True)
        return {"items": items, "total": len(items)}

    def get_correction(self, correction_id: str) -> dict[str, Any]:
        state = self.repository.read()
        correction = state.get("corrections", {}).get(correction_id)
        if correction is None:
            raise NotFoundError("勘误", correction_id)
        return correction_summary(
            correction,
            state["trees"].get(correction["tree_id"]),
        )

    def create_correction(self, payload: dict[str, Any]) -> dict[str, Any]:
        def action(state: dict[str, Any]) -> dict[str, Any]:
            observation_id = str(payload.get("observation_id") or "")
            observation = state["observations"].get(observation_id)
            if observation is None:
                raise NotFoundError("季节志", observation_id)
            actor_id = current_request_context().actor_id or "anonymous"
            record = build_correction(
                payload,
                observation,
                state.setdefault("corrections", {}),
                actor_id=actor_id,
                timestamp=now_iso(),
            )
            state.setdefault("corrections", {})[record["id"]] = record
            return record

        record = self.repository.atomic_update(action)
        return self.get_correction(record["id"])

    def adopt_correction(
        self,
        correction_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        def action(state: dict[str, Any]) -> dict[str, Any]:
            ledger = state.setdefault("corrections", {})
            correction = ledger.get(correction_id)
            if correction is None:
                raise NotFoundError("勘误", correction_id)
            observation = state["observations"].get(correction["observation_id"])
            if observation is None:
                raise NotFoundError("季节志", correction["observation_id"])
            actor_id = current_request_context().actor_id or "anonymous"
            updated = adopt_correction(
                correction,
                observation,
                ledger,
                payload,
                actor_id=actor_id,
                timestamp=now_iso(),
            )
            ledger[correction_id] = updated
            return updated

        self.repository.atomic_update(action)
        return self.get_correction(correction_id)

    def reject_correction(
        self,
        correction_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        def action(state: dict[str, Any]) -> dict[str, Any]:
            ledger = state.setdefault("corrections", {})
            correction = ledger.get(correction_id)
            if correction is None:
                raise NotFoundError("勘误", correction_id)
            actor_id = current_request_context().actor_id or "anonymous"
            updated = reject_correction(
                correction,
                payload,
                actor_id=actor_id,
                timestamp=now_iso(),
            )
            ledger[correction_id] = updated
            return updated

        self.repository.atomic_update(action)
        return self.get_correction(correction_id)

    def withdraw_correction(
        self,
        correction_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        def action(state: dict[str, Any]) -> dict[str, Any]:
            ledger = state.setdefault("corrections", {})
            correction = ledger.get(correction_id)
            if correction is None:
                raise NotFoundError("勘误", correction_id)
            actor_id = current_request_context().actor_id or "anonymous"
            updated = withdraw_correction(
                correction,
                payload,
                actor_id=actor_id,
                timestamp=now_iso(),
            )
            ledger[correction_id] = updated
            return updated

        self.repository.atomic_update(action)
        return self.get_correction(correction_id)
