from __future__ import annotations

from game_assets_api.m6_acceptance import (
    M6_CHECKS,
    run_m6_acceptance,
    run_provider_fault_matrix,
    run_recovery_drills,
)


def test_m6_provider_fault_matrix_uses_openai_compatible_http_contract() -> None:
    checks = {check.check_id: check for check in run_provider_fault_matrix()}
    assert set(checks) == {
        "provider.success",
        "provider.rate_limit",
        "provider.server_error",
        "provider.auth",
        "provider.quota",
        "provider.content_policy",
        "provider.bad_response",
    }
    assert all(check.status == "passed" for check in checks.values())
    assert checks["provider.success"].details["paths"] == [
        "/v1/chat/completions",
        "/v1/images/edits",
        "/v1/images/generations",
        "/v1/models",
    ]
    assert checks["provider.content_policy"].details["requests"] == 1
    assert checks["provider.auth"].details["retryable"] is False


def test_m6_recovery_drills_restore_and_rebuild_from_project_history() -> None:
    checks = {check.check_id: check for check in run_recovery_drills()}
    assert {
        "recovery.disk_full",
        "recovery.delivery_interruption",
        "recovery.service_crash",
        "recovery.worker_crash",
        "recovery.sqlite_rebuild",
        "recovery.project_scan",
    } <= set(checks)
    assert all(checks[key].status == "passed" for key in checks)
    assert checks["recovery.disk_full"].details["previous_lock_preserved"] is True
    assert checks["recovery.delivery_interruption"].details["delivery_status"] == "failed"
    assert checks["recovery.service_crash"].details["attempt_count"] == 1
    assert checks["recovery.worker_crash"].details["attempt_count"] == 1
    assert checks["recovery.sqlite_rebuild"].details["deliveries_rebuilt"] >= 2


def test_m6_report_is_green_without_secrets_and_marks_live_check_skipped() -> None:
    report = run_m6_acceptance()
    assert report["milestone"] == "M6"
    assert report["summary"]["ok"] is True
    assert report["summary"]["failed"] == 0
    assert report["provider"]["api_key_recorded"] is False
    assert {item["id"] for item in report["checks"]} == set(M6_CHECKS)
    live = next(item for item in report["checks"] if item["id"] == "provider.live_contract")
    assert live["status"] == "skipped"
