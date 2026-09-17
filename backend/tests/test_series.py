from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app.application import (
    CatalogService,
    ComparisonService,
    ObservationService,
    SeriesService,
)
from app.config import RuntimeConfig
from app.errors import DomainError, PreconditionError, ValidationError
from app.persistence import Database, Repository
from app.security import RequestContext, request_scope
from app.transport.server import create_server


def _request(actor_id: str, idempotency_key: str):
    return request_scope(
        RequestContext(
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            request_method="PUT",
            request_path="/api/test",
            request_hash=f"{actor_id}:{idempotency_key}",
            route_template="/api/test",
        )
    )


class SeriesServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "atlas.sqlite3")
        self.repository = Repository(self.database)
        self.repository.open()
        self.catalog = CatalogService(self.repository)
        self.observations = ObservationService(self.repository)
        self.comparisons = ComparisonService(self.repository)
        self.series = SeriesService(self.repository)
        with _request("local-admin", "seed-plot"):
            plot = self.catalog.create_plot(
                {
                    "code": "OR-9001",
                    "name": "多年比较试验园",
                    "locality": "河谷东坡",
                    "cultivar_focus": "酥梨",
                    "steward": "编研组",
                    "planting_year": 2010,
                    "note": "",
                }
            )
        with _request("local-admin", "seed-tree"):
            tree = self.catalog.create_tree(
                {
                    "plot_id": plot["id"],
                    "code": "OR-9001-T01",
                    "cultivar": "酥梨",
                    "rootstock": "杜梨",
                    "planting_year": 2010,
                    "status": "active",
                    "note": "",
                }
            )
        with _request("local-admin", "seed-confirm"):
            self.catalog.confirm_plot(plot["id"], expected_revision=plot["revision"])
        self.tree_id = tree["id"]

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def test_dispositions_distinguish_included_excluded_and_missing(self) -> None:
        self._seed_completed("2023", ["2023-03-10", "2023-04-01", "2023-04-18", "2023-09-02"])
        self._seed_completed(
            "2024",
            ["2024-03-12", "2024-04-03", "2024-04-20", "2024-09-01"],
            extra=[("first_bloom", "2024-03-28")],
        )
        self._seed_open("2025")

        record = self._create_series("2023", "2026")

        dispositions = {year["season"]: year for year in record["years"]}
        self.assertEqual(dispositions["2023"]["disposition"], "included")
        self.assertEqual(dispositions["2023"]["reason_code"], "completed_season")
        self.assertEqual(dispositions["2024"]["disposition"], "included")
        self.assertEqual(dispositions["2025"]["disposition"], "excluded")
        self.assertEqual(dispositions["2025"]["reason_code"], "season_not_completed")
        self.assertEqual(dispositions["2026"]["disposition"], "missing")
        self.assertEqual(dispositions["2026"]["reason_code"], "season_absent")
        self.assertIsNone(dispositions["2026"]["observation_id"])
        self.assertIn("first_bloom", dispositions["2023"]["missing_stages"])
        self.assertEqual(dispositions["2024"]["missing_stages"], [
            "bud_swell",
            "petal_fall",
            "fruit_growth",
            "leaf_fall",
        ])

        summary = record["summary"]
        self.assertEqual(summary["evaluated_year_count"], 4)
        self.assertEqual(summary["included_count"], 2)
        self.assertEqual(summary["excluded_count"], 1)
        self.assertEqual(summary["missing_count"], 1)
        self.assertEqual(summary["included_seasons"], ["2023", "2024"])
        self.assertEqual(summary["excluded_seasons"], ["2025"])
        self.assertEqual(summary["missing_seasons"], ["2026"])
        self.assertIn("不按零值处理", summary["sentence"])
        self.assertEqual(len(record["criteria"]), 5)

    def test_missing_and_excluded_years_never_enter_stage_statistics(self) -> None:
        self._seed_completed("2023", ["2023-03-10", "2023-04-01", "2023-04-18", "2023-09-02"])
        self._seed_completed("2024", ["2024-03-12", "2024-04-03", "2024-04-20", "2024-09-01"])
        self._seed_open("2025", bud_burst="2025-03-20")

        record = self._create_series("2023", "2026")
        rows = {row["stage"]: row for row in record["stage_series"]}

        bud_burst = rows["bud_burst"]
        # 2023-03-10 是第 69 天，2024-03-12（闰年）是第 72 天。
        self.assertEqual(
            [point["day_of_year"] for point in bud_burst["points"]],
            [69, 72],
        )
        self.assertEqual(bud_burst["covered_seasons"], ["2023", "2024"])
        self.assertEqual(bud_burst["missing_seasons"], [])
        # 平均只覆盖两个纳入年份；缺失与排除年份不按零值进入平均。
        self.assertEqual(bud_burst["average_day_of_year"], 70.5)
        self.assertEqual(bud_burst["span_days"], 3)
        self.assertEqual(bud_burst["shift_days"], 3)
        self.assertEqual(bud_burst["first_season"], "2023")
        self.assertEqual(bud_burst["last_season"], "2024")

        leaf_fall = rows["leaf_fall"]
        self.assertEqual(leaf_fall["points"], [])
        self.assertEqual(leaf_fall["covered_seasons"], [])
        self.assertEqual(leaf_fall["missing_seasons"], ["2023", "2024"])
        self.assertIsNone(leaf_fall["average_day_of_year"])
        self.assertIsNone(leaf_fall["span_days"])
        self.assertIsNone(leaf_fall["shift_days"])

    def test_stage_missing_in_one_included_year_is_not_averaged(self) -> None:
        self._seed_completed("2023", ["2023-03-10", "2023-04-01", "2023-04-18", "2023-09-02"])
        self._seed_completed(
            "2024",
            ["2024-03-12", "2024-04-03", "2024-04-20", "2024-09-01"],
            extra=[("first_bloom", "2024-03-28")],
        )

        record = self._create_series("2023", "2024")
        rows = {row["stage"]: row for row in record["stage_series"]}
        first_bloom = rows["first_bloom"]
        self.assertEqual(first_bloom["covered_seasons"], ["2024"])
        self.assertEqual(first_bloom["missing_seasons"], ["2023"])
        # 仅 2024 年覆盖初花期：平均即该年第 88 天，不与缺失年份强行求平均。
        self.assertEqual(first_bloom["average_day_of_year"], 88.0)
        self.assertIsNone(first_bloom["span_days"])
        self.assertIsNone(first_bloom["shift_days"])

    def test_insufficient_included_years_is_rejected(self) -> None:
        self._seed_completed("2023", ["2023-03-10", "2023-04-01", "2023-04-18", "2023-09-02"])
        self._seed_open("2025")
        with self.assertRaises(PreconditionError) as raised:
            self._create_series("2023", "2026")
        self.assertEqual(raised.exception.code, "insufficient_included_years")

    def test_invalid_range_and_unknown_fields_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self._create_series("2026", "2023")
        with self.assertRaises(ValidationError):
            self._create_series("1990", "2025")
        with self.assertRaises(ValidationError) as raised:
            self.series.create_series(
                {
                    "title": "非法字段",
                    "tree_id": self.tree_id,
                    "season_from": "2023",
                    "season_to": "2024",
                    "stage": "bud_burst",
                }
            )
        self.assertEqual(raised.exception.code, "validation_error")
        with self.assertRaises(DomainError) as not_found:
            self.series.create_series(
                {
                    "title": "不存在的植株",
                    "tree_id": "tree_missing",
                    "season_from": "2023",
                    "season_to": "2024",
                }
            )
        self.assertEqual(not_found.exception.code, "not_found")

    def test_duplicate_request_reuses_existing_record(self) -> None:
        self._seed_completed("2023", ["2023-03-10", "2023-04-01", "2023-04-18", "2023-09-02"])
        self._seed_completed("2024", ["2024-03-12", "2024-04-03", "2024-04-20", "2024-09-01"])
        first = self._create_series("2023", "2024")
        second = self._create_series("2023", "2024")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.repository.read()["series"]), 1)

        listed = self.series.list_series()
        self.assertEqual(listed["total"], 1)
        detail = self.series.get_series(first["id"])
        self.assertEqual(detail["summary"]["included_count"], 2)
        with self.assertRaises(DomainError) as raised:
            self.series.get_series("series_missing")
        self.assertEqual(raised.exception.code, "not_found")

    def test_single_year_comparison_behavior_is_unchanged(self) -> None:
        with _request("local-admin", "seed-plot-2"):
            second_plot = self.catalog.create_plot(
                {
                    "code": "OR-9002",
                    "name": "单年对照园",
                    "locality": "河谷西坡",
                    "cultivar_focus": "蜜香梨",
                    "steward": "编研组",
                    "planting_year": 2010,
                    "note": "",
                }
            )
        with _request("local-admin", "seed-tree-2"):
            second_tree = self.catalog.create_tree(
                {
                    "plot_id": second_plot["id"],
                    "code": "OR-9002-T01",
                    "cultivar": "蜜香梨",
                    "rootstock": "杜梨",
                    "planting_year": 2010,
                    "status": "active",
                    "note": "",
                }
            )
        left = self._seed_completed("2026", ["2026-03-10", "2026-04-01", "2026-04-18", "2026-09-02"])
        right = self._seed_completed(
            "2026",
            ["2026-03-15", "2026-04-05", "2026-04-22", "2026-09-07"],
            tree_id=second_tree["id"],
        )
        with _request("local-admin", "create-comparison"):
            comparison = self.comparisons.create_comparison(
                {
                    "title": "单年对齐",
                    "left_observation_id": left["id"],
                    "right_observation_id": right["id"],
                }
            )
        self.assertEqual(len(comparison["stage_offsets"]), 4)
        self.assertEqual(comparison["summary"]["common_stage_count"], 4)
        self.assertEqual(comparison["summary"]["average_offset_days"], 4.5)

    def _plot_id(self) -> str:
        return next(iter(self.repository.read()["plots"]))

    def _tree_by_code(self, code: str) -> dict[str, object]:
        for tree in self.repository.read()["trees"].values():
            if tree["code"] == code:
                return tree
        raise AssertionError(f"未找到植株 {code}")

    def _create_series(self, season_from: str, season_to: str) -> dict[str, object]:
        with _request("local-admin", f"series-{season_from}-{season_to}"):
            return self.series.create_series(
                {
                    "title": f"酥梨 {season_from}–{season_to} 多年比较",
                    "tree_id": self.tree_id,
                    "season_from": season_from,
                    "season_to": season_to,
                }
            )

    def _seed_completed(
        self,
        season: str,
        dates: list[str],
        *,
        extra: list[tuple[str, str]] | None = None,
        tree_id: str | None = None,
    ) -> dict[str, object]:
        observation = self._seed_open(season, tree_id=tree_id)
        stages = ["bud_burst", "full_bloom", "fruit_set", "harvest"]
        entries = list(zip(stages, dates)) + list(extra or [])
        entries.sort(key=lambda item: item[1])
        for stage, observed_on in entries:
            with _request("local-admin", f"stage-{observation['id']}-{stage}"):
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
        with _request("local-admin", f"complete-{observation['id']}"):
            return self.observations.complete_observation(
                observation["id"],
                {"revision": observation["revision"]},
            )

    def _seed_open(
        self,
        season: str,
        *,
        bud_burst: str | None = None,
        tree_id: str | None = None,
    ) -> dict[str, object]:
        with _request("local-admin", f"open-{tree_id or self.tree_id}-{season}"):
            observation = self.observations.start_observation(
                {
                    "tree_id": tree_id or self.tree_id,
                    "season": season,
                    "observer": "编研组",
                    "note": "",
                }
            )
        if bud_burst is not None:
            with _request("local-admin", f"open-stage-{observation['id']}"):
                observation = self.observations.add_stage(
                    observation["id"],
                    {
                        "stage": "bud_burst",
                        "observed_on": bud_burst,
                        "confidence": 3,
                        "note": "",
                        "revision": observation["revision"],
                    },
                )
        return observation


class SeriesHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        data_dir = Path(self.temporary.name)
        self.repository = Repository(Database(data_dir / "atlas.sqlite3"))
        self.repository.open()
        self.server = create_server(
            RuntimeConfig(
                host="127.0.0.1",
                port=0,
                data_dir=data_dir,
                request_limit=1_048_576,
                max_workers=8,
            ),
            self.repository,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/api"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.repository.close()
        self.temporary.cleanup()

    def test_series_routes_explain_inclusion_and_reject_bad_input(self) -> None:
        status, payload = self._request("GET", "/series")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "authentication_required")

        tree_id = self._seed_tree_with_seasons()
        create_status, created = self._request(
            "PUT",
            "/series",
            {
                "title": "酥梨 2023–2026 多年比较",
                "tree_id": tree_id,
                "season_from": "2023",
                "season_to": "2026",
            },
            {"X-Actor-Id": "local-admin", "X-Idempotency-Key": "series-http-1"},
        )
        self.assertEqual(create_status, 200)
        self.assertEqual(created["summary"]["included_count"], 2)
        self.assertEqual(created["summary"]["excluded_count"], 1)
        self.assertEqual(created["summary"]["missing_count"], 1)
        self.assertEqual(len(created["criteria"]), 5)
        bud_burst = next(
            row for row in created["stage_series"] if row["stage"] == "bud_burst"
        )
        self.assertEqual(bud_burst["average_day_of_year"], 70.5)

        list_status, listed = self._request(
            "GET", "/series", headers={"X-Actor-Id": "local-admin"}
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["items"][0]["summary"]["missing_count"], 1)

        detail_status, detail = self._request(
            "GET",
            f"/series/{created['id']}",
            headers={"X-Actor-Id": "local-admin"},
        )
        self.assertEqual(detail_status, 200)
        years = {year["season"]: year["disposition"] for year in detail["years"]}
        self.assertEqual(
            years,
            {"2023": "included", "2024": "included", "2025": "excluded", "2026": "missing"},
        )

        missing_status, missing = self._request(
            "GET", "/series/series_none", headers={"X-Actor-Id": "local-admin"}
        )
        self.assertEqual(missing_status, 404)
        self.assertEqual(missing["error"]["code"], "not_found")

        bad_status, bad = self._request(
            "PUT",
            "/series",
            {
                "title": "倒置年份",
                "tree_id": tree_id,
                "season_from": "2026",
                "season_to": "2023",
            },
            {"X-Actor-Id": "local-admin", "X-Idempotency-Key": "series-http-2"},
        )
        self.assertEqual(bad_status, 422)
        self.assertEqual(bad["error"]["code"], "validation_error")

    def _seed_tree_with_seasons(self) -> str:
        headers = {"X-Actor-Id": "local-admin"}
        _, plot = self._request(
            "PUT",
            "/plots",
            {
                "code": "OR-9101",
                "name": "HTTP 多年园",
                "locality": "测试地",
                "cultivar_focus": "酥梨",
                "steward": "测试组",
                "planting_year": 2010,
                "note": "",
            },
            {**headers, "X-Idempotency-Key": "http-series-plot"},
        )
        _, tree = self._request(
            "PUT",
            "/trees",
            {
                "plot_id": plot["id"],
                "code": "OR-9101-T01",
                "cultivar": "酥梨",
                "rootstock": "杜梨",
                "planting_year": 2010,
                "status": "active",
                "note": "",
            },
            {**headers, "X-Idempotency-Key": "http-series-tree"},
        )
        seasons = {
            "2023": ["2023-03-10", "2023-04-01", "2023-04-18", "2023-09-02"],
            "2024": ["2024-03-12", "2024-04-03", "2024-04-20", "2024-09-01"],
        }
        for season, dates in seasons.items():
            _, observation = self._request(
                "PUT",
                "/observations",
                {
                    "tree_id": tree["id"],
                    "season": season,
                    "observer": "测试组",
                    "note": "",
                },
                {**headers, "X-Idempotency-Key": f"http-series-obs-{season}"},
            )
            stages = ["bud_burst", "full_bloom", "fruit_set", "harvest"]
            for stage, observed_on in zip(stages, dates):
                _, observation = self._request(
                    "PUT",
                    f"/observations/{observation['id']}/stages",
                    {
                        "stage": stage,
                        "observed_on": observed_on,
                        "confidence": 4,
                        "note": "",
                        "revision": observation["revision"],
                    },
                    {
                        **headers,
                        "X-Idempotency-Key": f"http-series-{season}-{stage}",
                    },
                )
            self._request(
                "PUT",
                f"/observations/{observation['id']}/complete",
                {"revision": observation["revision"]},
                {**headers, "X-Idempotency-Key": f"http-series-done-{season}"},
            )
        self._request(
            "PUT",
            "/observations",
            {"tree_id": tree["id"], "season": "2025", "observer": "测试组", "note": ""},
            {**headers, "X-Idempotency-Key": "http-series-obs-2025"},
        )
        return tree["id"]

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read().decode("utf-8"))
            finally:
                error.close()


if __name__ == "__main__":
    unittest.main()
