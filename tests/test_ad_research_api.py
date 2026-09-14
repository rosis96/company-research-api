from __future__ import annotations

import time

from fastapi.testclient import TestClient

from ad_research.api import _output_column_names
from ad_research.main import create_app
from ad_research.settings import AdResearchSettings


def test_health_paths_support_both_railway_configs(tmp_path) -> None:
    settings = AdResearchSettings(database_url=f"sqlite:///{tmp_path / 'api.db'}")
    with TestClient(create_app(settings)) as client:
        for path in ("/api/health", "/v1/health"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.json()["service"] == "ad-research"
            assert response.json()["max_batch_size"] == settings.max_batch_size
            assert response.json()["max_upload_bytes"] == settings.max_upload_bytes
            assert response.json()["crawl_concurrency"] == settings.concurrency


def test_default_batch_capacity_supports_more_than_ten_thousand_rows() -> None:
    settings = AdResearchSettings(_env_file=None)
    assert settings.max_batch_size >= 10_000
    assert settings.max_upload_bytes >= 50_000_000


def test_batch_progress_results_and_csv_keep_failures_inconclusive(tmp_path) -> None:
    settings = AdResearchSettings(database_url=f"sqlite:///{tmp_path / 'api.db'}", concurrency=2)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/jobs",
            json={"domains": ["http://127.0.0.1", "http://0x7f000001"]},
        )
        assert response.status_code == 202
        job_id = response.json()["job_id"]

        for _ in range(50):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in {"COMPLETED", "INCONCLUSIVE"}:
                break
            time.sleep(0.02)

        assert job["status"] == "COMPLETED"
        assert job["completed_domains"] == 2
        assert job["failed_domains"] == 2
        assert len(job["result_ids"]) == 2

        stored = client.get(f"/api/results/{job['result_ids'][0]}").json()
        assert stored["result"]["status"] == "CRAWL_FAILED"
        assert stored["result"]["advertising_infrastructure_confidence"] == "INCONCLUSIVE"

        exported = client.get(f"/api/jobs/{job_id}/csv")
        assert exported.status_code == 200
        assert "Ad Infrastructure Result,Infrastructure Confidence" in exported.text
        assert "Primary Advertising Confidence" in exported.text
        assert "Combined Activity Confidence" in exported.text
        assert "Evidence 1,Evidence 2,Evidence 3" in exported.text
        assert "Detected Ad Platforms,Runtime Ad Platforms,Pages Crawled" in exported.text
        assert "Error / Notes" in exported.text
        assert "INCONCLUSIVE" in exported.text


def test_ui_is_served(tmp_path) -> None:
    settings = AdResearchSettings(database_url=f"sqlite:///{tmp_path / 'api.db'}")
    with TestClient(create_app(settings)) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Paid advertising infrastructure research" in response.text
        assert "localStorage.setItem(JOB_KEY,id)" in response.text
        assert "restoreJob();" in response.text


def test_recent_jobs_can_be_reopened_after_refresh(tmp_path) -> None:
    settings = AdResearchSettings(database_url=f"sqlite:///{tmp_path / 'api.db'}")
    with TestClient(create_app(settings)) as client:
        created = client.post("/api/jobs", json={"domains": ["http://127.0.0.1"]})
        job_id = created.json()["job_id"]
        recent = client.get("/api/jobs", params={"limit": 1})

        assert recent.status_code == 200
        assert recent.json()[0]["job_id"] == job_id


def test_output_columns_never_overwrite_existing_lead_columns() -> None:
    names = _output_column_names(["Email", "Ad Infrastructure Result", "Evidence 1"])
    assert names["classification"] == "Ad Infrastructure Result (Ad Research)"
    assert names["evidence_1"] == "Evidence 1 (Ad Research)"
    assert len(set(names.values())) == len(names)


def test_external_activity_evidence_is_a_separate_auditable_layer(tmp_path) -> None:
    settings = AdResearchSettings(database_url=f"sqlite:///{tmp_path / 'api.db'}")
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/activity-evidence",
            json={
                "domain": "example.com",
                "source": "meta_ad_library",
                "status": "ACTIVE",
                "active_ads_count": 7,
                "landing_page_urls": ["https://offer.example.com/demo"],
                "source_url": "https://www.facebook.com/ads/library/",
            },
        )
        assert response.status_code == 201
        assert response.json()["domain_key"] == "example.com"

        research = client.get("/api/research", params={"domain": "example.com"}).json()
        assert research["external_activity"]["confidence"] == "CONFIRMED_CURRENT"
        assert research["external_activity"]["active_ads_count"] == 7
        assert research["combined_activity_confidence"] == "MEDIUM"


def test_csv_upload_preserves_original_rows_order_and_duplicate_websites(tmp_path) -> None:
    settings = AdResearchSettings(database_url=f"sqlite:///{tmp_path / 'api.db'}", concurrency=2)
    content = (
        "First Name,Email,Website\n"
        "Ada,ada@example.test,http://127.0.0.1\n"
        "Grace,grace@example.test,http://127.0.0.1\n"
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/jobs/upload",
            files={"file": ("apollo.csv", content, "text/csv")},
        )
        assert response.status_code == 202
        job_id = response.json()["job_id"]

        for _ in range(50):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] == "COMPLETED":
                break
            time.sleep(0.02)

        page = client.get(f"/api/jobs/{job_id}/rows").json()
        assert page["source_columns"] == ["First Name", "Email", "Website"]
        assert [row["website"] for row in page["rows"]] == [
            "http://127.0.0.1",
            "http://127.0.0.1",
        ]
        assert [row["original_data"]["Email"] for row in page["rows"]] == [
            "ada@example.test",
            "grace@example.test",
        ]
        assert all(row["result_id"] for row in page["rows"])
        assert len({row["result_id"] for row in page["rows"]}) == 2

        exported = client.get(f"/api/jobs/{job_id}/csv").text
        assert "Ada,ada@example.test,http://127.0.0.1,INCONCLUSIVE" in exported
        assert "Grace,grace@example.test,http://127.0.0.1,INCONCLUSIVE" in exported
