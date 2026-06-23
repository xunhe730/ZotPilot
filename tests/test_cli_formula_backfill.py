from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def test_estimate_formula_backfill_cli_ignores_simpletex_auth_for_read_only_estimate(capsys):
    from zotpilot.cli import cmd_estimate_formula_backfill

    config = MagicMock()
    config.validate.return_value = ["SimpleTex formula OCR requires formula_ocr_simpletex_token"]
    indexer = MagicMock()
    indexer.estimate_formula_backfill.return_value = {
        "provider": "simpletex",
        "candidate_provider": "mineru_cache",
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
        patch("zotpilot.indexer.Indexer.for_formula_estimate", return_value=indexer),
    ):
        rc = cmd_estimate_formula_backfill(
            SimpleNamespace(
                config="config.json",
                item_key="DOC1",
                item_keys=None,
                limit=10,
                resume_after="DOC0",
                daily_call_budget=2,
                preview_candidates=1,
                preview_all_candidates=False,
                preview_chars=160,
                pdf_fallback_max_pages=0,
                cache_pdf_number_enrichment=True,
                page_min=None,
                page_max=None,
                sample_size=None,
                sample_seed=0,
                json=False,
            )
        )

    out = capsys.readouterr().out
    assert rc == 0
    assert "Formula backfill estimate:" in out
    assert "Candidate provider:        mineru_cache" in out
    assert "Estimated runs:            3" in out
    assert config.formula_candidate_cache_pdf_number_enrichment is True
    indexer.estimate_formula_backfill.assert_called_once_with(
        item_key="DOC1",
        item_keys=None,
        limit=10,
        resume_after="DOC0",
        daily_call_budget=2,
        candidate_preview_limit=1,
        candidate_preview_chars=160,
        pdf_fallback_max_pages=0,
        page_min=None,
        page_max=None,
        sample_size=None,
        sample_seed=0,
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
                include_high_density=True,
                allow_candidate_quality_warnings=True,
                pdf_fallback_max_pages=None,
                cache_pdf_number_enrichment=True,
                page_min=None,
                page_max=None,
                sample_size=None,
                sample_seed=0,
                json=False,
            )
        )

    out = capsys.readouterr().out
    assert rc == 0
    assert "Formula backfill complete:" in out
    assert "Resume after:            DOC1" in out
    assert "Next item:               DOC2" in out
    assert config.formula_candidate_cache_pdf_number_enrichment is True
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
        include_high_density=True,
        allow_candidate_quality_warnings=True,
        pdf_fallback_max_pages=None,
        page_min=None,
        page_max=None,
    )


def test_index_formulas_cli_dry_run_uses_estimate_without_lease(tmp_path, capsys):
    from zotpilot.cli import cmd_index_formulas

    config = MagicMock()
    config.validate.return_value = ["SimpleTex formula OCR requires formula_ocr_simpletex_token"]
    config.formula_ocr_enabled = True
    config.chroma_db_path = tmp_path / "chroma"
    indexer = MagicMock()
    indexer.estimate_formula_backfill.return_value = {
        "provider": "simpletex",
        "candidate_provider": "mineru_cache",
        "processed": 1,
        "candidate_count": 1,
        "average_candidates_per_paper": 1.0,
        "estimated_provider_calls": 0,
        "estimated_external_calls": 0,
        "estimated_min_duration": "0s",
        "daily_call_budget": 1800,
        "estimated_runs": 1,
        "data_egress": True,
        "summary": {"next_action": "Review candidates.", "warnings": []},
        "results": [
            {
                "item_key": "DOC1",
                "candidate_preview": [
                    {
                        "page_num": 1,
                        "source": "mineru_markdown",
                        "confidence": 0.9,
                        "equation_number": "(1)",
                        "has_latex": True,
                        "needs_ocr": False,
                        "latex_preview": r"E = mc^2",
                    }
                ],
            }
        ],
    }

    with (
        patch("zotpilot.cli.resolve_runtime_config", return_value=config),
        patch("zotpilot.index_authority.acquire_lease") as acquire_lease,
        patch("zotpilot.indexer.Indexer.for_formula_estimate", return_value=indexer),
    ):
        rc = cmd_index_formulas(
            SimpleNamespace(
                config="config.json",
                item_key="DOC1",
                item_keys=None,
                limit=1,
                dry_run=True,
                preview_candidates=1,
                preview_all_candidates=False,
                preview_chars=160,
                daily_call_budget=1800,
                resume_after=None,
                pdf_fallback_max_pages=None,
                cache_pdf_number_enrichment=True,
                page_min=None,
                page_max=None,
                sample_size=None,
                sample_seed=0,
                json=False,
            )
        )

    out = capsys.readouterr().out
    assert rc == 0
    assert "[dry-run] No formula chunks were written." in out
    assert "Candidate provider:        mineru_cache" in out
    assert "p1 (1) mineru_markdown cached" in out
    assert "E = mc^2" in out
    assert config.formula_candidate_cache_pdf_number_enrichment is True
    acquire_lease.assert_not_called()
    indexer.index_formulas.assert_not_called()
    indexer.estimate_formula_backfill.assert_called_once_with(
        item_key="DOC1",
        item_keys=None,
        limit=1,
        resume_after=None,
        daily_call_budget=1800,
        candidate_preview_limit=1,
        candidate_preview_chars=160,
        pdf_fallback_max_pages=None,
        page_min=None,
        page_max=None,
        sample_size=None,
        sample_seed=0,
    )


def test_estimate_formula_backfill_cli_can_export_all_candidate_preview(capsys):
    from zotpilot.cli import cmd_estimate_formula_backfill

    config = MagicMock()
    config.validate.return_value = []
    indexer = MagicMock()
    indexer.estimate_formula_backfill.return_value = {
        "provider": "simpletex",
        "candidate_provider": "auto",
        "processed": 1,
        "candidate_count": 2,
        "average_candidates_per_paper": 2.0,
        "estimated_provider_calls": 0,
        "estimated_external_calls": 0,
        "estimated_min_duration": "0s",
        "daily_call_budget": 1800,
        "estimated_runs": 1,
        "data_egress": True,
        "summary": {"next_action": "Review candidates.", "warnings": []},
        "results": [{"item_key": "DOC1", "candidate_preview": []}],
    }

    with (
        patch("zotpilot.cli.resolve_runtime_config", return_value=config),
        patch("zotpilot.indexer.Indexer.for_formula_estimate", return_value=indexer),
    ):
        rc = cmd_estimate_formula_backfill(
            SimpleNamespace(
                config="config.json",
                item_key="DOC1",
                item_keys=None,
                limit=None,
                resume_after=None,
                daily_call_budget=1800,
                preview_candidates=0,
                preview_all_candidates=True,
                preview_chars=0,
                pdf_fallback_max_pages=0,
                page_min=None,
                page_max=None,
                sample_size=None,
                sample_seed=0,
                json=True,
            )
        )

    assert rc == 0
    indexer.estimate_formula_backfill.assert_called_once_with(
        item_key="DOC1",
        item_keys=None,
        limit=None,
        resume_after=None,
        daily_call_budget=1800,
        candidate_preview_limit=-1,
        candidate_preview_chars=0,
        pdf_fallback_max_pages=0,
        page_min=None,
        page_max=None,
        sample_size=None,
        sample_seed=0,
    )
