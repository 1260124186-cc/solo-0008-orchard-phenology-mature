#!/usr/bin/env python3
"""错选阶段受控替换的版本关系自证脚本。

示例：某株树 05-20 的观察被现场误选成“果实膨大期”（fruit_growth），
核对纸质台账后确认它实际是 04-12 的“落瓣期”（petal_fall）。通过受控勘误
以正确阶段整体替换误录阶段，随后核对：

1. 替换前：当前轨道含误录阶段 fruit_growth、不含 petal_fall；旧比较与旧简报
   都基于这一世代冻结；
2. 采纳替换：当前轨道移除 fruit_growth、petal_fall 只出现一次；原始冻结事实
   仍保留误录阶段与其日期；
3. 旧分析不被原地改写（共同阶段集合与偏移不变），只有显式接续才能生成新分析，
   新分析采用正确阶段；同一对季节志至多一份当前图谱；
4. 关闭数据库并以全新仓储重新打开，恢复后的原始记录、勘误、新旧分析仍能对应。

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
                "name": "阶段替换验证园",
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

    # 左：误把落瓣期观察登记成果实膨大期（05-20）；真值是 04-12 落瓣期。
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
            ("bud_burst", "2026-03-15"),
            ("full_bloom", "2026-04-05"),
            ("petal_fall", "2026-04-15"),
            ("fruit_set", "2026-04-22"),
            ("harvest", "2026-09-07"),
        ],
    )

    # —— 替换前：冻结旧分析 ——
    with scope("r-cmp-before"):
        comparison_before = comparisons.create_comparison(
            {
                "title": "替换前图谱",
                "left_observation_id": left["id"],
                "right_observation_id": right["id"],
            }
        )
    with scope("r-brief-before"):
        brief_before = briefs.create_brief(plot["id"], {"title": "替换前简报"})

    before = {
        item["stage"]: item["offset_days"]
        for item in comparison_before["stage_offsets"]
    }

    # —— 提出并采纳阶段替换勘误 ——
    with scope("r-correction-create", "/api/corrections"):
        correction = corrections.create_correction(
            {
                "observation_id": left["id"],
                "reason": "核对纸质台账，该观察实为04-12落瓣期，被误记为05-20果实膨大期",
                "changes": [
                    {
                        "change_type": "replace",
                        "stage": "fruit_growth",
                        "correct_stage": "petal_fall",
                        "observed_on": "2026-04-12",
                        "confidence": 4,
                        "note": "阶段更正为落瓣期",
                    }
                ],
            }
        )
    with scope(
        "r-correction-adopt",
        f"/api/corrections/{correction['id']}/adopt",
    ):
        corrections.adopt_correction(correction["id"], {"revision": 1})

    # 隐式重算必须被拒绝。
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

    with scope("r-cmp-after"):
        comparison_after = comparisons.create_comparison(
            {
                "title": "替换后图谱",
                "left_observation_id": left["id"],
                "right_observation_id": right["id"],
                "supersedes_comparison_id": comparison_before["id"],
            }
        )
    with scope("r-brief-after"):
        brief_after = briefs.create_brief(plot["id"], {"title": "替换后简报"})

    # —— 关闭并以全新进程视角重新打开 ——
    repository.close()
    del repository, catalog, observations, comparisons, briefs, corrections
    reopened, catalog, observations, comparisons, briefs, corrections = build_stack(
        data_dir,
    )

    raw_left = reopened.read()["observations"][left["id"]]
    detail = observations.get_observation(left["id"])
    old_comparison = comparisons.get_comparison(comparison_before["id"])
    new_comparison = comparisons.get_comparison(comparison_after["id"])
    old_brief = briefs.get_brief(brief_before["id"])
    new_brief = briefs.get_brief(brief_after["id"])
    lineage = {line["stage"]: line for line in detail["entry_lineage"]}

    checks: list[tuple[str, bool, object, object]] = []

    def check(name: str, actual, expected) -> None:
        checks.append((name, actual == expected, actual, expected))

    current_stages = [entry["stage"] for entry in detail["entries"]]
    frozen_stages = [entry["stage"] for entry in detail["frozen_entries"]]
    raw_stages = [entry["stage"] for entry in raw_left["entries"]]
    after_offsets = {
        item["stage"]: item["offset_days"]
        for item in new_comparison["stage_offsets"]
    }

    # 原始记录
    check("原始记录仍含误录阶段 fruit_growth", "fruit_growth" in raw_stages, True)
    check("原始记录不含正确阶段 petal_fall", "petal_fall" not in raw_stages, True)
    check("原始季节志修订号未变", raw_left["revision"], left["revision"])

    # 替换后的当前事实
    check("当前轨道移除误录阶段", "fruit_growth" not in current_stages, True)
    check("当前轨道含正确阶段", "petal_fall" in current_stages, True)
    check(
        "正确阶段只出现一次",
        sum(1 for stage in current_stages if stage == "petal_fall"),
        1,
    )
    check(
        "正确阶段日期为真值",
        detail["entry_map"]["petal_fall"]["observed_on"],
        "2026-04-12",
    )
    check("冻结事实仍保留 fruit_growth", "fruit_growth" in frozen_stages, True)
    check("冻结事实不含 petal_fall", "petal_fall" not in frozen_stages, True)
    check("误录阶段谱系 replaced_out", lineage["fruit_growth"]["status"], "replaced_out")
    check(
        "误录阶段指向正确阶段",
        lineage["fruit_growth"]["replacement_stage"],
        "petal_fall",
    )
    check("正确阶段谱系 replaced_in", lineage["petal_fall"]["status"], "replaced_in")
    check(
        "正确阶段来源是误录阶段",
        lineage["petal_fall"]["replacement_stage"],
        "fruit_growth",
    )

    # 旧分析冻结、新分析接续
    check("旧图谱保持历史世代", old_comparison["basis_status"], "superseded")
    check(
        "旧图谱共同阶段与偏移未被改写",
        {item["stage"]: item["offset_days"] for item in old_comparison["stage_offsets"]},
        before,
    )
    check("隐式重算被拒绝", implicit_blocked, True)
    check("旧图谱指向新版", old_comparison["superseded_by_id"], comparison_after["id"])
    check("新图谱为当前世代", new_comparison["basis_status"], "current")
    check("新图谱不再含误录阶段", "fruit_growth" not in after_offsets, True)
    check("新图谱含正确阶段", "petal_fall" in after_offsets, True)
    check("新图谱落瓣期偏移为右04-15减左04-12=3", after_offsets["petal_fall"], 3)
    check(
        "新图谱冻结的勘误世代",
        new_comparison["left_basis"]["current_correction_id"],
        correction["id"],
    )
    current_count = sum(
        1
        for item in comparisons.list_comparisons()["items"]
        if item["basis_status"] == "current"
    )
    check("同一对季节志只有一份当前图谱", current_count, 1)

    # 旧简报冻结、新简报采用正确阶段
    old_left = next(
        item for item in old_brief["payload"]["observations"] if item["id"] == left["id"]
    )
    new_left = next(
        item for item in new_brief["payload"]["observations"] if item["id"] == left["id"]
    )
    check("旧简报为历史快照", old_brief["basis_status"], "superseded")
    check("旧简报保留误录阶段", "fruit_growth" in old_left["entry_map"], True)
    check("旧简报不含正确阶段", "petal_fall" not in old_left["entry_map"], True)
    check("新简报为当前快照", new_brief["basis_status"], "current")
    check("新简报采用正确阶段", "petal_fall" in new_left["entry_map"], True)
    check("新简报移除误录阶段", "fruit_growth" not in new_left["entry_map"], True)

    print("== 错选阶段受控替换 · 版本关系验证 ==")
    width = max(len(name) for name, *_ in checks)
    failed = 0
    for name, ok, actual, expected in checks:
        print(f"[{'通过' if ok else '失败'}] {name.ljust(width)}  实际={actual!r}")
        if not ok:
            failed += 1
            print(f"       期望={expected!r}")
    print("-" * 64)
    print(
        f"谱系：fruit_growth(05-20, 误录) → petal_fall(04-12, 正确)，"
        f"图谱 {comparison_before['id'][:10]}…(历史) → "
        f"{comparison_after['id'][:10]}…(当前)"
    )

    reopened.close()
    if temporary:
        shutil.rmtree(temporary, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
