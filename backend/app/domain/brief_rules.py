"""编研简报冻结规则。"""

from __future__ import annotations

from typing import Any

from ..errors import PreconditionError, ValidationError
from .correction_rules import current_correction_id
from .observation_rules import observation_summary
from .plot_rules import new_identifier, plot_summary


def create_brief_record(
    payload: dict[str, Any],
    plot: dict[str, Any],
    trees: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    corrections: dict[str, dict[str, Any]],
    state_revision: int,
    timestamp: str,
) -> dict[str, Any]:
    if plot["status"] != "confirmed":
        raise PreconditionError(
            "plot_not_confirmed",
            "只有已确认园区可以生成编研简报",
        )
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValidationError("请填写简报标题", field_name="title")
    if len(title) > 100:
        raise ValidationError("简报标题最多 100 个字符", field_name="title")
    completed = [item for item in observations if item["status"] == "completed"]
    if not completed:
        raise PreconditionError(
            "completed_season_required",
            "至少需要一份已完成季节志",
        )
    tree_map = {item["id"]: item for item in trees}
    observation_summaries = [
        observation_summary(item, tree_map.get(item["tree_id"]), corrections)
        for item in sorted(
            completed,
            key=lambda value: (
                value["season"],
                tree_map.get(value["tree_id"], {}).get("code", ""),
            ),
        )
    ]
    return {
        "id": new_identifier("brief"),
        "schema_version": 1,
        "title": title,
        "plot": plot_summary(plot, len(trees)),
        "trees": [
            {
                "id": tree["id"],
                "code": tree["code"],
                "cultivar": tree["cultivar"],
                "rootstock": tree["rootstock"],
                "planting_year": tree["planting_year"],
                "status": tree["status"],
                "note": tree["note"],
            }
            for tree in sorted(trees, key=lambda value: value["code"])
        ],
        "observations": observation_summaries,
        "state_revision": state_revision,
        "created_at": timestamp,
    }


def annotate_brief_basis(
    brief: dict[str, Any],
    corrections: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """依据当前勘误链，标注每份季节志是否在简报冻结后被新结论超越。"""
    revised_sources: list[dict[str, Any]] = []
    for item in brief.get("observations", []):
        frozen_basis = item.get("current_correction_id")
        latest_basis = current_correction_id(corrections, item["id"])
        superseded = frozen_basis != latest_basis
        if superseded:
            revised_sources.append(
                {
                    "observation_id": item["id"],
                    "frozen_correction_id": frozen_basis,
                    "current_correction_id": latest_basis,
                    "season": item["season"],
                    "tree_code": item["tree_code"],
                }
            )
    return {
        "basis_status": "superseded" if revised_sources else "current",
        "superseded_observations": revised_sources,
    }


def brief_summary(
    record: dict[str, Any],
    *,
    include_payload: bool,
    corrections: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    basis = annotate_brief_basis(record, corrections or {})
    summary = {
        "id": record["id"],
        "title": record["title"],
        "plot_id": record["plot"]["id"],
        "plot_code": record["plot"]["code"],
        "plot_name": record["plot"]["name"],
        "season_count": len({item["season"] for item in record["observations"]}),
        "tree_count": len(record["trees"]),
        "observation_count": len(record["observations"]),
        "state_revision": record["state_revision"],
        "created_at": record["created_at"],
        "basis_status": basis["basis_status"],
        "superseded_observation_count": len(basis["superseded_observations"]),
        "superseded_observations": basis["superseded_observations"],
    }
    if include_payload:
        summary["payload"] = {
            "plot": record["plot"],
            "trees": record["trees"],
            "observations": record["observations"],
        }
    return summary
