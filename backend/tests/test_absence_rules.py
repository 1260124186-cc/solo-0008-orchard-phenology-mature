"""阶段观察状态与完成依据的领域测试。"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from itertools import count
from pathlib import Path

from app.application import CatalogService, ObservationService
from app.errors import PreconditionError
from app.persistence import Database, Repository
from app.security import RequestContext, request_scope


REQUIRED_STAGES = ["bud_burst", "full_bloom", "fruit_set", "harvest"]
STAGE_DATES = {
    "bud_burst": "2026-03-14",
    "full_bloom": "2026-04-06",
    "fruit_set": "2026-04-24",
    "harvest": "2026-09-08",
}

_sequence = count()


@contextmanager
def _actor(actor_id: str):
    token = next(_sequence)
    context = RequestContext(
        actor_id=actor_id,
        idempotency_key=f"{actor_id}-{token}",
        request_method="PUT",
        request_path="/api/test",
        request_hash=f"{actor_id}:{token}",
        route_template="/api/test",
    )
    with request_scope(context):
        yield


class AbsenceRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        data_dir = Path(self.temporary.name)
        self.repository = Repository(Database(data_dir / "atlas.sqlite3"))
        self.repository.open()
        self.catalog = CatalogService(self.repository)
        self.observations = ObservationService(self.repository)
        with _actor("setup"):
            plot = self.catalog.create_plot(
                {
                    "code": "OR-7101",
                    "name": "状态规则试验园",
                    "locality": "北山沟",
                    "cultivar_focus": "秋梨",
                    "steward": "档案组",
                    "planting_year": 2010,
                    "note": "",
                }
            )
        with _actor("setup"):
            self.tree = self.catalog.create_tree(
                {
                    "plot_id": plot["id"],
                    "code": "OR-7101-T01",
                    "cultivar": "秋梨",
                    "rootstock": "杜梨",
                    "planting_year": 2010,
                    "status": "active",
                    "note": "",
                }
            )
        with _actor("setup"):
            self.season = self.observations.start_observation(
                {
                    "tree_id": self.tree["id"],
                    "season": "2026",
                    "observer": "沈观察",
                    "note": "",
                }
            )

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _season(self) -> dict:
        with _actor("observer"):
            return self.observations.get_observation(self.season["id"])

    def _add(self, stage: str) -> None:
        current = self._season()
        with _actor("observer"):
            self.observations.add_stage(
                current["id"],
                {
                    "stage": stage,
                    "observed_on": STAGE_DATES[stage],
                    "confidence": 4,
                    "note": "",
                    "revision": current["revision"],
                },
            )

    def _absence(self, stage: str, reason: str, basis: str) -> dict:
        current = self._season()
        with _actor("observer"):
            return self.observations.put_absence(
                current["id"],
                {
                    "stage": stage,
                    "reason": reason,
                    "basis": basis,
                    "revision": current["revision"],
                },
            )

    def _complete(self) -> dict:
        current = self._season()
        with _actor("observer"):
            return self.observations.complete_observation(
                current["id"], {"revision": current["revision"]}
            )

    def test_unobserved_and_pending_verification_do_not_bypass_required_stage(self) -> None:
        # 三个必需阶段有观察，采收期标记为“未观察到”。
        for stage in ["bud_burst", "full_bloom", "fruit_set"]:
            self._add(stage)
        self._absence(
            "harvest",
            "unobserved",
            "秋季巡查两次均未看到采收物候表现",
        )
        with self.assertRaises(PreconditionError) as raised:
            self._complete()
        self.assertEqual(raised.exception.code, "required_stages_missing")
        self.assertEqual(raised.exception.details["missing"], ["harvest"])

        # “仍在核实”同样不能完成。
        current = self._season()
        with _actor("observer"):
            current = self.observations.put_absence(
                current["id"],
                {
                    "stage": "harvest",
                    "reason": "pending_verification",
                    "basis": "等果农确认后再补充",
                    "revision": current["revision"],
                },
            )
        with _actor("observer"), self.assertRaises(PreconditionError):
            self.observations.complete_observation(
                current["id"], {"revision": current["revision"]}
            )

    def test_not_applicable_requires_substantive_basis(self) -> None:
        from app.errors import ValidationError

        for stage in ["bud_burst", "full_bloom", "fruit_set"]:
            self._add(stage)
        # 依据过短不被接受，标记不能成为绕过必需阶段的开关。
        with self.assertRaises(ValidationError):
            self._absence("harvest", "not_applicable", "无")
        current = self._season()
        with _actor("observer"), self.assertRaises(ValidationError):
            self.observations.put_absence(
                current["id"],
                {
                    "stage": "harvest",
                    "reason": "not_applicable",
                    "basis": "今年没采",
                    "revision": current["revision"],
                },
            )

    def test_not_applicable_with_basis_completes_and_freezes_basis(self) -> None:
        for stage in ["bud_burst", "full_bloom", "fruit_set"]:
            self._add(stage)
        updated = self._absence(
            "harvest",
            "not_applicable",
            "该树今年遭风折未挂果，经现场两次核查确认无采收期",
        )
        self.assertTrue(updated["completion_readiness"]["ready"])
        completed = self._complete()
        self.assertEqual(completed["status"], "completed")
        basis = completed["completion_basis"]
        self.assertFalse(basis["legacy"])
        self.assertEqual(basis["observed_required_count"], 3)
        self.assertEqual(basis["not_applicable_required_count"], 1)
        harvest_basis = next(
            item for item in basis["stages"] if item["stage"] == "harvest"
        )
        self.assertEqual(harvest_basis["state"], "not_applicable")
        self.assertIn("风折", harvest_basis["basis"])

        # 完成后缺失说明不可再改，比较仍只使用实际日期，不推断不适用阶段。
        with _actor("observer"), self.assertRaises(PreconditionError):
            self.observations.put_absence(
                completed["id"],
                {
                    "stage": "harvest",
                    "reason": "unobserved",
                    "basis": "试图改写冻结结论的文字内容",
                    "revision": completed["revision"],
                },
            )

    def test_recording_observation_clears_marker(self) -> None:
        self._absence(
            "harvest",
            "pending_verification",
            "正在向果农核实采收具体时间",
        )
        self._add("harvest")
        current = self._season()
        self.assertEqual(current["absence_markers"], [])
        for stage in ["bud_burst", "full_bloom", "fruit_set"]:
            self._add(stage)
        completed = self._complete()
        self.assertEqual(completed["completion_basis"]["not_applicable_required_count"], 0)

    def test_unknown_reason_rejected(self) -> None:
        from app.errors import ValidationError

        current = self._season()
        with _actor("observer"), self.assertRaises(ValidationError):
            self.observations.put_absence(
                current["id"],
                {
                    "stage": "harvest",
                    "reason": "skipped",
                    "basis": "some basis text here",
                    "revision": current["revision"],
                },
            )

    def test_legacy_completed_season_keeps_its_own_judgement(self) -> None:
        # 直接构造特性上线前完成的旧季节志：无 absence_markers、无 completion_basis。
        state = self.repository.read()
        observation = state["observations"][self.season["id"]]
        observation["status"] = "completed"
        observation["completed_at"] = observation["updated_at"]
        observation["entries"] = [
            {
                "id": f"entry_{stage}",
                "stage": stage,
                "observed_on": STAGE_DATES[stage],
                "confidence": 4,
                "note": "",
                "created_at": observation["created_at"],
            }
            for stage in REQUIRED_STAGES
        ]

        def action(working: dict) -> dict:
            working["observations"][observation["id"]] = observation
            return observation

        with _actor("migration"):
            self.repository.atomic_update(action)
            view = self.observations.get_observation(observation["id"])
        basis = view["completion_basis"]
        self.assertTrue(basis["legacy"])
        self.assertEqual(basis["rule_version"], 1)
        self.assertEqual(basis["observed_required_count"], 4)
        self.assertIn("沿用当时的完成判断", basis["basis_text"])
        # 旧记录没有缺失说明，开放接口不能事后追加。
        with _actor("observer"), self.assertRaises(PreconditionError):
            self.observations.put_absence(
                observation["id"],
                {
                    "stage": "leaf_fall",
                    "reason": "unobserved",
                    "basis": "试图给旧记录补充说明文字",
                    "revision": view["revision"],
                },
            )


if __name__ == "__main__":
    unittest.main()
