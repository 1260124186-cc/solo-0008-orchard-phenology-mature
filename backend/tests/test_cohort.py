from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app.config import RuntimeConfig
from app.persistence import Database, Repository
from app.transport.server import create_server


class CohortHttpTests(unittest.TestCase):
    """多年比较队列：纳入、排除、缺失与统计口径的端到端验证。"""

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
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/api"
        self.tree = self._seed_tree()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.repository.close()
        self.temporary.cleanup()

    def test_cohort_classifies_included_excluded_and_missing_years(self) -> None:
        self._complete_season(
            "2023",
            ["2023-03-12", "2023-04-02", "2023-04-20", "2023-09-01"],
        )
        self._open_season("2024", ["2024-03-15"])
        self._complete_season(
            "2026",
            ["2026-03-16", "2026-04-06", "2026-04-25", "2026-09-06"],
        )

        status, cohort = self._create_cohort("2023", "2026")
        self.assertEqual(status, 200)
        years = {item["season"]: item for item in cohort["years"]}
        self.assertEqual(years["2023"]["status"], "included")
        self.assertEqual(years["2023"]["reason_code"], "included")
        self.assertEqual(years["2024"]["status"], "excluded")
        self.assertEqual(years["2024"]["reason_code"], "record_incomplete")
        self.assertEqual(years["2025"]["status"], "missing")
        self.assertEqual(years["2025"]["reason_code"], "missing_year")
        self.assertEqual(years["2026"]["status"], "included")

        summary = cohort["summary"]
        self.assertEqual(summary["included_count"], 2)
        self.assertEqual(summary["excluded_count"], 1)
        self.assertEqual(summary["missing_count"], 1)
        self.assertIn("纳入 2 年", summary["sentence"])
        self.assertIn("排除 1 年", summary["sentence"])
        self.assertIn("缺失 1 年", summary["sentence"])

    def test_missing_years_are_not_zero_filled_or_averaged(self) -> None:
        self._complete_season(
            "2023",
            ["2023-03-12", "2023-04-02", "2023-04-20", "2023-09-01"],
        )
        self._complete_season(
            "2026",
            ["2026-03-16", "2026-04-06", "2026-04-25", "2026-09-06"],
        )

        status, cohort = self._create_cohort("2023", "2026")
        self.assertEqual(status, 200)
        series = {item["stage"]: item for item in cohort["stage_series"]}
        bud_burst = series["bud_burst"]
        self.assertTrue(bud_burst["comparable"])
        self.assertEqual(
            [point["season"] for point in bud_burst["points"]],
            ["2023", "2026"],
        )
        # 缺失年份不产生零值数据点。
        self.assertNotIn("2024", [point["season"] for point in bud_burst["points"]])
        self.assertNotIn("2025", [point["season"] for point in bud_burst["points"]])
        self.assertTrue(
            all(point["season_day"] > 0 for point in bud_burst["points"])
        )
        # 平均只覆盖纳入年份：2023-03-12 第 71 天，2026-03-16 第 75 天。
        self.assertEqual(bud_burst["average_season_day"], 73.0)
        self.assertEqual(
            bud_burst["deltas"],
            [{"from_season": "2023", "to_season": "2026", "span_years": 3, "days": 4}],
        )
        # 未记录的可选阶段不可比，且不强行求平均。
        bud_swell = series["bud_swell"]
        self.assertFalse(bud_swell["comparable"])
        self.assertIsNone(bud_swell["average_season_day"])
        self.assertEqual(bud_swell["trend"], "数据不足")

    def test_stage_missing_is_tracked_separately_from_year_missing(self) -> None:
        self._complete_season(
            "2023",
            ["2023-03-12", "2023-04-02", "2023-04-20", "2023-09-01"],
        )
        self._complete_season(
            "2024",
            [
                "2024-03-10",
                "2024-04-01",
                "2024-04-19",
                "2024-09-02",
            ],
            extra=[("leaf_fall", "2024-11-20")],
        )

        status, cohort = self._create_cohort("2023", "2024")
        self.assertEqual(status, 200)
        series = {item["stage"]: item for item in cohort["stage_series"]}
        leaf_fall = series["leaf_fall"]
        self.assertFalse(leaf_fall["comparable"])
        self.assertIsNone(leaf_fall["average_season_day"])
        self.assertEqual(leaf_fall["missing_seasons"], ["2023"])
        self.assertEqual([point["season"] for point in leaf_fall["points"]], ["2024"])

    def test_cohort_requires_at_least_one_included_year(self) -> None:
        self._open_season("2025", ["2025-03-15"])
        status, payload = self._create_cohort("2024", "2026")
        self.assertEqual(status, 412)
        self.assertEqual(payload["error"]["code"], "no_included_year")

    def test_cohort_validates_range_and_fields(self) -> None:
        self._complete_season(
            "2023",
            ["2023-03-12", "2023-04-02", "2023-04-20", "2023-09-01"],
        )
        status, payload = self._create_cohort("2026", "2023")
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "validation_error")

        status, payload = self._create_cohort("1980", "2026")
        self.assertEqual(status, 422)
        self.assertIn("maximum_span", payload["error"]["details"])

        status, payload = self._request(
            "PUT",
            "/cohorts",
            {
                "title": "非法字段队列",
                "tree_id": self.tree["id"],
                "season_start": "2023",
                "season_end": "2023",
                "stage": "bud_burst",
            },
        )
        self.assertEqual(status, 422)
        self.assertIn("unknown_fields", payload["error"]["details"])

    def test_cohort_creation_is_deduplicated_per_tree_and_range(self) -> None:
        self._complete_season(
            "2023",
            ["2023-03-12", "2023-04-02", "2023-04-20", "2023-09-01"],
        )
        first_status, first = self._create_cohort("2022", "2024")
        second_status, second = self._create_cohort("2022", "2024")
        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(first["id"], second["id"])

        list_status, listing = self._request("GET", "/cohorts")
        self.assertEqual(list_status, 200)
        self.assertEqual(listing["total"], 1)
        item = listing["items"][0]
        self.assertEqual(item["summary"]["included_count"], 1)
        self.assertEqual(item["summary"]["missing_count"], 2)

        filtered_status, filtered = self._request(
            "GET",
            f"/cohorts?tree_id={self.tree['id']}",
        )
        self.assertEqual(filtered_status, 200)
        self.assertEqual(filtered["total"], 1)
        other_status, other = self._request("GET", "/cohorts?tree_id=tree_none")
        self.assertEqual(other_status, 200)
        self.assertEqual(other["total"], 0)

    def test_cohort_export_explains_inclusion_criteria(self) -> None:
        self._complete_season(
            "2023",
            ["2023-03-12", "2023-04-02", "2023-04-20", "2023-09-01"],
        )
        self._open_season("2024", ["2024-03-15"])
        self._complete_season(
            "2026",
            ["2026-03-16", "2026-04-06", "2026-04-25", "2026-09-06"],
        )
        _, cohort = self._create_cohort("2023", "2026")

        status, export = self._request("GET", f"/cohorts/{cohort['id']}/export")
        self.assertEqual(status, 200)
        self.assertIn("多年队列", export["filename"])
        content = export["content"]
        self.assertIn("纳入 2 年", content)
        self.assertIn("排除 1 年", content)
        self.assertIn("缺失 1 年", content)
        self.assertIn("不按零值计入", content)
        self.assertIn("记录不可用", content)
        self.assertIn("2024｜排除｜", content)
        self.assertIn("2025｜缺失｜", content)
        self.assertIn("萌芽期｜可比 2 年｜平均季节年第 73 天", content)
        self.assertIn("落叶期｜数据不足（0 个可比年份），不计算均值", content)

        detail_status, detail = self._request("GET", f"/cohorts/{cohort['id']}")
        self.assertEqual(detail_status, 200)
        self.assertEqual(detail["summary"]["included_count"], 2)

        missing_status, missing = self._request("GET", "/cohorts/cohort_none/export")
        self.assertEqual(missing_status, 404)
        self.assertEqual(missing["error"]["code"], "not_found")

    def test_single_year_comparison_behavior_is_unchanged(self) -> None:
        first = self._complete_season(
            "2026",
            ["2026-03-10", "2026-04-01", "2026-04-18", "2026-09-02"],
        )
        other_tree = self._seed_tree(code_suffix="T02", plot_code="OR-9302")
        second = self._complete_season(
            "2026",
            ["2026-03-15", "2026-04-05", "2026-04-22", "2026-09-07"],
            tree=other_tree,
        )
        status, comparison = self._request(
            "PUT",
            "/comparisons",
            {
                "title": "单年对齐回归",
                "left_observation_id": first["id"],
                "right_observation_id": second["id"],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(comparison["stage_offsets"]), 4)
        self.assertEqual(comparison["summary"]["common_stage_count"], 4)
        self.assertEqual(comparison["summary"]["average_offset_days"], 4.5)

    def _seed_tree(
        self,
        *,
        code_suffix: str = "T01",
        plot_code: str = "OR-9301",
    ) -> dict[str, object]:
        _, plot = self._request(
            "PUT",
            "/plots",
            {
                "code": plot_code,
                "name": f"队列验证园 {plot_code}",
                "locality": "测试地",
                "cultivar_focus": "测试品种",
                "steward": "测试组",
                "planting_year": 2010,
                "note": "",
            },
        )
        _, tree = self._request(
            "PUT",
            "/trees",
            {
                "plot_id": plot["id"],
                "code": f"{plot_code}-{code_suffix}",
                "cultivar": "测试梨",
                "rootstock": "杜梨",
                "planting_year": 2010,
                "status": "active",
                "note": "",
            },
        )
        return tree

    def _complete_season(
        self,
        season: str,
        dates: list[str],
        *,
        tree: dict[str, object] | None = None,
        extra: list[tuple[str, str]] | None = None,
    ) -> dict[str, object]:
        observation = self._open_season(season, dates, tree=tree, extra=extra)
        status, completed = self._request(
            "PUT",
            f"/observations/{observation['id']}/complete",
            {"revision": observation["revision"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(completed["status"], "completed")
        return completed

    def _open_season(
        self,
        season: str,
        dates: list[str],
        *,
        tree: dict[str, object] | None = None,
        extra: list[tuple[str, str]] | None = None,
    ) -> dict[str, object]:
        target = tree or self.tree
        status, observation = self._request(
            "PUT",
            "/observations",
            {
                "tree_id": target["id"],
                "season": season,
                "observer": "队列测试组",
                "note": "",
            },
        )
        self.assertEqual(status, 200)
        stages = ["bud_burst", "full_bloom", "fruit_set", "harvest"]
        entries = list(zip(stages, dates)) + list(extra or [])
        for stage, observed_on in entries:
            status, observation = self._request(
                "PUT",
                f"/observations/{observation['id']}/stages",
                {
                    "stage": stage,
                    "observed_on": observed_on,
                    "confidence": 4,
                    "note": "",
                    "revision": observation["revision"],
                },
            )
            self.assertEqual(status, 200)
        return observation

    def _create_cohort(
        self,
        season_start: str,
        season_end: str,
    ) -> tuple[int, dict[str, object]]:
        return self._request(
            "PUT",
            "/cohorts",
            {
                "title": f"{season_start}—{season_end} 年队列",
                "tree_id": self.tree["id"],
                "season_start": season_start,
                "season_end": season_end,
            },
        )

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object]]:
        request = urllib.request.Request(
            f"{self.base}{path}",
            method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={
                "X-Actor-Id": "local-admin",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
