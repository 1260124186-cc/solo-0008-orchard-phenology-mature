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


class HttpFoundationTests(unittest.TestCase):
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

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.repository.close()
        self.temporary.cleanup()

    def test_http_auth_idempotency_audit_and_jobs(self) -> None:
        status, payload = self._request("GET", "/plots")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "authentication_required")

        headers = {
            "X-Actor-Id": "local-admin",
            "X-Idempotency-Key": "http-plot-key",
        }
        body = {
            "code": "OR-8101",
            "name": "HTTP 验证园",
            "locality": "测试地",
            "cultivar_focus": "测试品种",
            "steward": "测试组",
            "planting_year": 2010,
            "note": "",
        }
        first_status, first = self._request("PUT", "/plots", body, headers)
        second_status, second = self._request("PUT", "/plots", body, headers)
        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(first["id"], second["id"])

        audit_status, audit = self._request(
            "GET",
            f"/audit?resource_id={first['id']}",
            headers={"X-Actor-Id": "local-admin"},
        )
        self.assertEqual(audit_status, 200)
        self.assertEqual(audit["total"], 1)

        outbox_status, outbox = self._request(
            "GET",
            "/outbox?status=pending",
            headers={"X-Actor-Id": "local-admin"},
        )
        self.assertEqual(outbox_status, 200)
        self.assertEqual(outbox["total"], 1)
        publish_status, published = self._request(
            "PUT",
            f"/outbox/{outbox['items'][0]['event_id']}/publish",
            {},
            {"X-Actor-Id": "local-admin"},
        )
        self.assertEqual(publish_status, 200)
        self.assertEqual(published["status"], "published")

        job_status, job = self._request(
            "PUT",
            "/jobs",
            {
                "job_type": "integrity_scan",
                "payload": {},
                "max_attempts": 3,
                "priority": 100,
                "available_at": None,
            },
            {
                "X-Actor-Id": "local-admin",
                "X-Idempotency-Key": "http-job-key",
            },
        )
        self.assertEqual(job_status, 200)
        self.assertEqual(job["status"], "queued")

    def test_http_controlled_correction_and_basis_flags(self) -> None:
        headers = {"X-Actor-Id": "local-admin"}
        plot_status, plot = self._request(
            "PUT",
            "/plots",
            {
                "code": "OR-8201",
                "name": "勘误 HTTP 园",
                "locality": "测试地",
                "cultivar_focus": "测试品种",
                "steward": "测试组",
                "planting_year": 2010,
                "note": "",
            },
            headers,
        )
        self.assertEqual(plot_status, 200)
        _, tree_a = self._request(
            "PUT",
            "/trees",
            {
                "plot_id": plot["id"],
                "code": "OR-8201-T01",
                "cultivar": "秋梨",
                "rootstock": "杜梨",
                "planting_year": 2010,
                "status": "active",
                "note": "",
            },
            headers,
        )
        _, tree_b = self._request(
            "PUT",
            "/trees",
            {
                "plot_id": plot["id"],
                "code": "OR-8201-T02",
                "cultivar": "蜜梨",
                "rootstock": "杜梨",
                "planting_year": 2010,
                "status": "active",
                "note": "",
            },
            headers,
        )
        self._request(
            "PUT",
            f"/plots/{plot['id']}/confirm",
            {"revision": plot["revision"]},
            headers,
        )

        def completed_season(tree: dict[str, object], dates: list[str]) -> dict[str, object]:
            _, season = self._request(
                "PUT",
                "/observations",
                {
                    "tree_id": tree["id"],
                    "season": "2026",
                    "observer": "HTTP 测试员",
                    "note": "",
                },
                headers,
            )
            for stage, observed_on in zip(
                ["bud_burst", "full_bloom", "fruit_set", "harvest"],
                dates,
            ):
                _, season = self._request(
                    "PUT",
                    f"/observations/{season['id']}/stages",
                    {
                        "stage": stage,
                        "observed_on": observed_on,
                        "confidence": 4,
                        "note": "",
                        "revision": season["revision"],
                    },
                    headers,
                )
            _, season = self._request(
                "PUT",
                f"/observations/{season['id']}/complete",
                {"revision": season["revision"]},
                headers,
            )
            return season

        first = completed_season(
            tree_a,
            ["2026-03-10", "2026-04-01", "2026-04-18", "2026-09-02"],
        )
        second = completed_season(
            tree_b,
            ["2026-03-15", "2026-04-05", "2026-04-22", "2026-09-07"],
        )

        _, comparison = self._request(
            "PUT",
            "/comparisons",
            {
                "title": "HTTP 图谱",
                "left_observation_id": first["id"],
                "right_observation_id": second["id"],
            },
            headers,
        )
        _, brief = self._request(
            "PUT",
            f"/plots/{plot['id']}/briefs",
            {"title": "HTTP 简报"},
            headers,
        )

        propose_status, proposed = self._request(
            "PUT",
            "/corrections",
            {
                "observation_id": first["id"],
                "reason": "HTTP 台账复核后采收期顺延",
                "changes": [{"stage": "harvest", "observed_on": "2026-09-05"}],
            },
            headers,
        )
        self.assertEqual(propose_status, 200)
        self.assertEqual(proposed["status"], "proposed")

        list_status, listing = self._request(
            "GET",
            f"/corrections?observation_id={first['id']}",
            None,
            headers,
        )
        self.assertEqual(list_status, 200)
        self.assertEqual(listing["total"], 1)

        adopt_status, adopted = self._request(
            "PUT",
            f"/corrections/{proposed['id']}/adopt",
            {"revision": proposed["revision"]},
            headers,
        )
        self.assertEqual(adopt_status, 200)
        self.assertEqual(adopted["status"], "adopted")

        _, observation = self._request(
            "GET",
            f"/observations/{first['id']}",
            None,
            headers,
        )
        self.assertEqual(
            observation["entry_map"]["harvest"]["observed_on"],
            "2026-09-05",
        )
        self.assertEqual(
            next(
                item
                for item in observation["frozen_entries"]
                if item["stage"] == "harvest"
            )["observed_on"],
            "2026-09-02",
        )

        conflict_status, conflict = self._request(
            "PUT",
            "/comparisons",
            {
                "title": "隐式重算",
                "left_observation_id": first["id"],
                "right_observation_id": second["id"],
            },
            headers,
        )
        self.assertEqual(conflict_status, 409)
        self.assertEqual(conflict["error"]["code"], "comparison_basis_superseded")

        _, old_comparison = self._request(
            "GET",
            f"/comparisons/{comparison['id']}",
            None,
            headers,
        )
        self.assertEqual(old_comparison["basis_status"], "superseded")

        _, renewed = self._request(
            "PUT",
            "/comparisons",
            {
                "title": "HTTP 图谱新版",
                "left_observation_id": first["id"],
                "right_observation_id": second["id"],
                "supersedes_comparison_id": comparison["id"],
            },
            headers,
        )
        self.assertEqual(renewed["basis_status"], "current")

        _, old_brief = self._request(
            "GET",
            f"/briefs/{brief['id']}",
            None,
            headers,
        )
        self.assertEqual(old_brief["basis_status"], "superseded")

    def test_http_stage_replacement_correction(self) -> None:
        headers = {"X-Actor-Id": "local-admin"}
        _, plot = self._request(
            "PUT",
            "/plots",
            {
                "code": "OR-8301",
                "name": "阶段替换 HTTP 园",
                "locality": "测试地",
                "cultivar_focus": "测试品种",
                "steward": "测试组",
                "planting_year": 2010,
                "note": "",
            },
            headers,
        )
        _, left_tree = self._request(
            "PUT",
            "/trees",
            {
                "plot_id": plot["id"],
                "code": "OR-8301-T01",
                "cultivar": "秋梨",
                "rootstock": "杜梨",
                "planting_year": 2010,
                "status": "active",
                "note": "",
            },
            headers,
        )
        self._request(
            "PUT",
            f"/plots/{plot['id']}/confirm",
            {"revision": plot["revision"]},
            headers,
        )

        _, season = self._request(
            "PUT",
            "/observations",
            {
                "tree_id": left_tree["id"],
                "season": "2026",
                "observer": "HTTP 测试员",
                "note": "",
            },
            headers,
        )
        for stage, observed_on in (
            ("bud_burst", "2026-03-10"),
            ("full_bloom", "2026-04-01"),
            ("fruit_set", "2026-04-18"),
            ("fruit_growth", "2026-05-20"),
            ("harvest", "2026-09-02"),
        ):
            _, season = self._request(
                "PUT",
                f"/observations/{season['id']}/stages",
                {
                    "stage": stage,
                    "observed_on": observed_on,
                    "confidence": 4,
                    "note": "",
                    "revision": season["revision"],
                },
                headers,
            )
        _, season = self._request(
            "PUT",
            f"/observations/{season['id']}/complete",
            {"revision": season["revision"]},
            headers,
        )

        status, proposed = self._request(
            "PUT",
            "/corrections",
            {
                "observation_id": season["id"],
                "reason": "该观察实为04-12落瓣期，被误记为果实膨大期",
                "changes": [
                    {
                        "change_type": "replace",
                        "stage": "fruit_growth",
                        "correct_stage": "petal_fall",
                        "observed_on": "2026-04-12",
                        "confidence": 4,
                        "note": "",
                    }
                ],
            },
            headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(proposed["changes"][0]["change_type"], "replace")

        _, adopted = self._request(
            "PUT",
            f"/corrections/{proposed['id']}/adopt",
            {"revision": proposed["revision"]},
            headers,
        )
        self.assertEqual(adopted["status"], "adopted")

        _, detail = self._request(
            "GET",
            f"/observations/{season['id']}",
            None,
            headers,
        )
        stages = [entry["stage"] for entry in detail["entries"]]
        self.assertNotIn("fruit_growth", stages)
        self.assertIn("petal_fall", stages)
        self.assertEqual(
            detail["entry_map"]["petal_fall"]["observed_on"],
            "2026-04-12",
        )
        self.assertIn(
            "fruit_growth",
            [entry["stage"] for entry in detail["frozen_entries"]],
        )

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
            headers={
                "Content-Type": "application/json",
                **(headers or {}),
            },
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
