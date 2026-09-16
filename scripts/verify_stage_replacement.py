#!/usr/bin/env python3
"""连续阶段替换的版本关系自证脚本。

示例：某株树一条观察先被误选成“果实膨大期”（fruit_growth，05-20），
核对台账后第一次勘误确认它是 04-12 的“落瓣期”（petal_fall）；随后再次
核对，发现同一观察其实是 03-05 的“芽膨大期”（bud_swell）。于是形成
两跳替换链：

    fruit_growth(05-20) → petal_fall(04-12) → bud_swell(03-05)
    误录起点            中间阶段            最终正确阶段

脚本核对：
1. 当前轨道只保留最终阶段 bud_swell 且只出现一次；起点与中间阶段都移除；
2. 详情替换链完整记录两跳，中间阶段明确“从哪来、又去了哪”；谱系中起点为
   replaced_out、中间阶段为 replaced_transit、终点为 replaced_in；
3. 原始冻结事实仍保留最初误录阶段；每次替换前的旧比较/简报世代都被冻结、
   标记为历史，且只能显式接续新版；
4. 关闭数据库并以全新仓储重新打开后，谱系、旧分析和新分析仍能一条链对上。

用法：python3 scripts/verify_stage_replacement.py [--data-dir DIR]
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
from app.domain.stages import STAGE_BY_KEY
from app.persistence import Database, Repository
from app.security import RequestContext, request_scope


def scope(key: str, path: str | None = None):
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir")
    args = parser.parse_args()

    temporary = None
    if args.data_dir:
        data_dir = Path(args.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
    else:
        temporary = tempfile.mkdtemp(prefix="orchard-replace-")
        data_dir = Path(temporary)

    repository, catalog, observations, comparisons, briefs, corrections = build_stack(
        data_dir,
    )

    with scope("r-plot"):
        plot = catalog.create_plot(
            {
                "code": "OR-9301",
                "name": "连续替换验证园",
                "locality": "河湾北岭",
                "cultivar_focus": "秋白梨",
                "steward": "编研组",
                "planting_year": 2011,
                "note": "",
            }
        )
    trees = []
    for index, (code, cultivar) in enumerate(
        (("OR-9301-T01", "秋白梨"), ("OR-9301-T02", "蜜香梨")),
        start=1,
    ):
        with scope(f"r-tree-{index}"):
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
    with scope("r-plot-confirm"):
        catalog.confirm_plot(plot["id"], expected_revision=plot["revision"])

    def complete_season(seed: str, tree_id: str, entries):
        entries = sorted(entries, key=lambda item: (STAGE_BY_KEY[item[0]].rank, item[1]))
        with scope(f"r-season-{seed}"):
            record = observations.start_observation(
                {"tree_id": tree_id, "season": "2026", "observer": "验证员", "note": ""}
            )
        for index, (stage, observed_on) in enumerate(entries):
            note = "现场误录阶段" if seed == "left" and stage == "fruit_growth" else ""
            with scope(f"r-entry-{seed}-{index}"):
                record = observations.add_stage(
                    record["id"],
                    {
                        "stage": stage,
                        "observed_on": observed_on,
                        "confidence": 4,
                        "note": note,
                        "revision": record["revision"],
                    },
                )
        with scope(f"r-complete-{seed}"):
            return observations.complete_observation(
                record["id"], {"revision": record["revision"]}
            )

    # 左：误把观察登记成果实膨大期（05-20）
    left = complete_season(
        "left",
        trees[0]["id"],
        [
            ("bud_burst", "2026-03-10"),
            ("full_bloom", "2026-04-01"),
            ("fruit_set", "2026-04-18"),
            ("fruit_growth", "2026-05-20"),
            ("harvest", "2026-09-02"),
        ],
    )
    right = complete_season(
        "right",
        trees[1]["id"],
        [
            ("bud_swell", "2026-03-08"),
            ("bud_burst", "2026-03-15"),
            ("full_bloom", "2026-04-05"),
            ("petal_fall", "2026-04-15"),
            ("fruit_set", "2026-04-22"),
            ("harvest", "2026-09-07"),
        ],
    )

    # 第零代分析（基于误录阶段）
    with scope("r-cmp-0"):
        comparison_g0 = comparisons.create_comparison(
            {
                "title": "替换前图谱",
                "left_observation_id": left["id"],
                "right_observation_id": right["id"],
            }
        )
    with scope("r-brief-0"):
        brief_g0 = briefs.create_brief(plot["id"], {"title": "替换前简报"})

    def adopt_replace(key: str, source: str, target: str, observed_on: str, reason: str):
        with scope(f"{key}-create", "/api/corrections"):
            proposal = corrections.create_correction(
                {
                    "observation_id": left["id"],
                    "reason": reason,
                    "changes": [
                        {
                            "change_type": "replace",
                            "stage": source,
                            "correct_stage": target,
                            "observed_on": observed_on,
                            "confidence": 4,
                            "note": "",
                        }
                    ],
                }
            )
        with scope(
            f"{key}-adopt",
            f"/api/corrections/{proposal['id']}/adopt",
        ):
            corrections.adopt_correction(proposal["id"], {"revision": 1})
        return proposal

    # 第一代：fruit_growth → petal_fall
    correction_g1 = adopt_replace(
        "r-rep-1",
        "fruit_growth",
        "petal_fall",
        "2026-04-12",
        "该观察实为04-12落瓣期，被误记为果实膨大期",
    )
    with scope("r-cmp-1"):
        comparison_g1 = comparisons.create_comparison(
            {
                "title": "第一次替换后图谱",
                "left_observation_id": left["id"],
                "right_observation_id": right["id"],
                "supersedes_comparison_id": comparison_g0["id"],
            }
        )
    with scope("r-brief-1"):
        brief_g1 = briefs.create_brief(plot["id"], {"title": "第一次替换后简报"})

    # 第二代：petal_fall → bud_swell（连续替换中间阶段）
    correction_g2 = adopt_replace(
        "r-rep-2",
        "petal_fall",
        "bud_swell",
        "2026-03-05",
        "再次核对，同一观察实为03-05芽膨大期",
    )
    # 第二次替换后隐式重算必须被拒绝
    implicit_blocked = False
    try:
        with scope("r-cmp-naive"):
            comparisons.create_comparison(
                {
                    "title": "尝试悄悄重算",
                    "left_observation_id": left["id"],
                    "right_observation_id": right["id"],
                }
            )
    except Exception as exc:  # noqa: BLE001
        implicit_blocked = getattr(exc, "code", "") == "comparison_basis_superseded"

    with scope("r-cmp-2"):
        comparison_g2 = comparisons.create_comparison(
            {
                "title": "第二次替换后图谱",
                "left_observation_id": left["id"],
                "right_observation_id": right["id"],
                "supersedes_comparison_id": comparison_g1["id"],
            }
        )
    with scope("r-brief-2"):
        brief_g2 = briefs.create_brief(plot["id"], {"title": "第二次替换后简报"})

    # —— 关闭并以全新进程视角重新打开 ——
    repository.close()
    del repository, catalog, observations, comparisons, briefs, corrections
    reopened, catalog, observations, comparisons, briefs, corrections = build_stack(
        data_dir,
    )

    raw_left = reopened.read()["observations"][left["id"]]
    detail = observations.get_observation(left["id"])
    chain = detail["replacement_chain"]
    lineage = {line["stage"]: line for line in detail["entry_lineage"]}
    g0 = comparisons.get_comparison(comparison_g0["id"])
    g1 = comparisons.get_comparison(comparison_g1["id"])
    g2 = comparisons.get_comparison(comparison_g2["id"])
    b0 = briefs.get_brief(brief_g0["id"])
    b1 = briefs.get_brief(brief_g1["id"])
    b2 = briefs.get_brief(brief_g2["id"])

    checks: list[tuple[str, bool, object, object]] = []

    def check(name: str, actual, expected) -> None:
        checks.append((name, actual == expected, actual, expected))

    current_stages = [entry["stage"] for entry in detail["entries"]]

    # 当前轨道只剩最终阶段
    check("当前轨道含最终阶段 bud_swell", "bud_swell" in current_stages, True)
    check("当前轨道移除中间阶段 petal_fall", "petal_fall" not in current_stages, True)
    check("当前轨道移除起点 fruit_growth", "fruit_growth" not in current_stages, True)
    check(
        "最终阶段只出现一次",
        sum(1 for stage in current_stages if stage == "bud_swell"),
        1,
    )
    check(
        "最终阶段日期为真值",
        detail["entry_map"]["bud_swell"]["observed_on"],
        "2026-03-05",
    )

    # 替换链两跳完整
    check("替换链跳数", len(chain), 2)
    check(
        "替换链两跳顺序",
        [(hop["from_stage"], hop["to_stage"]) for hop in chain],
        [("fruit_growth", "petal_fall"), ("petal_fall", "bud_swell")],
    )
    check("第一跳的中间阶段已非当前", chain[0]["to_still_current"], False)
    check("第二跳的最终阶段仍为当前", chain[1]["to_still_current"], True)
    check("第一跳勘误", chain[0]["correction_id"], correction_g1["id"])
    check("第二跳勘误", chain[1]["correction_id"], correction_g2["id"])
    check("第一跳日期", chain[0]["observed_on"], "2026-04-12")
    check("第二跳日期", chain[1]["observed_on"], "2026-03-05")

    # 谱系三段齐全
    check("起点谱系 replaced_out", lineage["fruit_growth"]["status"], "replaced_out")
    check("起点去向", lineage["fruit_growth"]["replacement_stage"], "petal_fall")
    check(
        "中间阶段谱系 replaced_transit",
        lineage["petal_fall"]["status"],
        "replaced_transit",
    )
    check(
        "中间阶段来源",
        lineage["petal_fall"]["replaced_from_stage"],
        "fruit_growth",
    )
    check(
        "中间阶段后续去向",
        lineage["petal_fall"]["replacement_stage"],
        "bud_swell",
    )
    check("中间阶段无当前值", lineage["petal_fall"]["current"], None)
    check("终点谱系 replaced_in", lineage["bud_swell"]["status"], "replaced_in")
    check("终点来源", lineage["bud_swell"]["replaced_from_stage"], "petal_fall")
    check("终点无后续去向", lineage["bud_swell"]["replacement_stage"], None)
    check(
        "终点当前日期",
        lineage["bud_swell"]["current"]["observed_on"],
        "2026-03-05",
    )

    # 原始冻结事实仍是最初误录阶段
    check(
        "原始记录保留误录起点",
        "fruit_growth" in [entry["stage"] for entry in raw_left["entries"]],
        True,
    )
    check("原始季节志修订号不变", raw_left["revision"], left["revision"])

    # 三代比较世代一条链
    check("第零代图谱为历史", g0["basis_status"], "superseded")
    check("第一代图谱为历史", g1["basis_status"], "superseded")
    check("第二代图谱为当前", g2["basis_status"], "current")
    check("第零代被第一代接续", g0["superseded_by_id"], comparison_g1["id"])
    check("第一代被第二代接续", g1["superseded_by_id"], comparison_g2["id"])
    check("第二代无后续", g2["superseded_by_id"], None)
    check(
        "第二代冻结勘误世代",
        g2["left_basis"]["current_correction_id"],
        correction_g2["id"],
    )
    check("隐式重算被拒绝", implicit_blocked, True)
    current_comparisons = [
        item
        for item in comparisons.list_comparisons()["items"]
        if item["basis_status"] == "current"
    ]
    check("只有一份当前图谱", len(current_comparisons), 1)

    # 三代简报世代
    check("第零代简报为历史", b0["basis_status"], "superseded")
    check("第一代简报为历史", b1["basis_status"], "superseded")
    check("第二代简报为当前", b2["basis_status"], "current")
    final_left = next(
        item for item in b2["payload"]["observations"] if item["id"] == left["id"]
    )
    check("当前简报采用最终阶段", "bud_swell" in final_left["entry_map"], True)
    check(
        "当前简报替换链完整",
        [
            (hop["from_stage"], hop["to_stage"])
            for hop in final_left["replacement_chain"]
        ],
        [("fruit_growth", "petal_fall"), ("petal_fall", "bud_swell")],
    )
    first_left = next(
        item for item in b0["payload"]["observations"] if item["id"] == left["id"]
    )
    check("第零代简报保留误录阶段", "fruit_growth" in first_left["entry_map"], True)

    print("== 连续阶段替换 · 完整谱系版本关系验证 ==")
    width = max(len(name) for name, *_ in checks)
    failed = 0
    for name, ok, actual, expected in checks:
        print(f"[{'通过' if ok else '失败'}] {name.ljust(width)}  实际={actual!r}")
        if not ok:
            failed += 1
            print(f"       期望={expected!r}")
    print("-" * 64)
    print(
        "替换链：fruit_growth(05-20) → petal_fall(04-12, 中间) → "
        "bud_swell(03-05, 当前)"
    )
    print(
        "图谱链：第零代(误录) → 第一代 → 第二代(当前)，"
        "关闭重开后谱系、旧分析、新分析仍一一对应"
    )

    reopened.close()
    if temporary:
        shutil.rmtree(temporary, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
