"""CLI entry point for ZotPilot."""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

from .config import Config


def cmd_setup(args):
    """Interactive setup wizard."""
    from .zotero_detector import detect_zotero_data_dir

    print("ZotPilot Setup Wizard")
    print("=" * 40)

    # Step 1: Detect Zotero data directory
    print("\n[1/5] Detecting Zotero data directory...")
    detected = detect_zotero_data_dir()

    if detected:
        print(f"  Found: {detected}")
        response = input(f"  Use this path? [Y/n] ").strip().lower()
        if response in ("n", "no"):
            zotero_dir = input("  Enter Zotero data directory: ").strip()
        else:
            zotero_dir = str(detected)
    else:
        print("  Could not auto-detect Zotero data directory.")
        zotero_dir = input("  Enter Zotero data directory path: ").strip()

    zotero_path = Path(zotero_dir).expanduser()
    if not (zotero_path / "zotero.sqlite").exists():
        print(f"  WARNING: zotero.sqlite not found at {zotero_path}")
        if input("  Continue anyway? [y/N] ").strip().lower() not in ("y", "yes"):
            return 1

    # Step 2: Choose embedding provider
    print("\n[2/5] Choose embedding provider:")
    print("  1. Gemini (recommended, requires API key)")
    print("  2. Local (all-MiniLM-L6-v2, no API key needed)")
    choice = input("  Choice [1/2]: ").strip()
    embedding_provider = "local" if choice == "2" else "gemini"

    # Step 3: Configure API key if Gemini
    gemini_api_key = None
    if embedding_provider == "gemini":
        print("\n[3/5] Gemini API key:")
        import os
        existing_key = os.environ.get("GEMINI_API_KEY")
        if existing_key:
            print(f"  Found GEMINI_API_KEY in environment ({existing_key[:8]}...)")
            if input("  Use this key? [Y/n] ").strip().lower() not in ("n", "no"):
                gemini_api_key = existing_key
        if not gemini_api_key:
            gemini_api_key = input("  Enter Gemini API key: ").strip()
            if not gemini_api_key:
                print("  WARNING: No API key provided. Set GEMINI_API_KEY env var later.")
    else:
        print("\n[3/5] Skipping API key (local embeddings selected)")

    # Step 4: Check for existing deep-zotero config
    print("\n[4/5] Checking for existing configuration...")
    old_config = Path("~/.config/deep-zotero/config.json").expanduser()
    old_chroma = Path("~/.local/share/deep-zotero/chroma").expanduser()
    chroma_db_path = Path("~/.local/share/zotpilot/chroma").expanduser()

    if old_config.exists():
        print(f"  Found existing deep-zotero config: {old_config}")
        if input("  Migrate settings from deep-zotero? [Y/n] ").strip().lower() not in ("n", "no"):
            with open(old_config) as f:
                old_data = json.load(f)
            print(f"  Migrated {len(old_data)} settings from deep-zotero")
            # If old chroma index exists, reuse it
            if old_chroma.exists():
                print(f"  Found existing ChromaDB index: {old_chroma}")
                if input("  Reuse existing index? [Y/n] ").strip().lower() not in ("n", "no"):
                    chroma_db_path = old_chroma

    # Step 5: Write config
    print("\n[5/5] Writing configuration...")
    config_path = Path("~/.config/zotpilot/config.json").expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    config_data = {
        "zotero_data_dir": str(zotero_path),
        "chroma_db_path": str(chroma_db_path),
        "embedding_provider": embedding_provider,
    }
    if gemini_api_key:
        config_data["gemini_api_key"] = gemini_api_key

    with open(config_path, "w") as f:
        json.dump(config_data, f, indent=2)
    print(f"  Config written to: {config_path}")

    # Detect MCP client and offer to configure
    print("\n" + "=" * 40)
    print("Setup complete!")
    print()
    print("To start the MCP server, add to your client config:")
    print()
    print("  Claude Code (~/.claude/settings.json):")
    print('    "mcpServers": {')
    print('      "zotpilot": {')
    print('        "command": "uv",')
    print('        "args": ["tool", "run", "zotpilot"]')
    print("      }")
    print("    }")
    print()
    print("  Or run directly: zotpilot index")

    return 0


def cmd_index(args):
    """Index Zotero library."""
    from .indexer import Indexer

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    config = Config.load(args.config)
    errors = config.validate()
    if errors:
        for e in errors:
            print(f"Config error: {e}", file=sys.stderr)
        return 1

    if args.no_vision:
        from dataclasses import replace
        config = replace(config, vision_enabled=False)

    indexer = Indexer(config)
    result = indexer.index_all(
        force_reindex=args.force,
        limit=args.limit,
        item_key=args.item_key,
        title_pattern=args.title,
    )

    print(f"\nIndexing complete:")
    print(f"  Indexed:         {result['indexed']}")
    print(f"  Already indexed: {result['already_indexed']}")
    print(f"  Skipped (empty): {result['skipped']}")
    print(f"  Failed:          {result['failed']}")
    print(f"  Empty:           {result['empty']}")

    if result.get("quality_distribution"):
        dist = result["quality_distribution"]
        print(f"  Quality: A={dist.get('A',0)} B={dist.get('B',0)} "
              f"C={dist.get('C',0)} D={dist.get('D',0)} F={dist.get('F',0)}")

    if result.get("extraction_stats"):
        stats = result["extraction_stats"]
        print(f"  Pages: {stats.get('total_pages',0)} total, "
              f"{stats.get('text_pages',0)} text, "
              f"{stats.get('ocr_pages',0)} OCR, "
              f"{stats.get('empty_pages',0)} empty")

    failures = [r for r in result["results"] if r.status == "failed"]
    if failures:
        print(f"\nFailures:")
        for f in failures:
            print(f"  {f.item_key}: {f.reason}")

    if result["indexed"] > 0:
        logging.getLogger(__name__).info(
            "Waiting 60s for ChromaDB compaction to persist HNSW index to disk..."
        )
        time.sleep(60)

    return 1 if result["failed"] > 0 and result["indexed"] == 0 else 0


def cmd_status(args):
    """Show configuration and index stats."""
    config = Config.load(args.config)

    print("ZotPilot Status")
    print("=" * 40)
    print(f"  Zotero data dir:    {config.zotero_data_dir}")
    print(f"  ChromaDB path:      {config.chroma_db_path}")
    print(f"  Embedding provider: {config.embedding_provider}")
    print(f"  Embedding model:    {config.embedding_model}")
    print(f"  Embedding dims:     {config.embedding_dimensions}")
    print(f"  Reranking enabled:  {config.rerank_enabled}")
    print(f"  Vision enabled:     {config.vision_enabled}")

    errors = config.validate()
    if errors:
        print(f"\n  Config errors:")
        for e in errors:
            print(f"    - {e}")
        return 1

    # Try to get index stats
    try:
        from .embeddings import create_embedder
        from .vector_store import VectorStore

        embedder = create_embedder(config)
        store = VectorStore(config.chroma_db_path, embedder)
        doc_ids = store.get_indexed_doc_ids()
        total = store.count()
        print(f"\n  Index stats:")
        print(f"    Documents: {len(doc_ids)}")
        print(f"    Chunks:    {total}")
        if doc_ids:
            print(f"    Avg chunks/doc: {total / len(doc_ids):.1f}")
    except Exception as e:
        print(f"\n  Could not read index: {e}")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zotpilot",
        description="ZotPilot — AI-powered Zotero research assistant",
    )
    subparsers = parser.add_subparsers(dest="command")

    # setup
    sub_setup = subparsers.add_parser("setup", help="Interactive setup wizard")
    sub_setup.set_defaults(func=cmd_setup)

    # index
    sub_index = subparsers.add_parser("index", help="Index Zotero library")
    sub_index.add_argument("--force", action="store_true", help="Force re-index all")
    sub_index.add_argument("--limit", type=int, default=None, help="Max items to index")
    sub_index.add_argument("--item-key", type=str, default=None, help="Index specific item")
    sub_index.add_argument("--title", type=str, default=None, help="Filter by title regex")
    sub_index.add_argument("--no-vision", action="store_true", help="Disable vision extraction")
    sub_index.add_argument("--config", type=str, default=None, help="Config file path")
    sub_index.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub_index.set_defaults(func=cmd_index)

    # status
    sub_status = subparsers.add_parser("status", help="Show config and index stats")
    sub_status.add_argument("--config", type=str, default=None, help="Config file path")
    sub_status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)

    if not args.command:
        # Default: run MCP server
        from .server import main as server_main
        server_main()
        return 0

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
