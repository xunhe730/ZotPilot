from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def test_estimate_formula_backfill_cli_ignores_simpletex_auth_for_read_only_estimate(capsys):
    from zotpilot.cli import cmd_estimate_formula_backfill

    config = MagicMock()
    config.validate.return_value = ["SimpleTex formula OCR requires formula_ocr_simpletex_token"]
    indexer = MagicMock()
    indexer.estimate_formula_backfill.return_value = {
        "provider": "simpletex",
        "processed": 2,
        "candidate_count": 5,
        "average_candidates_per_paper": 2.5,
        "estimated_provider_calls": 5,
        "estimated_external_calls": 5,
        "estimated_min_duration": "2.5s",
        "daily_call_budget": 2,
        "estimated_runs": 3,
        "data_egress": True,
        "summary": {
            "next_action": "Run index_formulas with the same daily_call_budget.",
            "warnings": ["SimpleTex will send formula crops to the configured HTTPS endpoint."],
        },
    }

    with (
        patch("zotpilot.cli.resolve_runtime_config", return_value=config),
        patch("zotpilot.indexer.Indexer", return_value=indexer),
    ):
        rc = cmd_estimate_formula_backfill(
            SimpleNamespace(
                config="config.json",
                item_key="DOC1",
                item_keys=None,
                limit=10,
                resume_after="DOC0",
                daily_call_budget=2,
                json=False,
            )
        )

    out = capsys.readouterr().out
    assert rc == 0
    assert "Formula backfill estimate:" in out
    assert "Estimated runs:            3" in out
    indexer.estimate_formula_backfill.assert_called_once_with(
        item_key="DOC1",
        item_keys=None,
        limit=10,
        resume_after="DOC0",
        daily_call_budget=2,
    )


def test_index_formulas_cli_passes_budget_resume_and_status_jsonl(tmp_path, capsys):
    from zotpilot.cli import cmd_index_formulas

    config = MagicMock()
    config.validate.return_value = []
    config.formula_ocr_enabled = True
    config.chroma_db_path = tmp_path / "chroma"
    indexer = MagicMock()
    indexer.index_formulas.return_value = {
        "provider": "simpletex",
        "processed": 1,
        "formulas_indexed": 2,
        "provider_calls_used": 2,
        "external_calls_used": 2,
        "daily_call_budget": 2,
        "daily_call_budget_remaining": 0,
        "stopped_reason": "daily_call_budget",
        "resume_cursor": "DOC1",
        "next_item_key": "DOC2",
        "state_path": str(tmp_path / "formula_backfill_state.jsonl"),
        "low_confidence_review_count": 1,
        "results": [],
    }

    with (
        patch("zotpilot.cli.resolve_runtime_config", return_value=config),
        patch("zotpilot.index_authority.acquire_lease"),
        patch("zotpilot.index_authority.release_lease"),
        patch("zotpilot.indexer.Indexer", return_value=indexer),
    ):
        rc = cmd_index_formulas(
            SimpleNamespace(
                config="config.json",
                item_key=None,
                item_keys=["DOC1", "DOC2"],
                limit=2,
                no_refresh_existing=False,
                daily_call_budget=2,
                resume_after="DOC0",
                no_stop_on_quota=False,
                status_jsonl="",
                low_confidence_threshold=0.7,
                json=False,
            )
        )

    out = capsys.readouterr().out
    assert rc == 0
    assert "Formula backfill complete:" in out
    assert "Resume after:            DOC1" in out
    assert "Next item:               DOC2" in out
    indexer.index_formulas.assert_called_once_with(
        item_key=None,
        item_keys=["DOC1", "DOC2"],
        limit=2,
        refresh_existing=True,
        daily_call_budget=2,
        resume_after="DOC0",
        stop_on_quota=True,
        status_jsonl="",
        low_confidence_threshold=0.7,
    )
