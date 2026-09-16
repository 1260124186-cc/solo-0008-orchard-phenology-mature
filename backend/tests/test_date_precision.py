"""阶段日期精度、顺序、季节窗口、比较与恢复校验的测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.application import ComparisonService, ObservationService
from app.domain.comparison_rules import (
    calculate_stage_offsets,
    create_comparison_record,
)
from app.domain.date_precision import (
    PRECISION_DAY,
    PRECISION_ON_OR_AFTER,
    PRECISION_ON_OR_BEFORE,
    PRECISION_RANGE,
    clean_observed_date,
    offset_between,
    stages_in_order,
)
from app.domain.observation_rules import (
    add_stage_entry,
    complete_observation_record,
    create_observation_record,
)
from app.errors import DomainError, ValidationError
from app.persistence import Database, Repository
from app.security import RequestContext, request_scope


def _tree(tree_id: str = "tree_season_a") -> dict[str, object]:
    return {"id": tree_id, "plot_id": "plot_season", "status": "active"}


def _observation(season: str = "2026") -> dict[str, object]:
    return create_observation_record(
        {
            "tree_id": "tree_season_a",
            "season": season,
            "observer": "测试观察员",
            "note": "",
        },
        _tree(),
        timestamp="2026-01-01T00:00:00+00:00",
    )


def _add(
    observation: dict[str, object],
    stage: str,
    payload: dict[str, object],
    *,
    revision: int | None = None,
) -> dict[str, object]:
    body = {"stage": stage, "confidence": 4, "note": "", **payload}
    return add_stage_entry(
        observation,
        body,
        expected_revision=revision if revision is not None else observation["revision"],
    )


class DatePrecisionCleaningTests(unittest.TestCase):
    def test_missing_precision_defaults_to_single_day(self) -> None:
        observed = clean_observed_date({"observed_on": "2026-03-14"})
        self.assertEqual(observed.precision, PRECISION_DAY)
        self.assertEqual(observed.anchor.isoformat(), "2026-03-14")

    def test_range_requires_end_date_and_order(self) -> None:
        observed = clean_observed_date(
            {
                "precision": "range",
                "observed_on": "2026-03-10",
                "observed_end_on": "2026-03-14",
            }
        )
        self.assertEqual(observed.precision, PRECISION_RANGE)
        self.assertEqual(observed.start.isoformat(), "2026-03-10")
        self.assertEqual(observed.end.isoformat(), "2026-03-14")

        with self.assertRaises(ValidationError) as raised:
            clean_observed_date(
                {"precision": "range", "observed_on": "2026-03-14"}
            )
        self.assertEqual(raised.exception.details["field"], "observed_end_on")

        with self.assertRaises(ValidationError):
            clean_observed_date(
                {
                    "precision": "range",
                    "observed_on": "2026-03-14",
                    "observed_end_on": "2026-03-10",
                }
            )

    def test_non_range_precision_rejects_end_date(self) -> None:
        with self.assertRaises(ValidationError):
            clean_observed_date(
                {
                    "precision": "day",
                    "observed_on": "2026-03-14",
                    "observed_end_on": "2026-03-15",
                }
            )

    def test_unknown_precision_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            clean_observed_date(
                {"precision": "week", "observed_on": "2026-03-14"}
            )

    def test_boundary_precisions_keep_one_known_end(self) -> None:
        before = clean_observed_date(
            {"precision": "on_or_before", "observed_on": "2026-04-01"}
        )
        self.assertEqual(before.precision, PRECISION_ON_OR_BEFORE)
        self.assertIsNone(before.start)
        self.assertEqual(before.end.isoformat(), "2026-04-01")
        self.assertEqual(before.anchor.isoformat(), "2026-04-01")

        after = clean_observed_date(
            {"precision": "on_or_after", "observed_on": "2026-04-01"}
        )
        self.assertEqual(after.precision, PRECISION_ON_OR_AFTER)
        self.assertEqual(after.start.isoformat(), "2026-04-01")
        self.assertIsNone(after.end)


class SequenceAndWindowTests(unittest.TestCase):
    def test_uncertain_intervals_keep_order_when_overlap_possible(self) -> None:
        # 前一阶段 3 月 10–14 日，后一阶段不早于 3 月 14 日：可能不递减。
        earlier = clean_observed_date(
            {
                "precision": "range",
                "observed_on": "2026-03-10",
                "observed_end_on": "2026-03-14",
            }
        )
        later = clean_observed_date(
            {"precision": "on_or_after", "observed_on": "2026-03-12"}
        )
        self.assertTrue(stages_in_order(earlier, later))

    def test_provable_reversal_is_rejected(self) -> None:
        # 后一阶段最晚可能日仍早于前一阶段最早可能日，确定倒退。
        earlier = clean_observed_date(
            {"precision": "on_or_after", "observed_on": "2026-03-20"}
        )
        later = clean_observed_date(
            {"precision": "on_or_before", "observed_on": "2026-03-10"}
        )
        self.assertFalse(stages_in_order(earlier, later))

    def test_entries_with_range_and_boundaries_pass_sequence(self) -> None:
        observation = _observation()
        observation = _add(
            observation,
            "bud_burst",
            {"precision": "day", "observed_on": "2026-03-14"},
        )
        observation = _add(
            observation,
            "full_bloom",
            {
                "precision": "range",
                "observed_on": "2026-04-04",
                "observed_end_on": "2026-04-08",
            },
        )
        observation = _add(
            observation,
            "fruit_set",
            {"precision": "on_or_after", "observed_on": "2026-04-20"},
        )
        observation = _add(
            observation,
            "harvest",
            {"precision": "on_or_before", "observed_on": "2026-09-20"},
        )
        completed = complete_observation_record(
            observation, expected_revision=observation["revision"]
        )
        self.assertEqual(completed["status"], "completed")
        stored = {entry["stage"]: entry for entry in completed["entries"]}
        self.assertEqual(
            stored["full_bloom"]["observed_end_on"], "2026-04-08"
        )
        self.assertEqual(stored["fruit_set"]["precision"], "on_or_after")
        self.assertNotIn("observed_end_on", stored["harvest"])

    def test_reversed_entries_are_rejected_at_input(self) -> None:
        observation = _observation()
        # 按阶段顺序录入，但后续阶段被确定地限制在更早的时间。
        observation = _add(
            observation,
            "bud_burst",
            {"precision": "on_or_after", "observed_on": "2026-03-20"},
        )
        with self.assertRaises(ValidationError):
            _add(
                observation,
                "full_bloom",
                {"precision": "on_or_before", "observed_on": "2026-03-10"},
            )

    def test_window_validates_all_known_boundaries(self) -> None:
        observation = _observation()
        # 2026 季节窗口为 2025-10-03 至 2027-03-31，区间起点在窗口外。
        with self.assertRaises(ValidationError) as raised:
            _add(
                observation,
                "bud_burst",
                {
                    "precision": "range",
                    "observed_on": "2025-10-01",
                    "observed_end_on": "2025-10-10",
                },
            )
        self.assertEqual(raised.exception.details["field"], "observed_on")

    def test_on_or_before_window_anchor_inside_window(self) -> None:
        observation = _observation()
        # 2025-10-03 正好是窗口起点，开放的更早端由档案语义保留。
        updated = _add(
            observation,
            "bud_burst",
            {"precision": "on_or_before", "observed_on": "2025-10-03"},
        )
        self.assertEqual(updated["entries"][0]["observed_on"], "2025-10-03")


class OffsetRangeTests(unittest.TestCase):
    def _date(self, payload: dict[str, str]):
        return clean_observed_date(payload)

    def test_exact_dates_keep_historical_offset(self) -> None:
        left = self._date({"observed_on": "2026-04-01"})
        right = self._date({"observed_on": "2026-04-05"})
        offset_range = offset_between(left, right)
        self.assertTrue(offset_range.is_exact)
        self.assertEqual(offset_range.minimum, 4)
        self.assertEqual(offset_range.maximum, 4)

    def test_range_pair_preserves_offset_interval(self) -> None:
        left = self._date(
            {
                "precision": "range",
                "observed_on": "2026-04-01",
                "observed_end_on": "2026-04-03",
            }
        )
        right = self._date(
            {
                "precision": "range",
                "observed_on": "2026-04-06",
                "observed_end_on": "2026-04-12",
            }
        )
        offset_range = offset_between(left, right)
        self.assertEqual(offset_range.minimum, 3)
        self.assertEqual(offset_range.maximum, 11)
        self.assertFalse(offset_range.is_exact)

    def test_open_boundaries_yield_unbounded_offsets(self) -> None:
        left = self._date(
            {"precision": "on_or_after", "observed_on": "2026-04-01"}
        )
        right = self._date(
            {"precision": "on_or_before", "observed_on": "2026-04-05"}
        )
        offset_range = offset_between(left, right)
        # 左侧可能无限晚、右侧可能无限早，下确界无界；
        # 上确界为 4 天（右最晚边界 - 左最早边界）。
        self.assertIsNone(offset_range.minimum)
        self.assertEqual(offset_range.maximum, 4)

        both_unbounded = offset_between(
            self._date({"precision": "on_or_after", "observed_on": "2026-04-01"}),
            self._date({"precision": "on_or_after", "observed_on": "2026-04-05"}),
        )
        self.assertIsNone(both_unbounded.minimum)
        self.assertIsNone(both_unbounded.maximum)

    def test_comparison_rows_keep_range_and_no_fabricated_midpoint(self) -> None:
        left = _observation()
        right_season = create_observation_record(
            {
                "tree_id": "tree_season_b",
                "season": "2026",
                "observer": "测试观察员",
                "note": "",
            },
            {"id": "tree_season_b", "plot_id": "plot_season", "status": "active"},
            timestamp="2026-01-01T00:00:00+00:00",
        )
        left = _add(
            left,
            "bud_burst",
            {"precision": "day", "observed_on": "2026-03-10"},
        )
        left = _add(
            left,
            "full_bloom",
            {
                "precision": "range",
                "observed_on": "2026-04-01",
                "observed_end_on": "2026-04-03",
            },
        )
        left = _add(
            left,
            "fruit_set",
            {"precision": "day", "observed_on": "2026-04-18"},
        )
        left = _add(
            left,
            "harvest",
            {"precision": "day", "observed_on": "2026-09-02"},
        )
        left = complete_observation_record(
            left, expected_revision=left["revision"]
        )
        right_season = _add(
            right_season,
            "bud_burst",
            {"precision": "day", "observed_on": "2026-03-15"},
        )
        right_season = _add(
            right_season,
            "full_bloom",
            {
                "precision": "range",
                "observed_on": "2026-04-05",
                "observed_end_on": "2026-04-09",
            },
        )
        right_season = _add(
            right_season,
            "fruit_set",
            {"precision": "day", "observed_on": "2026-04-22"},
        )
        right_season = _add(
            right_season,
            "harvest",
            {"precision": "day", "observed_on": "2026-09-07"},
        )
        right_season = complete_observation_record(
            right_season, expected_revision=right_season["revision"]
        )

        rows = {
            row["stage"]: row
            for row in calculate_stage_offsets(left, right_season)
        }
        bloom = rows["full_bloom"]
        self.assertFalse(bloom["offset_exact"])
        self.assertEqual(bloom["offset_min_days"], 2)
        self.assertEqual(bloom["offset_max_days"], 8)
        self.assertNotIn("offset_days", bloom)
        self.assertEqual(bloom["left_end_date"], "2026-04-03")
        self.assertEqual(bloom["right_end_date"], "2026-04-09")

        burst = rows["bud_burst"]
        self.assertTrue(burst["offset_exact"])
        self.assertEqual(burst["offset_days"], 5)

        record = create_comparison_record(
            {
                "title": "不确定精度对齐",
                "left_observation_id": left["id"],
                "right_observation_id": right_season["id"],
            },
            left,
            right_season,
            None,
            None,
            timestamp="2026-09-10T00:00:00+00:00",
        )
        summary = record["summary"]
        self.assertFalse(summary["all_dates_exact"])
        self.assertEqual(summary["exact_stage_count"], 3)
        self.assertIsNone(summary["average_offset_days"])
        self.assertIn("偏移", summary["sentence"])

    def test_same_direction_open_comparison_is_direction_unknown(self) -> None:
        left = _observation()
        right_season = create_observation_record(
            {
                "tree_id": "tree_season_b",
                "season": "2026",
                "observer": "测试观察员",
                "note": "",
            },
            {"id": "tree_season_b", "plot_id": "plot_season", "status": "active"},
            timestamp="2026-01-01T00:00:00+00:00",
        )
        left = _add(
            left, "bud_burst", {"observed_on": "2026-03-10"}
        )
        left = _add(
            left,
            "full_bloom",
            {"precision": "on_or_after", "observed_on": "2026-04-01"},
        )
        left = _add(left, "fruit_set", {"observed_on": "2026-04-18"})
        left = _add(left, "harvest", {"observed_on": "2026-09-02"})
        left = complete_observation_record(
            left, expected_revision=left["revision"]
        )
        for stage, date_value in (
            ("bud_burst", "2026-03-15"),
            ("fruit_set", "2026-04-22"),
            ("harvest", "2026-09-07"),
        ):
            right_season = _add(
                right_season, stage, {"observed_on": date_value}
            )
        right_season = _add(
            right_season,
            "full_bloom",
            {"precision": "on_or_after", "observed_on": "2026-04-05"},
        )
        right_season = complete_observation_record(
            right_season, expected_revision=right_season["revision"]
        )

        rows = {
            row["stage"]: row
            for row in calculate_stage_offsets(left, right_season)
        }
        bloom = rows["full_bloom"]
        self.assertFalse(bloom["offset_exact"])
        self.assertIsNone(bloom["offset_min_days"])
        self.assertIsNone(bloom["offset_max_days"])
        self.assertNotIn("offset_days", bloom)
        # 其余单日阶段仍精确。
        self.assertTrue(rows["harvest"]["offset_exact"])
        self.assertEqual(rows["harvest"]["offset_days"], 5)

        record = create_comparison_record(
            {
                "title": "同方向开放",
                "left_observation_id": left["id"],
                "right_observation_id": right_season["id"],
            },
            left,
            right_season,
            None,
            None,
            timestamp="2026-09-10T00:00:00+00:00",
        )
        summary = record["summary"]
        self.assertIsNone(summary["average_offset_days"])
        self.assertIsNone(summary["minimum_offset_days"])
        self.assertIsNone(summary["maximum_offset_days"])
        self.assertIn("方向待定", summary["sentence"])
        self.assertEqual(summary["exact_stage_count"], 3)


class ServiceRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name)
        self.repository = Repository(Database(self.data_dir / "atlas.sqlite3"))
        self.repository.open()
        self.observations = ObservationService(self.repository)
        self.comparisons = ComparisonService(self.repository)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _request(self, key: str):
        return request_scope(
            RequestContext(
                actor_id="local-admin",
                idempotency_key=key,
                request_method="PUT",
                request_path="/api/test",
                request_hash=f"local-admin:{key}",
                route_template="/api/test",
            )
        )

    def test_precision_fields_survive_repository_round_trip(self) -> None:
        self._seed_tree("tree_season_a", "plot_season")
        with self._request("obs-range-start"):
            observation = self.observations.start_observation(
                {
                    "tree_id": "tree_season_a",
                    "season": "2025",
                    "observer": "测试观察员",
                    "note": "",
                }
            )
        with self._request("obs-range-add"):
            observation = self.observations.add_stage(
                observation["id"],
                {
                    "stage": "bud_burst",
                    "precision": "range",
                    "observed_on": "2025-03-10",
                    "observed_end_on": "2025-03-14",
                    "confidence": 4,
                    "note": "",
                    "revision": observation["revision"],
                },
            )
            entry = observation["entries"][0]
            self.assertEqual(entry["precision"], "range")
            self.assertEqual(entry["observed_end_on"], "2025-03-14")

        reloaded = Repository(Database(self.data_dir / "atlas.sqlite3"))
        reloaded.open()
        state = reloaded.read()
        stored = state["observations"][observation["id"]]["entries"][0]
        self.assertEqual(stored["precision"], "range")
        self.assertEqual(stored["observed_on"], "2025-03-10")
        self.assertEqual(stored["observed_end_on"], "2025-03-14")
        reloaded.close()

    def test_corrupt_range_without_end_date_blocks_recovery(self) -> None:
        self._seed_tree("tree_corrupt", "plot_corrupt")
        with self._request("obs-corrupt"):
            observation = self.observations.start_observation(
                {
                    "tree_id": "tree_corrupt",
                    "season": "2026",
                    "observer": "测试观察员",
                    "note": "",
                }
            )
        state = self.repository.read()
        record = state["observations"][observation["id"]]
        record["entries"].append(
            {
                "id": "entry_bad",
                "stage": "bud_burst",
                "observed_on": "2026-03-10",
                "precision": "range",
                "confidence": 4,
                "note": "",
            }
        )
        # 绕过领域校验直接写入损坏快照。
        with self.repository.database.transaction(immediate=True) as connection:
            import json

            connection.execute(
                """
                INSERT INTO entities (kind, id, payload, revision, created_at, updated_at)
                VALUES ('observation', ?, ?, 9, '2026-01-01T00:00:00+00:00',
                        '2026-01-01T00:00:00+00:00')
                ON CONFLICT(kind, id) DO UPDATE SET payload = excluded.payload
                """,
                (record["id"], json.dumps(record, ensure_ascii=False)),
            )

        recovered = Repository(Database(self.data_dir / "atlas.sqlite3"))
        with self.assertRaises(DomainError) as raised:
            recovered.open()
        self.assertEqual(raised.exception.code, "state_corrupt")
        recovered.close()

    def test_same_direction_open_offsets_survive_recovery(self) -> None:
        from app.persistence.snapshot import _check_comparison_record

        def comparison_record(stage_rows: list[dict[str, object]]) -> dict[str, object]:
            return {
                "id": "atlas_open",
                "stage_offsets": stage_rows,
            }

        open_row = {
            "stage": "full_bloom",
            "label": "盛花期",
            "rank": 40,
            "left_date": "2026-04-01",
            "right_date": "2026-04-05",
            "left_precision": "on_or_after",
            "right_precision": "on_or_after",
            "offset_min_days": None,
            "offset_max_days": None,
            "offset_exact": False,
            "confidence_gap": 0,
        }
        exact_row = {
            "stage": "harvest",
            "label": "采收期",
            "rank": 80,
            "left_date": "2026-09-02",
            "right_date": "2026-09-07",
            "left_precision": "day",
            "right_precision": "day",
            "offset_days": 5,
            "offset_min_days": 5,
            "offset_max_days": 5,
            "offset_exact": True,
            "confidence_gap": 0,
        }
        # 全开放行与精确行混合是合法的，恢复校验必须接受。
        _check_comparison_record(comparison_record([open_row, exact_row]))

        # 历史比较只有 offset_days（三个新字段整体缺失）仍被接受。
        legacy_row = {
            "stage": "harvest",
            "label": "采收期",
            "rank": 80,
            "left_date": "2026-09-02",
            "right_date": "2026-09-07",
            "offset_days": 5,
            "confidence_gap": 0,
        }
        _check_comparison_record(comparison_record([legacy_row]))

        # 不确定行携带虚构精确天数 -> 损坏。
        corrupt_open = {**open_row, "offset_days": 3}
        with self.assertRaises(DomainError):
            _check_comparison_record(comparison_record([corrupt_open]))

        # 只有一个边界字段（混合缺失）-> 损坏。
        partial_row = {k: v for k, v in open_row.items() if k != "offset_max_days"}
        with self.assertRaises(DomainError):
            _check_comparison_record(comparison_record([partial_row]))

        # 精确行的范围不一致 -> 损坏。
        bad_exact = {**exact_row, "offset_max_days": 6}
        with self.assertRaises(DomainError):
            _check_comparison_record(comparison_record([bad_exact]))

    def _seed_tree(self, tree_id: str, plot_id: str) -> None:
        import json

        with self.repository.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO entities (kind, id, payload, revision, created_at, updated_at)
                VALUES ('plot', ?, ?, 1, '2026-01-01T00:00:00+00:00',
                        '2026-01-01T00:00:00+00:00')
                """,
                (
                    plot_id,
                    json.dumps(
                        {
                            "id": plot_id,
                            "schema_version": 1,
                            "code": "OR-9999",
                            "name": "恢复测试园",
                            "locality": "测试地",
                            "cultivar_focus": "测试品种",
                            "steward": "测试组",
                            "planting_year": 2010,
                            "note": "",
                            "status": "confirmed",
                            "revision": 1,
                            "created_at": "2026-01-01T00:00:00+00:00",
                            "updated_at": "2026-01-01T00:00:00+00:00",
                            "confirmed_at": "2026-01-01T00:00:00+00:00",
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            connection.execute(
                """
                INSERT INTO entities (kind, id, payload, revision, created_at, updated_at)
                VALUES ('tree', ?, ?, 1, '2026-01-01T00:00:00+00:00',
                        '2026-01-01T00:00:00+00:00')
                """,
                (
                    tree_id,
                    json.dumps(
                        {
                            "id": tree_id,
                            "schema_version": 1,
                            "plot_id": plot_id,
                            "code": f"OR-9999-{tree_id[-2:]}",
                            "cultivar": "测试品种",
                            "rootstock": "杜梨",
                            "planting_year": 2010,
                            "status": "active",
                            "note": "",
                            "revision": 1,
                            "created_at": "2026-01-01T00:00:00+00:00",
                            "updated_at": "2026-01-01T00:00:00+00:00",
                        },
                        ensure_ascii=False,
                    ),
                ),
            )


if __name__ == "__main__":
    unittest.main()
