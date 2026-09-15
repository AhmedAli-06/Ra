"""
Ra command-line interface for the retrieval index.

  python ra_cli.py rag index [--dir PATH] [--file PATH]
  python ra_cli.py rag search "question" [-k 4] [--type file]
  python ra_cli.py rag stats
  python ra_cli.py rag sources
  python ra_cli.py rag deindex --source PATH
  python ra_cli.py rag clear
  python ra_cli.py rag ingest-screen
  python ra_cli.py rag ingest-devices
  python ra_cli.py rag grant|revoke {files|devices|screen}
  python ra_cli.py ask "your question"      (one-shot LLM reply with RAG)
"""
import argparse
import os
import sys

from ra import config
from ra.rag.embedder import HashEmbedder
from ra.rag.retriever import retrieve
from ra.rag.store import SemanticStore


def _store() -> SemanticStore:
    return SemanticStore(config.RAG_INDEX_PATH)


def _embedder() -> HashEmbedder:
    return HashEmbedder(dim=config.RAG_EMBED_DIM)


def cmd_index(args):
    store, embedder = _store(), _embedder()
    from ra.rag import indexer
    tot = {"files": 0, "chunks": 0, "skipped": 0, "errors": []}
    if args.file:
        path = args.file
        if not os.path.exists(path):
            print(f"Not found: {path}")
            sys.exit(1)
        added = indexer.index_file(path, store, embedder)
        st = {"files": 1, "chunks": added, "skipped": 0, "errors": []}
        tot = st
        print(f"Indexed {path}: {added} chunks.")
    else:
        dirs = args.dir or config.LIBRARY_DIRS
        for d in dirs:
            if not os.path.exists(d):
                print(f"  (skipping missing dir) {d}")
                continue
            st = indexer.index_directory(d, store, embedder)
            for k in ("files", "chunks", "skipped"):
                tot[k] += st[k]
            tot["errors"] += st["errors"]
            print(f"  {d}: {st['files']} files, {st['chunks']} chunks, {st['skipped']} skipped")
    for e in tot["errors"][:10]:
        print(f"  error: {e}")
    print(f"Total: {tot['files']} files, {tot['chunks']} chunks indexed.")
    print(store.stats())


def cmd_search(args):
    results = retrieve(_store(), _embedder(), args.query, k=args.k,
                       source_types=[args.type] if args.type else None)
    if not results:
        print("No matches found. Try: python ra_cli.py rag index")
        return
    for r in results:
        print(f"[{r.source_type} | {r.source} | {r.score:.2f}]")
        print(" ".join(r.text.split())[:600])
        print()


def cmd_stats(_):
    print(_store().stats())


def cmd_sources(_):
    for s in _store().sources():
        print(f"{s['source_type']:8} {s['source']}")


def cmd_deindex(args):
    n = _store().delete_source(os.path.abspath(args.source))
    print(f"Removed {n} chunks for {args.source}")


def cmd_clear(_):
    n = _store().clear()
    print(f"Cleared index ({n} chunks).")


def cmd_ingest_screen(_):
    from ra.rag import sources
    out = sources.ingest_screen(_store(), _embedder())
    print(out["result"])
    print(out["text"])


def cmd_ingest_devices(_):
    from ra.rag import sources
    out = sources.ingest_devices(_store(), _embedder())
    print(out["result"])
    print(out["text"])


def cmd_access(args):
    area = getattr(args, "area", None)
    if area is None:
        print("Current access grants:")
        for name, allowed in config.GRANTED_ACCESS.items():
            print(f"  {name:8} {'GRANTED' if allowed else 'revoked'}")
        return
    if area not in config.GRANTED_ACCESS:
        print(f"Unknown area '{area}'. Choose from {sorted(config.GRANTED_ACCESS)}")
        sys.exit(1)
    config.GRANTED_ACCESS[area] = not getattr(args, "revoke", False)
    print(f"{area}: {'GRANTED' if not getattr(args, 'revoke', False) else 'revoked'} (session only)")


def cmd_ask(args):
    from ra import brain
    try:
        reply = brain.ask(args.query)
    except RuntimeError as e:
        print(f"Ra can't answer right now: {e}")
        return
    except Exception as e:
        print(f"Ra hit a problem talking to the LLM: {type(e).__name__}: {e}")
        return
    print(f"\033[96m{config.ASSISTANT_NAME}:\033[0m {reply}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ra", description="Ra retrieval CLI")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("rag", help="retrieval index commands")
    rs = r.add_subparsers(dest="rag_cmd", required=True)

    idx = rs.add_parser("index")
    idx.add_argument("--dir", action="append", help="directory to index (repeatable)")
    idx.add_argument("--file", help="single file to index")
    idx.set_defaults(func=cmd_index)

    se = rs.add_parser("search")
    se.add_argument("query")
    se.add_argument("-k", type=int, default=4)
    se.add_argument("--type", choices=["file", "device", "screen"])
    se.set_defaults(func=cmd_search)

    rs.add_parser("stats").set_defaults(func=cmd_stats)
    rs.add_parser("sources").set_defaults(func=cmd_sources)

    de = rs.add_parser("deindex")
    de.add_argument("--source", required=True)
    de.set_defaults(func=cmd_deindex)

    rs.add_parser("clear").set_defaults(func=cmd_clear)
    rs.add_parser("ingest-screen").set_defaults(func=cmd_ingest_screen)
    rs.add_parser("ingest-devices").set_defaults(func=cmd_ingest_devices)

    ac = rs.add_parser("grant")
    ac.add_argument("area", choices=["files", "devices", "screen"])
    ac.add_argument("--revoke", action="store_true")
    ac.set_defaults(func=cmd_access)

    rv = rs.add_parser("revoke")
    rv.add_argument("area", choices=["files", "devices", "screen"])
    rv.set_defaults(func=cmd_access, revoke=True)

    status = rs.add_parser("access")
    status.set_defaults(func=cmd_access, area=None)

    a = sub.add_parser("ask")
    a.add_argument("query")
    a.set_defaults(func=cmd_ask)

    return p


def main(argv=None):
    from ra import ensure_utf8_console
    ensure_utf8_console()
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()