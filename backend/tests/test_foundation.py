from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.application import (
    BriefService,
    CatalogService,
    ComparisonService,
    CorrectionService,
    ObservationService,
)
from app.errors import ConflictError, DomainError, PreconditionError, ValidationError
from app.jobs import JobService
from app.persistence import Database, Repository
from app.security import (
    AuthorizationService,
    IdentityService,
    RequestContext,
    request_scope,
)


class RepositoryFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name)
        self.database = Database(self.data_dir / "atlas.sqlite3")
        self.repository = Repository(self.database)
        self.repository.open()
        self.catalog = CatalogService(self.repository)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def test_idempotency_key_reuses_response_and_revision(self) -> None:
        payload = self._plot_payload("OR-6101")
        request_hash = f"{payload['code']}:{payload['name']}"
        with _request("local-admin", "same-key", request_hash=request_hash):
            first = self.catalog.create_plot(payload)
            revision_after_first = self.repository.read()["revision"]
            second = self.catalog.create_plot(payload)
            revision_after_second = self.repository.read()["revision"]

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(revision_after_first, revision_after_second)
        self.assertEqual(len(self.repository.read()["plots"]), 1)

    def test_idempotency_key_rejects_different_payload(self) -> None:
        first = self._plot_payload("OR-6102")
        second = self._plot_payload("OR-6103")
        with _request(
            "local-admin",
            "shared-key",
            request_hash=f"{first['code']}:{first['name']}",
        ):
            self.catalog.create_plot(first)
            with self.assertRaises(ConflictError) as raised:
                with _request(
                    "local-admin",
                    "shared-key",
                    request_hash=f"{second['code']}:{second['name']}",
                ):
                    self.catalog.create_plot(second)
        self.assertEqual(raised.exception.code, "idempotency_key_reused")

    def test_object_versions_and_audit_are_committed_together(self) -> None:
        with _request("local-admin", "create-plot"):
            plot = self.catalog.create_plot(self._plot_payload("OR-6104"))
        with _request("local-admin", "update-plot"):
            self.catalog.update_plot(
                plot["id"],
                {
                    "name": "更新后的园区",
                    "revision": plot["revision"],
                },
            )

        versions = self.repository.list_entity_versions(
            kind="plot",
            identifier=plot["id"],
        )
        audit = self.repository.list_audit_events(
            resource_kind="plot",
            resource_id=plot["id"],
        )
        self.assertEqual([item["revision"] for item in versions["items"]], [2, 1])
        self.assertEqual(audit["total"], 2)
        self.assertEqual(audit["items"][0]["actor_id"], "local-admin")

    def test_authorization_supports_wildcards_and_scopes(self) -> None:
        authorization = AuthorizationService(self.database)
        authorization.require(
            actor_id="local-admin",
            capability="anything:write",
            resource_kind="anything",
            resource_id="one",
        )
        authorization.require(
            actor_id="local-observer",
            capability="plot:read",
            resource_kind="plot",
            resource_id="any-plot",
        )
        with self.assertRaises(DomainError) as raised:
            authorization.require(
                actor_id="local-observer",
                capability="plot:write",
                resource_kind="plot",
                resource_id="any-plot",
            )
        self.assertEqual(raised.exception.code, "forbidden")

    def test_identity_and_scope_revocation_are_enforced(self) -> None:
        identity = IdentityService(self.database)
        identity.create_actor(actor_id="observer-a", display_name="观察员甲")
        grant = identity.grant(
            actor_id="observer-a",
            capability="plot:read",
            resource_kind="plot",
            resource_id="plot-one",
        )
        authorization = AuthorizationService(self.database)
        authorization.require(
            actor_id="observer-a",
            capability="plot:read",
            resource_kind="plot",
            resource_id="plot-one",
        )
        with self.assertRaises(DomainError) as raised:
            authorization.require(
                actor_id="observer-a",
                capability="plot:read",
                resource_kind="plot",
                resource_id="plot-two",
            )
        self.assertEqual(raised.exception.code, "forbidden")

        identity.revoke(grant["id"])
        with self.assertRaises(DomainError) as raised:
            authorization.require(
                actor_id="observer-a",
                capability="plot:read",
                resource_kind="plot",
                resource_id="plot-one",
            )
        self.assertEqual(raised.exception.code, "forbidden")
        audit = self.repository.list_audit_events(resource_kind="grant")
        self.assertEqual(audit["total"], 2)
        self.assertGreaterEqual(self.repository.read()["revision"], 3)

    def test_concurrent_writers_do_not_lose_updates(self) -> None:
        def create(index: int) -> str:
            with _request("local-admin", f"concurrent-{index}"):
                return self.catalog.create_plot(
                    self._plot_payload(f"OR-{7100 + index}")
                )["id"]

        with ThreadPoolExecutor(max_workers=6) as executor:
            identifiers = list(executor.map(create, range(12)))

        state = self.repository.read()
        self.assertEqual(len(set(identifiers)), 12)
        self.assertEqual(len(state["plots"]), 12)
        self.assertGreaterEqual(state["revision"], 12)

    def _plot_payload(self, code: str) -> dict[str, object]:
        return {
            "code": code,
            "name": f"测试园区 {code}",
            "locality": "测试地点",
            "cultivar_focus": "测试品种",
            "steward": "测试组",
            "planting_year": 2010,
            "note": "",
        }


class ControlledCorrectionTests(unittest.TestCase):
    """受控勘误：原始事实、冻结分析与当前结论三层版本关系。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name)
        self.database = Database(self.data_dir / "atlas.sqlite3")
        self.repository = Repository(self.database)
        self.repository.open()
        self.catalog = CatalogService(self.repository)
        self.observations = ObservationService(self.repository)
        self.comparisons = ComparisonService(self.repository)
        self.briefs = BriefService(self.repository)
        self.corrections = CorrectionService(self.repository)
        self._seed_plot()
        self.first = self._complete_season(
            self.tree_a["id"],
            ["2026-03-10", "2026-04-01", "2026-04-18", "2026-09-02"],
        )
        self.second = self._complete_season(
            self.tree_b["id"],
            ["2026-03-15", "2026-04-05", "2026-04-22", "2026-09-07"],
        )

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _seed_plot(self) -> None:
        with _request("local-admin", "corr-plot-create"):
            self.plot = self.catalog.create_plot(
                {
                    "code": "OR-6201",
                    "name": "勘误测试园",
                    "locality": "测试地点",
                    "cultivar_focus": "测试品种",
                    "steward": "测试组",
                    "planting_year": 2010,
                    "note": "",
                }
            )
        with _request("local-admin", "corr-tree-a"):
            self.tree_a = self.catalog.create_tree(
                {
                    "plot_id": self.plot["id"],
                    "code": "OR-6201-T01",
                    "cultivar": "秋梨",
                    "rootstock": "杜梨",
                    "planting_year": 2010,
                    "status": "active",
                    "note": "",
                }
            )
        with _request("local-admin", "corr-tree-b"):
            self.tree_b = self.catalog.create_tree(
                {
                    "plot_id": self.plot["id"],
                    "code": "OR-6201-T02",
                    "cultivar": "蜜梨",
                    "rootstock": "杜梨",
                    "planting_year": 2010,
                    "status": "active",
                    "note": "",
                }
            )
        with _request("local-admin", "corr-plot-confirm"):
            self.catalog.confirm_plot(
                self.plot["id"],
                expected_revision=self.plot["revision"],
            )

    def _complete_season(
        self,
        tree_id: str,
        dates: list[str],
    ) -> dict[str, object]:
        stages = ["bud_burst", "full_bloom", "fruit_set", "harvest"]
        with _request("local-admin", f"start-{tree_id[-3:]}"):
            record = self.observations.start_observation(
                {
                    "tree_id": tree_id,
                    "season": "2026",
                    "observer": "勘误测试员",
                    "note": "",
                }
            )
        for index, (stage, observed_on) in enumerate(zip(stages, dates)):
            with _request("local-admin", f"stage-{tree_id[-3:]}-{index}"):
                record = self.observations.add_stage(
                    record["id"],
                    {
                        "stage": stage,
                        "observed_on": observed_on,
                        "confidence": 4,
                        "note": "",
                        "revision": record["revision"],
                    },
                )
        with _request("local-admin", f"complete-{tree_id[-3:]}"):
            return self.observations.complete_observation(
                record["id"],
                {"revision": record["revision"]},
            )

    def _frozen_comparison(self) -> dict[str, object]:
        with _request("local-admin", "corr-cmp-1"):
            return self.comparisons.create_comparison(
                {
                    "title": "勘误前图谱",
                    "left_observation_id": self.first["id"],
                    "right_observation_id": self.second["id"],
                }
            )

    def test_proposal_keeps_current_fact_and_correction_only_changes_fact_after_adoption(
        self,
    ) -> None:
        with _request("local-admin", "corr-propose"):
            proposed = self.corrections.create_correction(
                {
                    "observation_id": self.first["id"],
                    "reason": "原始台账显示采收日期应为九月五日",
                    "changes": [{"stage": "harvest", "observed_on": "2026-09-05"}],
                }
            )
        self.assertEqual(proposed["status"], "proposed")
        pending = self.observations.get_observation(self.first["id"])
        self.assertEqual(pending["entry_map"]["harvest"]["observed_on"], "2026-09-02")
        self.assertEqual(pending["proposed_correction_count"], 1)
        self.assertIsNone(pending["current_correction_id"])

        with _request("local-admin", "corr-adopt"):
            adopted = self.corrections.adopt_correction(
                proposed["id"],
                {"revision": proposed["revision"]},
            )
        self.assertEqual(adopted["status"], "adopted")
        detail = self.observations.get_observation(self.first["id"])
        self.assertEqual(detail["entry_map"]["harvest"]["observed_on"], "2026-09-05")
        self.assertEqual(detail["current_correction_id"], proposed["id"])
        self.assertEqual(detail["current_correction_seq"], 1)
        frozen_harvest = next(
            item for item in detail["frozen_entries"] if item["stage"] == "harvest"
        )
        self.assertEqual(frozen_harvest["observed_on"], "2026-09-02")
        lineage = next(
            item for item in detail["entry_lineage"] if item["stage"] == "harvest"
        )
        self.assertTrue(lineage["revised"])
        self.assertEqual(lineage["frozen"]["observed_on"], "2026-09-02")
        self.assertEqual(lineage["current"]["observed_on"], "2026-09-05")

        # 季节志本体字节与修订号永不被勘误改写。
        raw = self.repository.read()["observations"][self.first["id"]]
        raw_harvest = next(
            item for item in raw["entries"] if item["stage"] == "harvest"
        )
        self.assertEqual(raw_harvest["observed_on"], "2026-09-02")
        self.assertEqual(raw["revision"], self.first["revision"])

    def test_correction_is_validated_against_completed_record(self) -> None:
        with _request("local-admin", "corr-open-stage"):
            draft = self.observations.start_observation(
                {
                    "tree_id": self.tree_a["id"],
                    "season": "2025",
                    "observer": "草稿员",
                    "note": "",
                }
            )
        with self.assertRaises(PreconditionError) as raised:
            with _request("local-admin", "corr-on-open"):
                self.corrections.create_correction(
                    {
                        "observation_id": draft["id"],
                        "reason": "草稿不能提出勘误",
                        "changes": [{"stage": "harvest", "observed_on": "2025-09-05"}],
                    }
                )
        self.assertEqual(raised.exception.code, "season_not_completed")

        with self.assertRaises(PreconditionError) as stage_raised:
            with _request("local-admin", "corr-bad-stage"):
                self.corrections.create_correction(
                    {
                        "observation_id": self.first["id"],
                        "reason": "不能修正没有冻结过的落叶期",
                        "changes": [{"stage": "leaf_fall", "observed_on": "2026-11-20"}],
                    }
                )
        self.assertEqual(
            stage_raised.exception.code,
            "correction_stage_not_recorded",
        )
        with self.assertRaises(ValidationError):
            with _request("local-admin", "corr-sequence"):
                self.corrections.create_correction(
                    {
                        "observation_id": self.first["id"],
                        "reason": "萌芽期不能晚于盛花期",
                        "changes": [{"stage": "bud_burst", "observed_on": "2026-05-01"}],
                    }
                )
        with self.assertRaises(ValidationError):
            with _request("local-admin", "corr-same"):
                self.corrections.create_correction(
                    {
                        "observation_id": self.first["id"],
                        "reason": "与冻结事实相同不算勘误",
                        "changes": [{"stage": "harvest", "observed_on": "2026-09-02"}],
                    }
                )

    def test_rejected_and_withdrawn_corrections_never_change_facts(self) -> None:
        with _request("local-admin", "corr-reject-create"):
            candidate = self.corrections.create_correction(
                {
                    "observation_id": self.first["id"],
                    "reason": "待拒绝的错误勘误建议",
                    "changes": [{"stage": "harvest", "observed_on": "2026-08-30"}],
                }
            )
        with _request("local-admin", "corr-reject"):
            rejected = self.corrections.reject_correction(
                candidate["id"],
                {"revision": candidate["revision"], "note": "台账不支持该日期"},
            )
        self.assertEqual(rejected["status"], "rejected")

        with _request("local-admin", "corr-withdraw-create"):
            other = self.corrections.create_correction(
                {
                    "observation_id": self.first["id"],
                    "reason": "待撤回的勘误建议",
                    "changes": [{"stage": "harvest", "observed_on": "2026-08-31"}],
                }
            )
        with _request("local-admin", "corr-withdraw"):
            withdrawn = self.corrections.withdraw_correction(
                other["id"],
                {"revision": other["revision"]},
            )
        self.assertEqual(withdrawn["status"], "withdrawn")
        detail = self.observations.get_observation(self.first["id"])
        self.assertEqual(detail["entry_map"]["harvest"]["observed_on"], "2026-09-02")
        self.assertFalse(detail["has_corrections"])
        self.assertIsNone(detail["current_correction_id"])

        with self.assertRaises(PreconditionError):
            with _request("local-admin", "corr-double-decide"):
                self.corrections.adopt_correction(
                    rejected["id"],
                    {"revision": rejected["revision"]},
                )

    def test_frozen_comparison_is_not_rewritten_and_requires_explicit_new_version(
        self,
    ) -> None:
        original = self._frozen_comparison()
        original_offsets = [
            dict(item) for item in original["stage_offsets"]
        ]
        with _request("local-admin", "corr-for-cmp-create"):
            proposal = self.corrections.create_correction(
                {
                    "observation_id": self.first["id"],
                    "reason": "复核后采收期顺延三天",
                    "changes": [{"stage": "harvest", "observed_on": "2026-09-05"}],
                }
            )
        with _request("local-admin", "corr-for-cmp-adopt"):
            self.corrections.adopt_correction(
                proposal["id"],
                {"revision": proposal["revision"]},
            )

        frozen = self.comparisons.get_comparison(original["id"])
        self.assertEqual(frozen["basis_status"], "superseded")
        self.assertEqual(
            [item["offset_days"] for item in frozen["stage_offsets"]],
            [item["offset_days"] for item in original_offsets],
        )

        with self.assertRaises(ConflictError) as raised:
            with _request("local-admin", "corr-cmp-naive"):
                self.comparisons.create_comparison(
                    {
                        "title": "悄悄重算",
                        "left_observation_id": self.first["id"],
                        "right_observation_id": self.second["id"],
                    }
                )
        self.assertEqual(
            raised.exception.code,
            "comparison_basis_superseded",
        )
        self.assertEqual(
            raised.exception.details["existing_comparison_id"],
            original["id"],
        )

        with _request("local-admin", "corr-cmp-new"):
            renewed = self.comparisons.create_comparison(
                {
                    "title": "勘误后图谱",
                    "left_observation_id": self.first["id"],
                    "right_observation_id": self.second["id"],
                    "supersedes_comparison_id": original["id"],
                }
            )
        self.assertEqual(renewed["basis_status"], "current")
        self.assertEqual(renewed["supersedes_comparison_id"], original["id"])
        renewed_harvest = next(
            item for item in renewed["stage_offsets"] if item["stage"] == "harvest"
        )
        self.assertEqual(renewed_harvest["offset_days"], 2)
        self.assertEqual(
            renewed["left_basis"]["current_correction_id"],
            proposal["id"],
        )

        old = self.comparisons.get_comparison(original["id"])
        new = self.comparisons.get_comparison(renewed["id"])
        self.assertEqual(old["basis_status"], "superseded")
        self.assertEqual(old["superseded_by_id"], renewed["id"])
        self.assertEqual(new["basis_status"], "current")

        # 同世代重复请求复用同一版本，不会产生两套“当前”图谱。
        with _request("local-admin", "corr-cmp-duplicate"):
            duplicate = self.comparisons.create_comparison(
                {
                    "title": "勘误后图谱",
                    "left_observation_id": self.first["id"],
                    "right_observation_id": self.second["id"],
                }
            )
        self.assertEqual(duplicate["id"], renewed["id"])
        listing = self.comparisons.list_comparisons()
        current = [
            item for item in listing["items"] if item["basis_status"] == "current"
        ]
        self.assertEqual(len(current), 1)

    def test_second_correction_forms_chain_and_stale_proposal_is_revalidated(
        self,
    ) -> None:
        # X 在冻结事实上成立：采收期可前移到 06-01。
        with _request("local-admin", "corr-chain-x-create"):
            stale = self.corrections.create_correction(
                {
                    "observation_id": self.first["id"],
                    "reason": "采收日期拟前移到六月",
                    "changes": [{"stage": "harvest", "observed_on": "2026-06-01"}],
                }
            )

        # Y 先把坐果期核定为 08-20，采纳后 X 与新事实冲突。
        with _request("local-admin", "corr-chain-y-create"):
            intervening = self.corrections.create_correction(
                {
                    "observation_id": self.first["id"],
                    "reason": "坐果期实际为八月下旬",
                    "changes": [{"stage": "fruit_set", "observed_on": "2026-08-20"}],
                }
            )
        with _request("local-admin", "corr-chain-y-adopt"):
            self.corrections.adopt_correction(
                intervening["id"],
                {"revision": intervening["revision"]},
            )

        # 采纳点必须基于最新事实重新校验，系统拒绝且不套用 X。
        with self.assertRaises(ValidationError):
            with _request("local-admin", "corr-chain-x-adopt"):
                self.corrections.adopt_correction(
                    stale["id"],
                    {"revision": stale["revision"]},
                )
        pending = self.corrections.get_correction(stale["id"])
        self.assertEqual(pending["status"], "proposed")
        self.assertEqual(
            self.observations.get_observation(self.first["id"])["entry_map"]["harvest"][
                "observed_on"
            ],
            "2026-09-02",
        )

        with _request("local-admin", "corr-chain-z-create"):
            valid = self.corrections.create_correction(
                {
                    "observation_id": self.first["id"],
                    "reason": "采收日期二次核定",
                    "changes": [{"stage": "harvest", "observed_on": "2026-09-08"}],
                }
            )
        with _request("local-admin", "corr-chain-z-adopt"):
            self.corrections.adopt_correction(
                valid["id"],
                {"revision": valid["revision"]},
            )
        detail = self.observations.get_observation(self.first["id"])
        self.assertEqual(detail["current_correction_id"], valid["id"])
        self.assertEqual(detail["current_correction_seq"], 2)
        self.assertEqual(detail["entry_map"]["harvest"]["observed_on"], "2026-09-08")
        self.assertEqual(
            detail["entry_map"]["fruit_set"]["observed_on"],
            "2026-08-20",
        )
        ledger = self.corrections.list_corrections(
            observation_id=self.first["id"],
            status="adopted",
        )
        self.assertEqual(ledger["total"], 2)

    def test_brief_freezes_basis_and_is_flagged_after_later_correction(self) -> None:
        with _request("local-admin", "corr-brief-1"):
            first_brief = self.briefs.create_brief(
                self.plot["id"],
                {"title": "勘误前简报"},
            )
        self.assertEqual(first_brief["basis_status"], "current")
        with _request("local-admin", "corr-brief-adopt-create"):
            proposal = self.corrections.create_correction(
                {
                    "observation_id": self.first["id"],
                    "reason": "简报后核定采收日期",
                    "changes": [{"stage": "harvest", "observed_on": "2026-09-05"}],
                }
            )
        with _request("local-admin", "corr-brief-adopt"):
            self.corrections.adopt_correction(
                proposal["id"],
                {"revision": proposal["revision"]},
            )

        frozen_brief = self.briefs.get_brief(first_brief["id"])
        self.assertEqual(frozen_brief["basis_status"], "superseded")
        frozen_season = next(
            item
            for item in frozen_brief["payload"]["observations"]
            if item["id"] == self.first["id"]
        )
        self.assertEqual(
            frozen_season["entry_map"]["harvest"]["observed_on"],
            "2026-09-02",
        )
        self.assertEqual(
            frozen_brief["superseded_observations"][0]["current_correction_id"],
            proposal["id"],
        )

        with _request("local-admin", "corr-brief-2"):
            current_brief = self.briefs.create_brief(
                self.plot["id"],
                {"title": "勘误后简报"},
            )
        self.assertEqual(current_brief["basis_status"], "current")
        current_season = next(
            item
            for item in current_brief["payload"]["observations"]
            if item["id"] == self.first["id"]
        )
        self.assertEqual(
            current_season["entry_map"]["harvest"]["observed_on"],
            "2026-09-05",
        )

    def test_correction_versions_and_audit_share_transaction(self) -> None:
        with _request(
            "local-admin",
            "corr-version-create",
            request_path="/api/corrections",
            route_template="/api/corrections",
        ):
            created = self.corrections.create_correction(
                {
                    "observation_id": self.first["id"],
                    "reason": "审计所需的勘误记录",
                    "changes": [{"stage": "harvest", "observed_on": "2026-09-05"}],
                }
            )
        with _request(
            "local-admin",
            "corr-version-adopt",
            request_path=f"/api/corrections/{created['id']}/adopt",
            route_template="/api/corrections/{correction_id}/adopt",
        ):
            self.corrections.adopt_correction(
                created["id"],
                {"revision": created["revision"]},
            )
        versions = self.repository.list_entity_versions(
            kind="correction",
            identifier=created["id"],
        )
        self.assertEqual([item["revision"] for item in versions["items"]], [2, 1])
        actions = {
            item["action"]
            for item in self.repository.list_audit_events(
                resource_kind="correction",
                resource_id=created["id"],
            )["items"]
        }
        self.assertIn("adopt", actions)

    def _plot_payload(self, code: str) -> dict[str, object]:
        return {
            "code": code,
            "name": f"测试园区 {code}",
            "locality": "测试地点",
            "cultivar_focus": "测试品种",
            "steward": "测试组",
            "planting_year": 2010,
            "note": "",
        }


class StageReplacementTests(unittest.TestCase):
    """受控勘误：用正确阶段整体替换误录阶段。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name)
        self.database = Database(self.data_dir / "atlas.sqlite3")
        self.repository = Repository(self.database)
        self.repository.open()
        self.catalog = CatalogService(self.repository)
        self.observations = ObservationService(self.repository)
        self.comparisons = ComparisonService(self.repository)
        self.briefs = BriefService(self.repository)
        self.corrections = CorrectionService(self.repository)
        with _request("local-admin", "rep-plot-create"):
            self.plot = self.catalog.create_plot(
                {
                    "code": "OR-6301",
                    "name": "阶段替换测试园",
                    "locality": "测试地点",
                    "cultivar_focus": "测试品种",
                    "steward": "测试组",
                    "planting_year": 2010,
                    "note": "",
                }
            )
        with _request("local-admin", "rep-tree-left"):
            self.tree_left = self.catalog.create_tree(
                {
                    "plot_id": self.plot["id"],
                    "code": "OR-6301-T01",
                    "cultivar": "秋梨",
                    "rootstock": "杜梨",
                    "planting_year": 2010,
                    "status": "active",
                    "note": "",
                }
            )
        with _request("local-admin", "rep-tree-right"):
            self.tree_right = self.catalog.create_tree(
                {
                    "plot_id": self.plot["id"],
                    "code": "OR-6301-T02",
                    "cultivar": "蜜梨",
                    "rootstock": "杜梨",
                    "planting_year": 2010,
                    "status": "active",
                    "note": "",
                }
            )
        with _request("local-admin", "rep-plot-confirm"):
            self.catalog.confirm_plot(
                self.plot["id"],
                expected_revision=self.plot["revision"],
            )

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _season(
        self,
        seed: str,
        tree_id: str,
        entries: list[tuple[str, str]],
    ) -> dict[str, object]:
        entries = sorted(entries, key=lambda item: item[1])
        with _request("local-admin", f"rep-start-{seed}"):
            record = self.observations.start_observation(
                {
                    "tree_id": tree_id,
                    "season": "2026",
                    "observer": "替换测试员",
                    "note": "",
                }
            )
        for index, (stage, observed_on) in enumerate(entries):
            with _request("local-admin", f"rep-stage-{seed}-{index}"):
                record = self.observations.add_stage(
                    record["id"],
                    {
                        "stage": stage,
                        "observed_on": observed_on,
                        "confidence": 4,
                        "note": "现场误录阶段"
                        if seed == "left" and stage == "fruit_growth"
                        else "",
                        "revision": record["revision"],
                    },
                )
        with _request("local-admin", f"rep-complete-{seed}"):
            return self.observations.complete_observation(
                record["id"],
                {"revision": record["revision"]},
            )

    def _seed_pair(self) -> tuple[dict[str, object], dict[str, object]]:
        # 左：05-20 被误记为果实膨大期，实际是 04-12 的落瓣期
        left = self._season(
            "left",
            self.tree_left["id"],
            [
                ("bud_burst", "2026-03-10"),
                ("full_bloom", "2026-04-01"),
                ("fruit_set", "2026-04-18"),
                ("fruit_growth", "2026-05-20"),
                ("harvest", "2026-09-02"),
            ],
        )
        right = self._season(
            "right",
            self.tree_right["id"],
            [
                ("bud_burst", "2026-03-15"),
                ("full_bloom", "2026-04-05"),
                ("petal_fall", "2026-04-15"),
                ("fruit_set", "2026-04-22"),
                ("harvest", "2026-09-07"),
            ],
        )
        return left, right

    def test_stage_replacement_moves_stage_once_and_keeps_original(self) -> None:
        left, _ = self._seed_pair()
        with _request("local-admin", "rep-propose"):
            proposed = self.corrections.create_correction(
                {
                    "observation_id": left["id"],
                    "reason": "该观察实为04-12落瓣期，被误记为05-20果实膨大期",
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
        pending = self.observations.get_observation(left["id"])
        self.assertIn("fruit_growth", pending["entry_map"])
        self.assertNotIn("petal_fall", pending["entry_map"])

        with _request(
            "local-admin",
            "rep-adopt",
            request_path=f"/api/corrections/{proposed['id']}/adopt",
            route_template="/api/corrections/{correction_id}/adopt",
        ):
            self.corrections.adopt_correction(
                proposed["id"],
                {"revision": proposed["revision"]},
            )

        detail = self.observations.get_observation(left["id"])
        current_stages = [entry["stage"] for entry in detail["entries"]]
        self.assertNotIn("fruit_growth", current_stages)
        self.assertIn("petal_fall", current_stages)
        self.assertEqual(
            sum(1 for stage in current_stages if stage == "petal_fall"),
            1,
        )
        self.assertEqual(detail["entry_map"]["petal_fall"]["observed_on"], "2026-04-12")

        # 原始冻结事实仍完整保留误录阶段与日期。
        frozen_stages = [entry["stage"] for entry in detail["frozen_entries"]]
        self.assertIn("fruit_growth", frozen_stages)
        self.assertNotIn("petal_fall", frozen_stages)

        lineage = {line["stage"]: line for line in detail["entry_lineage"]}
        self.assertEqual(lineage["fruit_growth"]["status"], "replaced_out")
        self.assertEqual(lineage["fruit_growth"]["replacement_stage"], "petal_fall")
        self.assertIsNone(lineage["fruit_growth"]["current"])
        self.assertEqual(lineage["petal_fall"]["status"], "replaced_in")
        self.assertEqual(lineage["petal_fall"]["replacement_stage"], "fruit_growth")
        self.assertEqual(
            lineage["petal_fall"]["current"]["observed_on"],
            "2026-04-12",
        )

        # 季节志本体字节与修订号不变。
        raw = self.repository.read()["observations"][left["id"]]
        self.assertEqual(raw["revision"], left["revision"])
        self.assertIn("fruit_growth", {entry["stage"] for entry in raw["entries"]})

    def test_invalid_replacements_are_rejected(self) -> None:
        left, _ = self._seed_pair()
        cases = [
            (
                "正确阶段已存在",
                {
                    "change_type": "replace",
                    "stage": "bud_burst",
                    "correct_stage": "full_bloom",
                    "observed_on": "2026-03-20",
                },
                "correction_target_stage_exists",
            ),
            (
                "替换会移除必需阶段",
                {
                    "change_type": "replace",
                    "stage": "harvest",
                    "correct_stage": "leaf_fall",
                    "observed_on": "2026-11-20",
                },
                "correction_required_stage_removed",
            ),
            (
                "正确阶段与误录阶段相同",
                {
                    "change_type": "replace",
                    "stage": "bud_burst",
                    "correct_stage": "bud_burst",
                    "observed_on": "2026-03-11",
                },
                "validation_error",
            ),
            (
                "替换后违反阶段顺序",
                {
                    "change_type": "replace",
                    "stage": "fruit_growth",
                    "correct_stage": "petal_fall",
                    "observed_on": "2026-05-20",
                },
                "validation_error",
            ),
        ]
        for index, (_, change, code) in enumerate(cases):
            with self.assertRaises(DomainError) as raised:
                with _request("local-admin", f"rep-invalid-{index}"):
                    self.corrections.create_correction(
                        {
                            "observation_id": left["id"],
                            "reason": "非法替换用例",
                            "changes": [change],
                        }
                    )
            self.assertEqual(raised.exception.code, code)

    def test_frozen_comparison_and_brief_keep_old_generation_after_replacement(
        self,
    ) -> None:
        left, right = self._seed_pair()
        with _request("local-admin", "rep-cmp-old"):
            old_comparison = self.comparisons.create_comparison(
                {
                    "title": "替换前图谱",
                    "left_observation_id": left["id"],
                    "right_observation_id": right["id"],
                }
            )
        with _request("local-admin", "rep-brief-old"):
            old_brief = self.briefs.create_brief(
                self.plot["id"],
                {"title": "替换前简报"},
            )
        old_common = {
            item["stage"]: item["offset_days"]
            for item in old_comparison["stage_offsets"]
        }
        self.assertNotIn("petal_fall", old_common)
        self.assertEqual(set(old_common), {
            "bud_burst",
            "full_bloom",
            "fruit_set",
            "harvest",
        })

        with _request("local-admin", "rep-create-2"):
            proposed = self.corrections.create_correction(
                {
                    "observation_id": left["id"],
                    "reason": "该观察实为04-12落瓣期，被误记为果实膨大期",
                    "changes": [
                        {
                            "change_type": "replace",
                            "stage": "fruit_growth",
                            "correct_stage": "petal_fall",
                            "observed_on": "2026-04-12",
                            "confidence": 4,
                        }
                    ],
                }
            )
        with _request(
            "local-admin",
            "rep-adopt-2",
            request_path=f"/api/corrections/{proposed['id']}/adopt",
            route_template="/api/corrections/{correction_id}/adopt",
        ):
            self.corrections.adopt_correction(
                proposed["id"],
                {"revision": proposed["revision"]},
            )

        frozen_comparison = self.comparisons.get_comparison(old_comparison["id"])
        self.assertEqual(frozen_comparison["basis_status"], "superseded")
        self.assertEqual(
            {
                item["stage"]: item["offset_days"]
                for item in frozen_comparison["stage_offsets"]
            },
            old_common,
        )

        with self.assertRaises(ConflictError) as raised:
            with _request("local-admin", "rep-cmp-naive"):
                self.comparisons.create_comparison(
                    {
                        "title": "隐式重算",
                        "left_observation_id": left["id"],
                        "right_observation_id": right["id"],
                    }
                )
        self.assertEqual(
            raised.exception.code,
            "comparison_basis_superseded",
        )

        with _request("local-admin", "rep-cmp-new"):
            new_comparison = self.comparisons.create_comparison(
                {
                    "title": "替换后图谱",
                    "left_observation_id": left["id"],
                    "right_observation_id": right["id"],
                    "supersedes_comparison_id": old_comparison["id"],
                }
            )
        new_common = {
            item["stage"]: item["offset_days"]
            for item in new_comparison["stage_offsets"]
        }
        self.assertEqual(new_comparison["basis_status"], "current")
        self.assertNotIn("fruit_growth", new_common)
        self.assertIn("petal_fall", new_common)
        self.assertEqual(new_common["petal_fall"], 3)
        self.assertEqual(
            self.comparisons.get_comparison(old_comparison["id"])[
                "superseded_by_id"
            ],
            new_comparison["id"],
        )

        frozen_brief = self.briefs.get_brief(old_brief["id"])
        self.assertEqual(frozen_brief["basis_status"], "superseded")
        frozen_left = next(
            item
            for item in frozen_brief["payload"]["observations"]
            if item["id"] == left["id"]
        )
        self.assertIn("fruit_growth", frozen_left["entry_map"])
        self.assertNotIn("petal_fall", frozen_left["entry_map"])

        with _request("local-admin", "rep-brief-new"):
            new_brief = self.briefs.create_brief(
                self.plot["id"],
                {"title": "替换后简报"},
            )
        self.assertEqual(new_brief["basis_status"], "current")
        new_left = next(
            item
            for item in new_brief["payload"]["observations"]
            if item["id"] == left["id"]
        )
        self.assertIn("petal_fall", new_left["entry_map"])
        self.assertNotIn("fruit_growth", new_left["entry_map"])

        # 同一对季节志只能有一份当前图谱。
        current = [
            item
            for item in self.comparisons.list_comparisons()["items"]
            if item["basis_status"] == "current"
        ]
        self.assertEqual(len(current), 1)


class JobQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "atlas.sqlite3")
        self.database.initialize()
        self.jobs = JobService(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_job_lifecycle_and_idempotent_enqueue(self) -> None:
        first = self.jobs.enqueue(
            actor_id="local-admin",
            job_type="integrity_scan",
            payload={},
            idempotency_key="job-key",
        )
        second = self.jobs.enqueue(
            actor_id="local-admin",
            job_type="integrity_scan",
            payload={},
            idempotency_key="job-key",
        )
        self.assertEqual(first["id"], second["id"])

        claimed = self.jobs.claim(worker_id="worker-a", lease_seconds=30)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed["attempt"], 1)
        completed = self.jobs.complete(
            claimed["id"],
            worker_id="worker-a",
            result={"status": "healthy"},
        )
        self.assertEqual(completed["status"], "succeeded")

    def test_failed_job_is_requeued_until_dead_letter(self) -> None:
        job = self.jobs.enqueue(
            actor_id="local-admin",
            job_type="failing_job",
            payload={},
            max_attempts=1,
        )
        claimed = self.jobs.claim(worker_id="worker-a")
        assert claimed is not None
        failed = self.jobs.fail(
            job["id"],
            worker_id="worker-a",
            error="boom",
        )
        self.assertEqual(failed["status"], "dead_letter")
        self.assertIn("boom", failed["error"])


def _request(
    actor_id: str,
    idempotency_key: str,
    *,
    request_hash: str | None = None,
    request_path: str = "/api/test",
    route_template: str = "/api/test",
):
    return request_scope(
        RequestContext(
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            request_method="PUT",
            request_path=request_path,
            request_hash=request_hash or f"{actor_id}:{idempotency_key}",
            route_template=route_template,
        )
    )


if __name__ == "__main__":
    unittest.main()
