from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.application import CatalogService, ObservationService
from app.errors import ConflictError, PreconditionError, ValidationError
from app.persistence import Database, Repository
from app.security import RequestContext, request_scope


class TreeStatusLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        data_dir = Path(self.temporary.name)
        self.repository = Repository(Database(data_dir / "atlas.sqlite3"))
        self.repository.open()
        self.catalog = CatalogService(self.repository)
        self.observations = ObservationService(self.repository)
        token = f"{id(self)}"
        with _request("admin", f"seed-plot-{token}"):
            self.plot = self.catalog.create_plot(
                {
                    "code": "OR-7201",
                    "name": "状态流转试验园",
                    "locality": "东坡台地",
                    "cultivar_focus": "冬梨",
                    "steward": "档案组",
                    "planting_year": 2008,
                    "note": "",
                }
            )
        with _request("admin", f"seed-tree-{token}"):
            self.tree = self.catalog.create_tree(
                {
                    "plot_id": self.plot["id"],
                    "code": "OR-7201-T01",
                    "cultivar": "冬梨",
                    "rootstock": "杜梨",
                    "planting_year": 2008,
                    "note": "建档时备注",
                }
            )

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _change(self, key: str, **payload):
        with _request("admin", key):
            return self.catalog.change_tree_status(self.tree["id"], payload)

    def test_create_records_initial_status_event(self) -> None:
        self.assertEqual(self.tree["status"], "active")
        self.assertEqual(len(self.tree["status_history"]), 1)
        first = self.tree["status_history"][0]
        self.assertEqual(first["status"], "active")
        self.assertIsNone(first["previous_status"])
        self.assertEqual(first["actor_id"], "admin")

    def test_close_requires_reason_evidence_and_revision(self) -> None:
        with self.assertRaises(ValidationError):
            self._change(
                "missing-reason",
                status="lost",
                evidence="现场记录",
                revision=self.tree["revision"],
            )
        with self.assertRaises(ValidationError):
            self._change(
                "missing-evidence",
                status="lost",
                reason="巡查未见萌发",
                revision=self.tree["revision"],
            )
        with self.assertRaises(ValidationError):
            self._change(
                "missing-revision",
                status="lost",
                reason="巡查未见萌发",
                evidence="现场记录",
            )

    def test_close_restore_preserves_identity_and_keeps_full_trail(self) -> None:
        closed = self._change(
            "close",
            status="lost",
            reason="连续两季巡查未见萌发，疑似枯死",
            evidence="2026-04-02 现场照片与管护员笔录",
            revision=self.tree["revision"],
        )
        self.assertEqual(closed["status"], "lost")
        self.assertEqual(closed["id"], self.tree["id"])
        self.assertEqual(len(closed["status_history"]), 2)

        # 关闭期间不能再建立季节志
        with self.assertRaises(PreconditionError) as blocked:
            with _request("admin", "season-blocked"):
                self.observations.start_observation(
                    {
                        "tree_id": self.tree["id"],
                        "season": "2026",
                        "observer": "周岚",
                        "note": "",
                    }
                )
        self.assertEqual(blocked.exception.code, "tree_not_active")

        with self.assertRaises(PreconditionError) as repeated:
            self._change(
                "repeat-lost",
                status="lost",
                reason="重复标记",
                evidence="重复依据",
                revision=closed["revision"],
            )
        self.assertEqual(repeated.exception.code, "tree_status_unchanged")

        restored = self._change(
            "restore",
            status="active",
            reason="复查发现根蘖重新萌发，原枯死判定为误判",
            evidence="2026-05-10 复查照片、管护员签字确认",
            revision=closed["revision"],
        )
        self.assertEqual(restored["status"], "active")
        self.assertEqual(restored["id"], self.tree["id"])
        self.assertEqual(restored["revision"], closed["revision"] + 1)
        self.assertEqual(len(restored["status_history"]), 3)

        history = self.catalog.get_tree_status_history(self.tree["id"])
        self.assertEqual(history["current_status"], "active")
        self.assertEqual(history["total"], 3)
        self.assertEqual(
            [event["status"] for event in history["items"]],
            ["active", "lost", "active"],
        )
        self.assertEqual(history["items"][1]["previous_status"], "active")
        self.assertEqual(history["items"][2]["previous_status"], "lost")
        self.assertIn("误判", history["items"][2]["reason"])
        self.assertEqual(history["items"][2]["actor_id"], "admin")

        # 恢复后仍是同一株：园区内只有一条记录，且可以重新建立季节志
        plot_detail = self.catalog.get_plot(self.plot["id"])
        self.assertEqual(len(plot_detail["trees"]), 1)
        self.assertEqual(plot_detail["trees"][0]["id"], self.tree["id"])
        with _request("admin", "season-restored"):
            season = self.observations.start_observation(
                {
                    "tree_id": self.tree["id"],
                    "season": "2026",
                    "observer": "周岚",
                    "note": "恢复后继续观察",
                }
            )
        self.assertEqual(season["tree_id"], self.tree["id"])
        self.assertEqual(season["tree_code"], "OR-7201-T01")

        versions = self.repository.list_entity_versions(
            kind="tree",
            identifier=self.tree["id"],
        )
        self.assertEqual([item["revision"] for item in versions["items"]], [3, 2, 1])
        audit = self.repository.list_audit_events(
            resource_kind="tree",
            resource_id=self.tree["id"],
        )
        self.assertEqual(
            [event["action"] for event in audit["items"][:2]],
            ["status_change", "status_change"],
        )

    def test_stale_revision_is_rejected(self) -> None:
        self._change(
            "close-first",
            status="retired",
            reason="更新复壮退出观察",
            evidence="2026-03-01 管护记录",
            revision=self.tree["revision"],
        )
        with self.assertRaises(ConflictError) as stale:
            self._change(
                "stale-restore",
                status="active",
                reason="依据旧页面恢复",
                evidence="旧页面",
                revision=self.tree["revision"],
            )
        self.assertEqual(stale.exception.code, "revision_conflict")

    def test_invalid_target_status_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self._change(
                "bad-status",
                status="removed",
                reason="非法状态",
                evidence="非法依据",
                revision=self.tree["revision"],
            )

    def test_note_is_preserved_when_status_change_omits_it(self) -> None:
        original_note = self.tree["note"]
        closed = self._change(
            "close-note",
            status="lost",
            reason="巡查未见萌发",
            evidence="现场照片",
            revision=self.tree["revision"],
        )
        self.assertEqual(closed["note"], original_note)
        restored = self._change(
            "restore-note",
            status="active",
            reason="复查后确认误判",
            evidence="复查照片",
            revision=closed["revision"],
        )
        self.assertEqual(restored["note"], original_note)

    def test_explicit_note_replaces_original_and_empty_clears(self) -> None:
        closed = self._change(
            "close-with-note",
            status="retired",
            reason="移栽退出观察",
            evidence="交接单",
            note="移栽至南坡新园",
            revision=self.tree["revision"],
        )
        self.assertEqual(closed["note"], "移栽至南坡新园")
        restored = self._change(
            "restore-clear-note",
            status="active",
            reason="误操作恢复",
            evidence="核查记录",
            note="",
            revision=closed["revision"],
        )
        self.assertEqual(restored["note"], "")

    def test_history_keeps_transitions_and_labels_after_reopen(self) -> None:
        closed = self._change(
            "close-reopen",
            status="lost",
            reason="连续两季未见萌发",
            evidence="现场照片与笔录",
            revision=self.tree["revision"],
        )
        self._change(
            "restore-reopen",
            status="active",
            reason="复查发现根蘖重新萌发",
            evidence="复查照片",
            revision=closed["revision"],
        )

        # 重新打开仓储，模拟页面刷新后从持久层读取
        self.repository.close()
        self.repository = Repository(
            Database(Path(self.temporary.name) / "atlas.sqlite3")
        )
        self.repository.open()
        self.catalog = CatalogService(self.repository)

        tree = self.catalog.get_tree(self.tree["id"])
        events = tree["status_history"]
        self.assertEqual(
            [(event["previous_status"], event["status"]) for event in events],
            [(None, "active"), ("active", "lost"), ("lost", "active")],
        )
        # 植株详情内嵌的沿革必须带中文标签，前端无需猜测
        self.assertEqual(
            [event["status_label"] for event in events],
            ["在册", "已遗失", "在册"],
        )
        self.assertIsNone(events[0]["previous_status_label"])
        self.assertEqual(events[1]["previous_status_label"], "在册")
        self.assertEqual(events[2]["previous_status_label"], "已遗失")

        detail = self.catalog.get_plot(self.plot["id"])
        embedded = detail["trees"][0]["status_history"]
        self.assertEqual(
            [event["previous_status_label"] for event in embedded],
            [None, "在册", "已遗失"],
        )
        self.assertEqual(detail["trees"][0]["note"], "建档时备注")

    def test_historical_observations_remain_attached_after_changes(self) -> None:
        with _request("admin", "season-before-close"):
            season = self.observations.start_observation(
                {
                    "tree_id": self.tree["id"],
                    "season": "2025",
                    "observer": "周岚",
                    "note": "关闭前季节志",
                }
            )
        self._change(
            "close-for-history",
            status="retired",
            reason="移栽他处退出本园观察",
            evidence="移栽交接单",
            revision=self.tree["revision"],
        )
        listing = self.observations.list_observations(tree_id=self.tree["id"])
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["id"], season["id"])
        self.assertEqual(listing["items"][0]["tree_code"], "OR-7201-T01")


def _request(actor_id: str, idempotency_key: str):
    return request_scope(
        RequestContext(
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            request_method="PUT",
            request_path=f"/api/trees/x/status/{idempotency_key}",
            request_hash=f"{actor_id}:{idempotency_key}",
            route_template="/api/trees/{tree_id}/status",
        )
    )


if __name__ == "__main__":
    unittest.main()
