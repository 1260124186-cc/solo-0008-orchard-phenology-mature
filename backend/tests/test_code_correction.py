"""园区编号级联修正的领域与用例测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.application import (
    BriefService,
    CatalogService,
    ComparisonService,
    ObservationService,
)
from app.errors import ConflictError, PreconditionError, ValidationError
from app.persistence import Database, Repository
from app.security import RequestContext, request_scope


def _context(
    *,
    actor: str = "local-admin",
    key: str | None = None,
    method: str = "PUT",
    path: str = "/api/test",
) -> object:
    return request_scope(
        RequestContext(
            actor_id=actor,
            idempotency_key=key,
            request_method=method,
            request_path=path,
            request_hash=f"{actor}:{method}:{path}:{key}",
            route_template=path,
        )
    )


PLOT_PAYLOAD = {
    "name": "北岭老梨园",
    "locality": "河湾村北岭东侧",
    "cultivar_focus": "黄皮秋梨",
    "steward": "县农业志编研组",
    "planting_year": 2009,
    "note": "",
}
STAGES = ["bud_burst", "full_bloom", "fruit_set", "harvest"]
DATES = ["2026-03-14", "2026-04-06", "2026-04-24", "2026-09-08"]


class CodeCorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        data_dir = Path(self.temporary.name)
        self.repository = Repository(Database(data_dir / "atlas.sqlite3"))
        self.repository.open()
        self.catalog = CatalogService(self.repository)
        self.observations = ObservationService(self.repository)
        self.comparisons = ComparisonService(self.repository)
        self.briefs = BriefService(self.repository)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _plot(self, code: str) -> dict:
        with _context(path="/api/plots"):
            return self.catalog.create_plot({**PLOT_PAYLOAD, "code": code})

    def _tree(self, plot_id: str, code: str, *, year: int = 2009) -> dict:
        with _context(path="/api/trees"):
            return self.catalog.create_tree(
                {
                    "plot_id": plot_id,
                    "code": code,
                    "cultivar": "黄皮秋梨",
                    "rootstock": "杜梨",
                    "planting_year": year,
                    "status": "active",
                    "note": "",
                }
            )

    def _completed_observation(self, tree: dict, season: str = "2026") -> dict:
        with _context(path="/api/observations"):
            observation = self.observations.start_observation(
                {
                    "tree_id": tree["id"],
                    "season": season,
                    "observer": "周岚",
                    "note": "",
                }
            )
        for stage, observed_on in zip(STAGES, DATES):
            with _context(path=f"/api/observations/{observation['id']}/stages"):
                observation = self.observations.add_stage(
                    observation["id"],
                    {
                        "stage": stage,
                        "observed_on": observed_on,
                        "confidence": 4,
                        "note": "",
                        "revision": observation["revision"],
                    },
                )
        with _context(path=f"/api/observations/{observation['id']}/complete"):
            observation = self.observations.complete_observation(
                observation["id"],
                {"revision": observation["revision"]},
            )
        return observation

    def test_cascade_updates_plot_and_all_trees_in_one_transaction(self) -> None:
        plot = self._plot("OR-3101")
        first = self._tree(plot["id"], "OR-3101-T01")
        second = self._tree(plot["id"], "OR-3101-T02")
        revision_before = self.repository.read()["revision"]

        with _context(path=f"/api/plots/{plot['id']}/code-correction"):
            report = self.catalog.correct_plot_code(
                plot["id"],
                {
                    "code": "OR-3202",
                    "reason": "录入笔误",
                    "revision": plot["revision"],
                },
            )

        state = self.repository.read()
        self.assertEqual(state["plots"][plot["id"]]["code"], "OR-3202")
        self.assertEqual(
            sorted(tree["code"] for tree in state["trees"].values()),
            ["OR-3202-T01", "OR-3202-T02"],
        )
        self.assertEqual([item["old_code"] for item in report["trees"]], [
            "OR-3101-T01",
            "OR-3101-T02",
        ])
        # 园区与两株植株在同一个事务提交，全局修订号只前进一次。
        self.assertEqual(state["revision"], revision_before + 1)
        plot_aliases = state["plots"][plot["id"]]["code_aliases"]
        self.assertEqual(plot_aliases[0]["code"], "OR-3101")
        self.assertEqual(plot_aliases[0]["changed_to"], "OR-3202")
        self.assertEqual(plot_aliases[0]["reason"], "录入笔误")
        self.assertEqual(plot_aliases[0]["cause"], "plot_code_correction")
        tree_reports = {item["tree_id"]: item for item in report["trees"]}
        for tree_id, old_code, new_code in (
            (first["id"], "OR-3101-T01", "OR-3202-T01"),
            (second["id"], "OR-3101-T02", "OR-3202-T02"),
        ):
            aliases = state["trees"][tree_id]["code_aliases"]
            self.assertEqual(aliases[0]["code"], old_code)
            self.assertEqual(aliases[0]["changed_to"], new_code)
            self.assertEqual(aliases[0]["cause"], "plot_code_correction")
            self.assertEqual(state["trees"][tree_id]["revision"], 2)
            self.assertEqual(tree_reports[tree_id]["old_code"], old_code)
            self.assertEqual(tree_reports[tree_id]["new_code"], new_code)

        versions = self.repository.list_entity_versions(
            kind="plot",
            identifier=plot["id"],
        )
        self.assertEqual([item["revision"] for item in versions["items"]], [2, 1])
        audit = self.repository.list_audit_events(
            resource_kind="plot",
            resource_id=plot["id"],
        )
        self.assertEqual(audit["items"][0]["action"], "recode")

    def test_unresolved_tree_blocks_everything_and_lists_pending(self) -> None:
        plot = self._plot("OR-3301")
        self._tree(plot["id"], "OR-3301-T01")
        stray = self._tree(plot["id"], "OR-3301-T02")

        # 模拟历史遗留：植株编号前缀与园区不一致。
        def mutate(state: dict) -> None:
            tree = state["trees"][stray["id"]]
            state["trees"][stray["id"]] = {**tree, "code": "ZZ-9001-T02"}

        self.repository.atomic_update(mutate)
        revision_before = self.repository.read()["revision"]

        preview = self.catalog.preview_plot_code_correction(
            plot["id"],
            {"code": "OR-3401"},
        )
        self.assertFalse(preview["applicable"])
        self.assertEqual(len(preview["pending"]), 1)
        self.assertEqual(preview["pending"][0]["tree_id"], stray["id"])
        self.assertEqual(preview["pending"][0]["tree_code"], "ZZ-9001-T02")
        # 级联计划只包含前缀一致的植株，未决植株只进入待处理清单。
        self.assertEqual(len(preview["changes"]), 1)
        self.assertEqual(preview["changes"][0]["tree_code"], "OR-3301-T01")

        with _context(path="/api/plots/x/code-correction"):
            with self.assertRaises(PreconditionError) as raised:
                self.catalog.correct_plot_code(
                    plot["id"],
                    {"code": "OR-3401", "revision": plot["revision"]},
                )
        self.assertEqual(raised.exception.code, "tree_code_unresolved")
        pending = raised.exception.details["pending"]
        self.assertEqual(pending[0]["tree_id"], stray["id"])

        # 整批不落库：园区与所有植株仍是旧编号，全局修订号不变。
        state = self.repository.read()
        self.assertEqual(state["revision"], revision_before)
        self.assertEqual(state["plots"][plot["id"]]["code"], "OR-3301")
        self.assertEqual(
            sorted(tree["code"] for tree in state["trees"].values()),
            ["OR-3301-T01", "ZZ-9001-T02"],
        )

    def test_repair_tree_code_then_cascade_succeeds(self) -> None:
        plot = self._plot("OR-3501")
        stray = self._tree(plot["id"], "OR-3501-T09")

        def mutate(state: dict) -> None:
            tree = state["trees"][stray["id"]]
            state["trees"][stray["id"]] = {**tree, "code": "ZZ-9002-T09"}

        self.repository.atomic_update(mutate)

        with _context(path="/api/trees/x/code-correction"):
            report = self.catalog.correct_tree_code(
                stray["id"],
                {"code": "OR-3501-T09", "reason": "编号归位", "revision": 1},
            )
        self.assertEqual(report["old_code"], "ZZ-9002-T09")
        self.assertEqual(report["new_code"], "OR-3501-T09")

        with _context(path="/api/plots/x/code-correction"):
            self.catalog.correct_plot_code(
                plot["id"],
                {"code": "OR-3601", "reason": "园区笔误", "revision": plot["revision"]},
            )
        state = self.repository.read()
        self.assertEqual(state["plots"][plot["id"]]["code"], "OR-3601")
        self.assertEqual(state["trees"][stray["id"]]["code"], "OR-3601-T09")
        causes = [
            alias["cause"]
            for alias in state["trees"][stray["id"]]["code_aliases"]
        ]
        self.assertEqual(causes, ["tree_code_repair", "plot_code_correction"])

    def test_tree_repair_rejects_wrong_prefix_and_duplicate(self) -> None:
        plot = self._plot("OR-3701")
        first = self._tree(plot["id"], "OR-3701-T01")
        second = self._tree(plot["id"], "OR-3701-T02")

        def mutate(state: dict) -> None:
            tree = state["trees"][second["id"]]
            state["trees"][second["id"]] = {**tree, "code": "ZZ-9003-T02"}

        self.repository.atomic_update(mutate)

        with _context(path="/api/trees/x/code-correction"):
            with self.assertRaises(ValidationError):
                self.catalog.correct_tree_code(
                    second["id"],
                    {"code": "OR-9999-T02", "revision": 1},
                )
            with self.assertRaises(ConflictError) as duplicate:
                self.catalog.correct_tree_code(
                    second["id"],
                    {"code": "OR-3701-T01", "revision": 1},
                )
        self.assertEqual(duplicate.exception.code, "tree_code_exists")
        # 失败不留痕迹。
        self.assertEqual(
            self.repository.read()["trees"][second["id"]]["code"],
            "ZZ-9003-T02",
        )
        self.assertEqual(first["code"], "OR-3701-T01")

    def test_duplicate_plot_code_rejects_without_partial_update(self) -> None:
        plot = self._plot("OR-3801")
        self._tree(plot["id"], "OR-3801-T01")
        other = self._plot("OR-3802")
        revision_before = self.repository.read()["revision"]

        with _context(path="/api/plots/x/code-correction"):
            with self.assertRaises(ConflictError) as raised:
                self.catalog.correct_plot_code(
                    plot["id"],
                    {"code": "OR-3802", "revision": plot["revision"]},
                )
        self.assertEqual(raised.exception.code, "plot_code_exists")
        blockers = raised.exception.details["blockers"]
        self.assertTrue(any(item["plot_id"] == other["id"] for item in blockers))

        state = self.repository.read()
        self.assertEqual(state["revision"], revision_before)
        self.assertEqual(state["plots"][plot["id"]]["code"], "OR-3801")
        self.assertEqual(
            state["trees"][next(iter(state["trees"]))]["code"],
            "OR-3801-T01",
        )

    def test_concurrent_revision_conflict_leaves_no_change(self) -> None:
        plot = self._plot("OR-3901")
        self._tree(plot["id"], "OR-3901-T01")
        revision_before = self.repository.read()["revision"]

        # 其他请求先把园区修订号推进。
        with _context(method="PATCH", path="/api/plots/x"):
            self.catalog.update_plot(
                plot["id"],
                {"name": "更新后的园区", "revision": plot["revision"]},
            )

        with _context(path="/api/plots/x/code-correction"):
            with self.assertRaises(ConflictError) as raised:
                self.catalog.correct_plot_code(
                    plot["id"],
                    {"code": "OR-3999", "revision": plot["revision"]},
                )
        self.assertEqual(raised.exception.code, "revision_conflict")
        state = self.repository.read()
        self.assertEqual(state["plots"][plot["id"]]["code"], "OR-3901")
        self.assertEqual(state["revision"], revision_before + 1)

    def test_stale_plan_fingerprint_is_rejected(self) -> None:
        plot = self._plot("OR-4001")
        self._tree(plot["id"], "OR-4001-T01")
        preview = self.catalog.preview_plot_code_correction(
            plot["id"],
            {"code": "OR-4101"},
        )
        with _context(method="PATCH", path="/api/plots/x"):
            self.catalog.update_plot(
                plot["id"],
                {"name": "并行修改", "revision": plot["revision"]},
            )
        with _context(path="/api/plots/x/code-correction"):
            with self.assertRaises(ConflictError) as raised:
                self.catalog.correct_plot_code(
                    plot["id"],
                    {
                        "code": "OR-4101",
                        "revision": plot["revision"] + 1,
                        "fingerprint": preview["fingerprint"],
                    },
                )
        self.assertEqual(raised.exception.code, "correction_plan_stale")

    def test_confirmed_plot_cannot_be_recoded(self) -> None:
        plot = self._plot("OR-4201")
        tree = self._tree(plot["id"], "OR-4201-T01")
        with _context(path=f"/api/plots/{plot['id']}/confirm"):
            self.catalog.confirm_plot(plot["id"], expected_revision=plot["revision"])
        with _context(path="/api/plots/x/code-correction"):
            with self.assertRaises(PreconditionError) as raised:
                self.catalog.correct_plot_code(
                    plot["id"],
                    {"code": "OR-4301", "revision": 2},
                )
        self.assertEqual(raised.exception.code, "plot_not_editable")
        self.assertEqual(
            self.repository.read()["trees"][tree["id"]]["code"],
            "OR-4201-T01",
        )

    def test_comparison_keeps_generation_time_codes_but_stays_explainable(self) -> None:
        plot = self._plot("OR-4401")
        first = self._tree(plot["id"], "OR-4401-T01")
        second = self._tree(plot["id"], "OR-4401-T02", year=2010)
        left = self._completed_observation(first)
        right = self._completed_observation(second)

        with _context(path="/api/comparisons"):
            comparison = self.comparisons.create_comparison(
                {
                    "title": "同园两株对齐",
                    "left_observation_id": left["id"],
                    "right_observation_id": right["id"],
                }
            )
        self.assertIn("OR-4401-T01", comparison["left_label"])

        with _context(path="/api/plots/x/code-correction"):
            self.catalog.correct_plot_code(
                plot["id"],
                {"code": "OR-4501", "reason": "笔误", "revision": plot["revision"]},
            )

        stored = self.repository.read()["comparisons"][comparison["id"]]
        # 历史比较继续使用生成时编号。
        self.assertIn("OR-4401-T01", stored["left_label"])
        self.assertIn("OR-4401-T02", stored["right_label"])
        self.assertIn("OR-4401-T01", stored["summary"]["sentence"])

        report = self.catalog.identity_report()
        self.assertTrue(report["unique"])
        self.assertEqual(report["unresolved_trees"], [])
        historical = next(
            item
            for item in report["historical_objects"]
            if item["kind"] == "comparison" and item["id"] == comparison["id"]
        )
        self.assertTrue(historical["explainable"])
        self.assertEqual(historical["left_label"], stored["left_label"])

        # 实时季节志列表改用新编号。
        live = self.observations.list_observations(plot_id=plot["id"])
        self.assertTrue(all("OR-4501-" in item["tree_code"] for item in live["items"]))

    def test_frozen_brief_codes_remain_explainable_after_legacy_rename(self) -> None:
        plot = self._plot("OR-4601")
        tree = self._tree(plot["id"], "OR-4601-T01")
        self._completed_observation(tree)
        with _context(path=f"/api/plots/{plot['id']}/confirm"):
            self.catalog.confirm_plot(plot["id"], expected_revision=plot["revision"])
        with _context(path=f"/api/plots/{plot['id']}/briefs"):
            brief = self.briefs.create_brief(plot["id"], {"title": "旧编号简报"})
        self.assertEqual(brief["payload"]["plot"]["code"], "OR-4601")

        # 模拟历史导入的数据曾把编号改为 OR-4701，并留有别名轨迹。
        def rename(state: dict) -> None:
            record_plot = state["plots"][plot["id"]]
            state["plots"][plot["id"]] = {
                **record_plot,
                "code": "OR-4701",
                "code_aliases": [
                    {
                        "code": "OR-4601",
                        "changed_to": "OR-4701",
                        "reason": "历史迁移",
                        "actor_id": "migration",
                        "changed_at": "2026-01-01T00:00:00+00:00",
                        "cause": "plot_code_correction",
                    }
                ],
            }
            record_tree = state["trees"][tree["id"]]
            state["trees"][tree["id"]] = {
                **record_tree,
                "code": "OR-4701-T01",
                "code_aliases": [
                    {
                        "code": "OR-4601-T01",
                        "changed_to": "OR-4701-T01",
                        "reason": "历史迁移",
                        "actor_id": "migration",
                        "changed_at": "2026-01-01T00:00:00+00:00",
                        "cause": "plot_code_correction",
                    }
                ],
            }

        self.repository.atomic_update(rename)

        stored = self.repository.read()["briefs"][brief["id"]]
        self.assertEqual(stored["plot"]["code"], "OR-4601")
        report = self.catalog.identity_report()
        brief_history = next(
            item
            for item in report["historical_objects"]
            if item["kind"] == "brief" and item["id"] == brief["id"]
        )
        self.assertTrue(brief_history["explainable"])

    def test_identity_report_flags_unexplainable_history_and_duplicates(self) -> None:
        plot = self._plot("OR-4801")
        tree = self._tree(plot["id"], "OR-4801-T01")
        left = self._completed_observation(tree)
        other_plot = self._plot("OR-4802")
        other_tree = self._tree(other_plot["id"], "OR-4802-T01", year=2010)
        right = self._completed_observation(other_tree)
        with _context(path="/api/comparisons"):
            comparison = self.comparisons.create_comparison(
                {
                    "title": "跨园对齐",
                    "left_observation_id": left["id"],
                    "right_observation_id": right["id"],
                }
            )

        # 只改植株编号、园区保持原样：植株前缀与园区不一致且没有别名轨迹，
        # 历史比较应被标为不可解释。
        def opaque_rename(state: dict) -> None:
            record_tree = state["trees"][tree["id"]]
            state["trees"][tree["id"]] = {**record_tree, "code": "OR-4999-T01"}

        self.repository.atomic_update(opaque_rename)

        report = self.catalog.identity_report()
        self.assertFalse(report["unique"])
        self.assertEqual(report["unresolved_trees"][0]["tree_id"], tree["id"])
        historical = next(
            item
            for item in report["historical_objects"]
            if item["kind"] == "comparison" and item["id"] == comparison["id"]
        )
        self.assertFalse(historical["explainable"])


if __name__ == "__main__":
    unittest.main()
