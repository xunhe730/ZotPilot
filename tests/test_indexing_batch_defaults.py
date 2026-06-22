from unittest.mock import MagicMock, patch


def test_index_library_defaults_to_small_batches():
    from zotpilot.tools.indexing import index_library

    index_result = {
        "results": [],
        "indexed": 0,
        "failed": 0,
        "empty": 0,
        "skipped": 0,
        "already_indexed": 0,
        "has_more": False,
    }
    config = MagicMock()
    config.validate.return_value = []
    config.max_pages = 40
    config.vision_enabled = True

    captured = {}

    def fake_index_all_libraries(cfg, **kwargs):
        captured.update(kwargs)
        return index_result

    with (
        patch("zotpilot.tools.indexing._get_config", return_value=config),
        patch("zotpilot.tools.indexing._get_store") as mock_store,
        patch("zotpilot.indexer.index_all_libraries", fake_index_all_libraries),
        patch("dataclasses.replace", side_effect=lambda obj, **kwargs: obj),
    ):
        mock_store.return_value.clear_query_cache = MagicMock()
        index_library()

    assert captured["batch_size"] == 2
