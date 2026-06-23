import json
import os
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
                fail_on_write_blocked=False,
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


def test_estimate_formula_backfill_json_redirects_third_party_stdout_to_stderr(capfd):
    from zotpilot.cli import cmd_estimate_formula_backfill

    config = MagicMock()
    config.validate.return_value = []
    indexer = MagicMock()

    def noisy_estimate(**_kwargs):
        print("MinerU cache warning")
        os.write(1, b"MuPDF fd warning\n")
        return {
            "provider": "simpletex",
            "candidate_provider": "mineru_cache",
            "processed": 0,
            "candidate_count": 0,
            "summary": {"warnings": [], "next_action": "No matches."},
        }

    indexer.estimate_formula_backfill.side_effect = noisy_estimate

    with (
        patch("zotpilot.cli.resolve_runtime_config", return_value=config),
        patch("zotpilot.indexer.Indexer.for_formula_estimate", return_value=indexer),
    ):
        rc = cmd_estimate_formula_backfill(
            SimpleNamespace(
                config="config.json",
                item_key=None,
                item_keys=None,
                limit=None,
                resume_after=None,
                daily_call_budget=None,
                preview_candidates=0,
                preview_all_candidates=False,
                preview_chars=160,
                pdf_fallback_max_pages=None,
                cache_pdf_number_enrichment=False,
                page_min=None,
                page_max=None,
                sample_size=None,
                sample_seed=0,
                json=True,
            )
        )

    captured = capfd.readouterr()
    assert rc == 0
    assert json.loads(captured.out)["provider"] == "simpletex"
    assert captured.out.lstrip().startswith("{")
    assert "MinerU cache warning" in captured.err
    assert "MuPDF fd warning" in captured.err


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
        "write_blocked": True,
        "write_ready": False,
        "write_block_reasons": ["candidate_quality_review_required"],
        "next_action": "Review candidate-stage formula quality warnings before rerunning.",
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
                fail_on_write_blocked=False,
                json=False,
            )
        )

    out = capsys.readouterr().out
    assert rc == 0
    assert "Formula backfill complete:" in out
    assert "Write status:            blocked" in out
    assert "Next:                    Review candidate-stage formula quality warnings before rerunning." in out
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


def test_index_formulas_cli_shows_review_required_writes(tmp_path, capsys):
    from zotpilot.cli import cmd_index_formulas

    config = MagicMock()
    config.validate.return_value = []
    config.formula_ocr_enabled = True
    config.chroma_db_path = tmp_path / "chroma"
    indexer = MagicMock()
    indexer.index_formulas.return_value = {
        "provider": "simpletex",
        "processed": 1,
        "formulas_indexed": 1,
        "provider_calls_used": 1,
        "external_calls_used": 1,
        "write_blocked": False,
        "write_ready": True,
        "write_review_required": True,
        "write_block_reasons": [],
        "next_action": "Review 1 low-confidence formula row(s) before scaling up.",
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
                item_key="DOC1",
                item_keys=None,
                limit=None,
                no_refresh_existing=False,
                daily_call_budget=2,
                resume_after=None,
                no_stop_on_quota=False,
                status_jsonl=None,
                low_confidence_threshold=0.7,
                include_high_density=False,
                allow_candidate_quality_warnings=False,
                pdf_fallback_max_pages=None,
                cache_pdf_number_enrichment=False,
                page_min=None,
                page_max=None,
                sample_size=None,
                sample_seed=0,
                fail_on_write_blocked=True,
                fail_on_review_required=False,
                json=False,
            )
        )

    out = capsys.readouterr().out
    assert rc == 0
    assert "Write status:            ready" in out
    assert "Review required:         yes" in out
    assert "Next:                    Review 1 low-confidence formula row(s) before scaling up." in out


def test_index_formulas_cli_can_fail_when_review_required(tmp_path, capsys):
    from zotpilot.cli import cmd_index_formulas

    config = MagicMock()
    config.validate.return_value = []
    config.formula_ocr_enabled = True
    config.chroma_db_path = tmp_path / "chroma"
    indexer = MagicMock()
    indexer.index_formulas.return_value = {
        "provider": "simpletex",
        "processed": 1,
        "formulas_indexed": 1,
        "provider_calls_used": 1,
        "external_calls_used": 1,
        "write_blocked": False,
        "write_ready": True,
        "write_review_required": True,
        "write_block_reasons": [],
        "next_action": "Review 1 low-confidence formula row(s) before scaling up.",
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
                item_key="DOC1",
                item_keys=None,
                limit=None,
                no_refresh_existing=False,
                daily_call_budget=2,
                resume_after=None,
                no_stop_on_quota=False,
                status_jsonl=None,
                low_confidence_threshold=0.7,
                include_high_density=False,
                allow_candidate_quality_warnings=False,
                pdf_fallback_max_pages=None,
                cache_pdf_number_enrichment=False,
                page_min=None,
                page_max=None,
                sample_size=None,
                sample_seed=0,
                fail_on_write_blocked=True,
                fail_on_review_required=True,
                json=False,
            )
        )

    out = capsys.readouterr().out
    assert rc == 3
    assert "Write status:            ready" in out
    assert "Review required:         yes" in out
    assert "Next:                    Review 1 low-confidence formula row(s) before scaling up." in out


def test_index_formulas_cli_can_fail_when_write_blocked(tmp_path, capsys):
    from zotpilot.cli import cmd_index_formulas

    config = MagicMock()
    config.validate.return_value = []
    config.formula_ocr_enabled = True
    config.chroma_db_path = tmp_path / "chroma"
    indexer = MagicMock()
    indexer.index_formulas.return_value = {
        "provider": "simpletex",
        "processed": 1,
        "formulas_indexed": 0,
        "provider_calls_used": 0,
        "external_calls_used": 0,
        "write_blocked": True,
        "write_ready": False,
        "write_block_reasons": ["candidate_quality_review_required"],
        "next_action": "Review candidate-stage formula quality warnings before rerunning.",
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
                item_keys=["DOC1"],
                limit=None,
                no_refresh_existing=False,
                daily_call_budget=2,
                resume_after=None,
                no_stop_on_quota=False,
                status_jsonl=None,
                low_confidence_threshold=None,
                include_high_density=False,
                allow_candidate_quality_warnings=False,
                pdf_fallback_max_pages=None,
                cache_pdf_number_enrichment=False,
                page_min=None,
                page_max=None,
                sample_size=None,
                sample_seed=0,
                fail_on_write_blocked=True,
                fail_on_review_required=True,
                json=False,
            )
        )

    out = capsys.readouterr().out
    assert rc == 2
    assert "Write status:            blocked" in out
    assert "Next:                    Review candidate-stage formula quality warnings before rerunning." in out


def test_index_formulas_cli_prioritizes_write_blocked_exit_code(tmp_path, capsys):
    from zotpilot.cli import cmd_index_formulas

    config = MagicMock()
    config.validate.return_value = []
    config.formula_ocr_enabled = True
    config.chroma_db_path = tmp_path / "chroma"
    indexer = MagicMock()
    indexer.index_formulas.return_value = {
        "provider": "simpletex",
        "processed": 1,
        "formulas_indexed": 0,
        "provider_calls_used": 0,
        "external_calls_used": 0,
        "write_blocked": True,
        "write_ready": False,
        "write_review_required": True,
        "write_block_reasons": ["candidate_quality_review_required"],
        "next_action": "Review candidate-stage formula quality warnings before rerunning.",
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
                item_keys=["DOC1"],
                limit=None,
                no_refresh_existing=False,
                daily_call_budget=2,
                resume_after=None,
                no_stop_on_quota=False,
                status_jsonl=None,
                low_confidence_threshold=None,
                include_high_density=False,
                allow_candidate_quality_warnings=False,
                pdf_fallback_max_pages=None,
                cache_pdf_number_enrichment=False,
                page_min=None,
                page_max=None,
                sample_size=None,
                sample_seed=0,
                fail_on_write_blocked=True,
                fail_on_review_required=True,
                json=False,
            )
        )

    out = capsys.readouterr().out
    assert rc == 2
    assert "Write status:            blocked" in out
    assert "Review required:         yes" in out


def test_index_formulas_cli_json_can_fail_when_write_blocked(tmp_path, capsys):
    from zotpilot.cli import cmd_index_formulas

    config = MagicMock()
    config.validate.return_value = []
    config.formula_ocr_enabled = True
    config.chroma_db_path = tmp_path / "chroma"
    indexer = MagicMock()
    indexer.index_formulas.return_value = {
        "provider": "simpletex",
        "processed": 1,
        "formulas_indexed": 0,
        "write_blocked": True,
        "write_ready": False,
        "write_block_reasons": ["candidate_quality_review_required"],
        "next_action": "Review candidate-stage formula quality warnings before rerunning.",
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
                item_keys=["DOC1"],
                limit=None,
                no_refresh_existing=False,
                daily_call_budget=2,
                resume_after=None,
                no_stop_on_quota=False,
                status_jsonl=None,
                low_confidence_threshold=None,
                include_high_density=False,
                allow_candidate_quality_warnings=False,
                pdf_fallback_max_pages=None,
                cache_pdf_number_enrichment=False,
                page_min=None,
                page_max=None,
                sample_size=None,
                sample_seed=0,
                fail_on_write_blocked=True,
                fail_on_review_required=True,
                json=True,
            )
        )

    out = capsys.readouterr().out
    assert rc == 2
    assert '"write_blocked": true' in out
    assert '"write_block_reasons": [' in out


def test_index_formulas_cli_json_can_fail_when_review_required(tmp_path, capsys):
    from zotpilot.cli import cmd_index_formulas

    config = MagicMock()
    config.validate.return_value = []
    config.formula_ocr_enabled = True
    config.chroma_db_path = tmp_path / "chroma"
    indexer = MagicMock()
    indexer.index_formulas.return_value = {
        "provider": "simpletex",
        "processed": 1,
        "formulas_indexed": 1,
        "write_blocked": False,
        "write_ready": True,
        "write_review_required": True,
        "write_block_reasons": [],
        "next_action": "Review 1 low-confidence formula row(s) before scaling up.",
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
                item_keys=["DOC1"],
                limit=None,
                no_refresh_existing=False,
                daily_call_budget=2,
                resume_after=None,
                no_stop_on_quota=False,
                status_jsonl=None,
                low_confidence_threshold=None,
                include_high_density=False,
                allow_candidate_quality_warnings=False,
                pdf_fallback_max_pages=None,
                cache_pdf_number_enrichment=False,
                page_min=None,
                page_max=None,
                sample_size=None,
                sample_seed=0,
                fail_on_write_blocked=True,
                fail_on_review_required=True,
                json=True,
            )
        )

    out = capsys.readouterr().out
    assert rc == 3
    assert '"write_review_required": true' in out
    assert '"write_blocked": false' in out


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
