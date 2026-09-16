#!/usr/bin/env python3
"""阶段替换“回到最初阶段”的版本关系自证脚本。

示例：某株树一条观察先被误选成“果实膨大期”（fruit_growth，05-20），
核对台账后第一次勘误确认它是 04-12 的“落瓣期”（petal_fall）；随后再次
核对，确认同一观察其实仍是“果实膨大期”，但日期应修正为 05-22。于是形成
一个环回替换链：

    fruit_growth(05-20) → petal_fall(04-12) → fruit_growth(05-22)
    最初阶段             中间阶段            最终（改回最初阶段）

脚本核对：
1. 当前轨道采用最初阶段 fruit_growth 且只出现一次，中间阶段 petal_fall 移除；
2. 最终阶段在谱系中只有一行，状态 restored，只显示来源（petal_fall）、不显示
   后续去向；同名阶段不会同时出现“移出”和“进入”两行；
3. 替换链两跳按采纳顺序排列，每跳“是否仍在当前轨道”以最终事实集计算：
   第一跳的 petal_fall 已非当前，第二跳的 fruit_growth 仍为当前；
4. 原始冻结事实保留最初误录日期；每代比较/简报都被冻结、只能显式接续新版；
5. 关闭数据库并以全新仓储重新打开后，链条、当前轨道、旧分析和新分析仍一致。

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
            ("fruit_growth", "2026-05-25"),
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

    # 第二代：petal_fall → fruit_growth（回到最初阶段，日期修正为 05-22）
    correction_g2 = adopt_replace(
        "r-rep-2",
        "petal_fall",
        "fruit_growth",
        "2026-05-22",
        "复核后确认同一观察仍是果实膨大期，日期修正为05-22",
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
    lineage_counts: dict[str, int] = {}
    for line in detail["entry_lineage"]:
        lineage_counts[line["stage"]] = lineage_counts.get(line["stage"], 0) + 1

    # 当前轨道改回最初阶段，且只出现一次
    check("当前轨道采用最初阶段 fruit_growth", "fruit_growth" in current_stages, True)
    check("当前轨道移除中间阶段 petal_fall", "petal_fall" not in current_stages, True)
    check(
        "最初阶段在当前轨道只出现一次",
        sum(1 for stage in current_stages if stage == "fruit_growth"),
        1,
    )
    check(
        "最初阶段当前日期为修正值",
        detail["entry_map"]["fruit_growth"]["observed_on"],
        "2026-05-22",
    )
    check("谱系中最初阶段只有一行", lineage_counts.get("fruit_growth"), 1)
    check("谱系中中间阶段只有一行", lineage_counts.get("petal_fall"), 1)

    # 替换链两跳完整；to_still_current 按最终事实集判定
    check("替换链跳数", len(chain), 2)
    check(
        "替换链两跳顺序",
        [(hop["from_stage"], hop["to_stage"]) for hop in chain],
        [("fruit_growth", "petal_fall"), ("petal_fall", "fruit_growth")],
    )
    check("第一跳进入的中间阶段已非当前", chain[0]["to_still_current"], False)
    check("第二跳改回的最初阶段仍为当前", chain[1]["to_still_current"], True)
    check("第一跳勘误", chain[0]["correction_id"], correction_g1["id"])
    check("第二跳勘误", chain[1]["correction_id"], correction_g2["id"])
    check("第一跳日期", chain[0]["observed_on"], "2026-04-12")
    check("第二跳日期", chain[1]["observed_on"], "2026-05-22")

    # 谱系：中间阶段 transit；最终阶段 restored 单行、只来不去
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
        "fruit_growth",
    )
    check("中间阶段无当前值", lineage["petal_fall"]["current"], None)
    check("最终阶段谱系 restored", lineage["fruit_growth"]["status"], "restored")
    check(
        "最终阶段只显示来源",
        lineage["fruit_growth"]["replaced_from_stage"],
        "petal_fall",
    )
    check("最终阶段不显示后续去向", lineage["fruit_growth"]["replacement_stage"], None)
    check(
        "最终阶段当前日期",
        lineage["fruit_growth"]["current"]["observed_on"],
        "2026-05-22",
    )
    check(
        "最终阶段仍可查最初冻结日期",
        lineage["fruit_growth"]["frozen"]["observed_on"],
        "2026-05-20",
    )

    # 原始冻结事实仍是最初误录阶段与日期
    check(
        "原始记录保留最初阶段",
        "fruit_growth" in [entry["stage"] for entry in raw_left["entries"]],
        True,
    )
    check(
        "原始记录日期仍为冻结值",
        next(
            entry
            for entry in raw_left["entries"]
            if entry["stage"] == "fruit_growth"
        )["observed_on"],
        "2026-05-20",
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
    final_offsets = {
        item["stage"]: item["offset_days"] for item in g2["stage_offsets"]
    }
    check("当前图谱不含中间阶段", "petal_fall" not in final_offsets, True)
    check("当前图谱采用回到的最初阶段", "fruit_growth" in final_offsets, True)
    check("当前图谱果实膨大期偏移为右05-25减左05-22", final_offsets["fruit_growth"], 3)
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
    check("当前简报采用最初阶段", "fruit_growth" in final_left["entry_map"], True)
    check(
        "当前简报不含中间阶段",
        "petal_fall" not in final_left["entry_map"],
        True,
    )
    check(
        "当前简报替换链完整",
        [
            (hop["from_stage"], hop["to_stage"])
            for hop in final_left["replacement_chain"]
        ],
        [("fruit_growth", "petal_fall"), ("petal_fall", "fruit_growth")],
    )
    first_left = next(
        item for item in b0["payload"]["observations"] if item["id"] == left["id"]
    )
    check(
        "第零代简报保留最初冻结日期",
        first_left["entry_map"]["fruit_growth"]["observed_on"],
        "2026-05-20",
    )

    print("== 回到最初阶段 · 环回替换版本关系验证 ==")
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
        "fruit_growth(05-22, 改回最初阶段)"
    )
    print(
        "图谱链：第零代(误录) → 第一代(中间阶段) → 第二代(改回)，"
        "关闭重开后谱系、当前轨道、旧分析、新分析仍一一对应"
    )

    reopened.close()
    if temporary:
        shutil.rmtree(temporary, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
