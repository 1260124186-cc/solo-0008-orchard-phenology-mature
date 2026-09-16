#!/usr/bin/env python3
"""受控勘误的版本关系自证脚本。

依次完成：
1. 建立园区、两株植株、两份已完成季节志、一份比较图谱和一份简报；
2. 对左侧季节志提出并采纳一条日期勘误；
3. 核对原始记录未被改写、旧分析保持冻结且被标记为历史、新分析采用新结论；
4. 关闭数据库并以全新仓储重新打开，核对恢复后的数据与版本表、审计链完全一致。

用法：python3 scripts/verify_corrections.py [--data-dir DIR]
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

from app.application import (
    BriefService,
    CatalogService,
    ComparisonService,
    CorrectionService,
    ObservationService,
)
from app.persistence import Database, Repository
from app.security import RequestContext, request_scope

STAGES = ["bud_burst", "full_bloom", "fruit_set", "harvest"]


def request_scope_for(key: str, path: str | None = None):
    route = path or f"/api/{key}"
    return request_scope(
        RequestContext(
            actor_id="local-admin",
            idempotency_key=key,
            request_method="PUT",
            request_path=route,
            request_hash=key,
            route_template=route,
        )
    )


def build_stack(data_dir: Path):
    repository = Repository(Database(data_dir / "atlas.sqlite3"))
    repository.open()
    return (
        repository,
        CatalogService(repository),
        ObservationService(repository),
        ComparisonService(repository),
        BriefService(repository),
        CorrectionService(repository),
    )


def seed(catalog, observations, corrections_unused=None):
    with request_scope_for("v-plot"):
        plot = catalog.create_plot(
            {
                "code": "OR-9101",
                "name": "版本关系验证园",
                "locality": "河湾北岭",
                "cultivar_focus": "秋白梨",
                "steward": "编研组",
                "planting_year": 2011,
                "note": "",
            }
        )
    trees = []
    for index, (code, cultivar) in enumerate(
        (("OR-9101-T01", "秋白梨"), ("OR-9101-T02", "蜜香梨")),
        start=1,
    ):
        with request_scope_for(f"v-tree-{index}"):
            trees.append(
                catalog.create_tree(
                    {
                        "plot_id": plot["id"],
                        "code": code,
                        "cultivar": cultivar,
                        "rootstock": "杜梨",
                        "planting_year": 2011,
                        "status": "active",
                        "note": "",
                    }
                )
            )
    with request_scope_for("v-plot-confirm"):
        catalog.confirm_plot(plot["id"], expected_revision=plot["revision"])

    def complete_season(tree, dates, suffix: str):
        with request_scope_for(f"v-season-{suffix}"):
            season = observations.start_observation(
                {
                    "tree_id": tree["id"],
                    "season": "2026",
                    "observer": "验证员",
                    "note": "",
                }
            )
        for index, (stage, observed_on) in enumerate(zip(STAGES, dates)):
            with request_scope_for(f"v-entry-{suffix}-{index}"):
                season = observations.add_stage(
                    season["id"],
                    {
                        "stage": stage,
                        "observed_on": observed_on,
                        "confidence": 4,
                        "note": "",
                        "revision": season["revision"],
                    },
                )
        with request_scope_for(f"v-complete-{suffix}"):
            return observations.complete_observation(
                season["id"], {"revision": season["revision"]}
            )

    left = complete_season(
        trees[0],
        ["2026-03-10", "2026-04-01", "2026-04-18", "2026-09-02"],
        "left",
    )
    right = complete_season(
        trees[1],
        ["2026-03-15", "2026-04-05", "2026-04-22", "2026-09-07"],
        "right",
    )
    return plot, left, right


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir")
    args = parser.parse_args()

    temporary = None
    if args.data_dir:
        data_dir = Path(args.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
    else:
        temporary = tempfile.mkdtemp(prefix="orchard-verify-")
        data_dir = Path(temporary)

    repository, catalog, observations, comparisons, briefs, corrections = build_stack(
        data_dir,
    )
    plot, left, right = seed(catalog, observations)

    with request_scope_for("v-comparison-1"):
        comparison_v1 = comparisons.create_comparison(
            {
                "title": "勘误前对齐",
                "left_observation_id": left["id"],
                "right_observation_id": right["id"],
            }
        )
    with request_scope_for("v-brief-1"):
        brief_v1 = briefs.create_brief(plot["id"], {"title": "勘误前简报"})

    with request_scope_for(
        "v-correction-create",
        "/api/corrections",
    ):
        correction = corrections.create_correction(
            {
                "observation_id": left["id"],
                "reason": "核对纸质台账，采收日期登记偏早三天",
                "changes": [{"stage": "harvest", "observed_on": "2026-09-05"}],
            }
        )
    with request_scope_for(
        "v-correction-adopt",
        f"/api/corrections/{correction['id']}/adopt",
    ):
        corrections.adopt_correction(correction["id"], {"revision": 1})

    naive_blocked = False
    try:
        with request_scope_for("v-comparison-naive"):
            comparisons.create_comparison(
                {
                    "title": "隐式重算",
                    "left_observation_id": left["id"],
                    "right_observation_id": right["id"],
                }
            )
    except Exception as exc:  # noqa: BLE001 - 需要稳定错误码
        naive_blocked = getattr(exc, "code", "") == "comparison_basis_superseded"

    with request_scope_for("v-comparison-2"):
        comparison_v2 = comparisons.create_comparison(
            {
                "title": "勘误后对齐",
                "left_observation_id": left["id"],
                "right_observation_id": right["id"],
                "supersedes_comparison_id": comparison_v1["id"],
            }
        )
    with request_scope_for("v-brief-2"):
        brief_v2 = briefs.create_brief(plot["id"], {"title": "勘误后简报"})

    # 关闭并以全新进程视角重新打开，验证恢复后的版本关系。
    repository.close()
    del repository, catalog, observations, comparisons, briefs, corrections
    reopened, catalog, observations, comparisons, briefs, corrections = build_stack(
        data_dir,
    )

    raw_left = reopened.read()["observations"][left["id"]]
    detail = observations.get_observation(left["id"])
    old_comparison = comparisons.get_comparison(comparison_v1["id"])
    new_comparison = comparisons.get_comparison(comparison_v2["id"])
    old_brief = briefs.get_brief(brief_v1["id"])
    new_brief = briefs.get_brief(brief_v2["id"])
    correction_record = corrections.get_correction(correction["id"])

    versions = reopened.list_entity_versions(
        kind="observation",
        identifier=left["id"],
    )
    correction_versions = reopened.list_entity_versions(
        kind="correction",
        identifier=correction["id"],
    )

    checks: list[tuple[str, bool, object, object]] = []

    def check(name: str, actual, expected) -> None:
        checks.append((name, actual == expected, actual, expected))

    raw_harvest = next(
        entry for entry in raw_left["entries"] if entry["stage"] == "harvest"
    )["observed_on"]
    check("原始季节志采收日期字节不变", raw_harvest, "2026-09-02")
    check("原始季节志修订号不变", raw_left["revision"], left["revision"])
    check(
        "当前事实采收日期为勘误值",
        detail["entry_map"]["harvest"]["observed_on"],
        "2026-09-05",
    )
    check(
        "谱系中采收期标记为已修正",
        next(
            line
            for line in detail["entry_lineage"]
            if line["stage"] == "harvest"
        )["revised"],
        True,
    )
    check("勘误链长度为一", detail["current_correction_seq"], 1)
    check(
        "旧图谱偏移冻结为 +5",
        next(
            item
            for item in old_comparison["stage_offsets"]
            if item["stage"] == "harvest"
        )["offset_days"],
        5,
    )
    check("旧图谱标记为历史", old_comparison["basis_status"], "superseded")
    check(
        "旧图谱指向新版",
        old_comparison["superseded_by_id"],
        comparison_v2["id"],
    )
    check(
        "新图谱偏移采用当前事实 +2",
        next(
            item
            for item in new_comparison["stage_offsets"]
            if item["stage"] == "harvest"
        )["offset_days"],
        2,
    )
    check("新图谱标记为当前", new_comparison["basis_status"], "current")
    check(
        "新图谱冻结的左侧勘误世代",
        new_comparison["left_basis"]["current_correction_id"],
        correction["id"],
    )
    check("隐式重算被拒绝", naive_blocked, True)
    check("旧简报标记为历史", old_brief["basis_status"], "superseded")
    old_brief_harvest = next(
        item
        for item in old_brief["payload"]["observations"]
        if item["id"] == left["id"]
    )["entry_map"]["harvest"]["observed_on"]
    check("旧简报正文仍冻结为旧日期", old_brief_harvest, "2026-09-02")
    check("新简报标记为当前", new_brief["basis_status"], "current")
    new_brief_harvest = next(
        item
        for item in new_brief["payload"]["observations"]
        if item["id"] == left["id"]
    )["entry_map"]["harvest"]["observed_on"]
    check("新简报采用当前日期", new_brief_harvest, "2026-09-05")
    check("勘误记录已采纳", correction_record["status"], "adopted")
    check(
        "勘误记录前序为首版（无）",
        correction_record["supersedes_correction_id"],
        None,
    )
    check("勘误采纳序号为一", correction_record["adoption_seq"], 1)
    check("季节志版本表停留在完成世代", len(versions["items"]), left["revision"])
    check("勘误版本表包含提议与采纳", len(correction_versions["items"]), 2)

    print("== 受控勘误版本关系验证 ==")
    width = max(len(name) for name, *_ in checks)
    failed = 0
    for name, ok, actual, expected in checks:
        print(f"[{'通过' if ok else '失败'}] {name.ljust(width)}  实际={actual!r}")
        if not ok:
            failed += 1
            print(f"       期望={expected!r}")
    print("-" * 60)
    audit = reopened.list_audit_events(resource_kind="correction")
    print(
        "审计链：",
        ", ".join(
            f"{item['action']}@{item['revision']}" for item in reversed(audit["items"])
        ),
    )
    print(
        "恢复后图谱：旧版 basis_status="
        f"{old_comparison['basis_status']} → 新版 basis_status="
        f"{new_comparison['basis_status']}"
    )

    reopened.close()
    if temporary:
        shutil.rmtree(temporary, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
