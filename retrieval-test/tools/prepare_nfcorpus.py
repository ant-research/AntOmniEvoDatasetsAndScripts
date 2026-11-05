#!/usr/bin/env python3
"""Download + convert BEIR NFCorpus into this project's dataset schema.

Source: BeIR/nfcorpus on Hugging Face (via the hf-mirror.com CN mirror, since
huggingface.co is unreachable from this network). The upstream repo ships the
corpus and queries as parquet and the relevance labels (qrels) as TSV in a
sibling repo BeIR/nfcorpus-qrels (train/dev/test splits).

Outputs (under datasets/nfcorpus/):
    corpus.jsonl   — {"_id": ..., "title": ..., "text": ...}   (BEIR-native)
    queries.jsonl  — {"query_id": ..., "query": ..., "golden_doc_ids": [...]}
                     (one record per query-id present in qrels.test.tsv)

The project's corpus loader (generate.py:_load_corpus / evaluate.py:_load_corpus)
already understands BEIR fields (`_id` -> doc_id, `text` -> doc text), so these
files drop straight into `--corpus` / `--queries`.

Raw downloads are cached under datasets/nfcorpus/_raw/ so re-runs are idempotent.

Usage:
    python tools/prepare_nfcorpus.py            # download + convert (test split)
    python tools/prepare_nfcorpus.py --split dev
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT_ROOT / "datasets" / "nfcorpus"
RAW_DIR = OUT_DIR / "_raw"

MIRROR = "https://hf-mirror.com"
CORPUS_URL = f"{MIRROR}/datasets/BeIR/nfcorpus/resolve/main/corpus/corpus-00000-of-00001.parquet"
QUERIES_URL = f"{MIRROR}/datasets/BeIR/nfcorpus/resolve/main/queries/queries-00000-of-00001.parquet"
QRELS_URL = f"{MIRROR}/datasets/BeIR/nfcorpus-qrels/resolve/main/{{split}}.tsv"


def _download(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  cached: {dest.name} ({dest.stat().st_size} bytes)")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  downloading {url}")
    urllib.request.urlretrieve(url, dest)
    print(f"  -> {dest.name} ({dest.stat().st_size} bytes)")


def _read_parquet(path: Path):
    import pyarrow.parquet as pq  # optional dep, only needed for conversion
    return pq.read_table(path).to_pydict()


def _write_jsonl(records, path: Path) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"  wrote {path.relative_to(PROJECT_ROOT)} ({len(records)} records)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", default="test", choices=["train", "dev", "test"],
                        help="qrels split to use for golden_doc_ids (default test).")
    args = parser.parse_args()

    print("[1/3] downloading raw files (hf-mirror.com)…")
    _download(CORPUS_URL, RAW_DIR / "corpus.parquet")
    _download(QUERIES_URL, RAW_DIR / "queries.parquet")
    for split in ("train", "dev", "test"):
        _download(QRELS_URL.format(split=split), RAW_DIR / f"qrels.{split}.tsv")

    print("[2/3] converting to project schema…")
    corpus = _read_parquet(RAW_DIR / "corpus.parquet")
    corpus_records = [
        {"_id": str(_id), "title": str(title or ""), "text": str(text or "")}
        for _id, title, text in zip(corpus["_id"], corpus["title"], corpus["text"])
    ]
    _write_jsonl(corpus_records, OUT_DIR / "corpus.jsonl")

    queries_table = _read_parquet(RAW_DIR / "queries.parquet")
    qtext: dict[str, str] = {
        str(qid): str(qt)
        for qid, qt in zip(queries_table["_id"], queries_table["text"])
    }

    # qrels: query-id<TAB>corpus-id<TAB>score
    golden: dict[str, list[str]] = {}
    with (RAW_DIR / f"qrels.{args.split}.tsv").open(encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            qid = row["query-id"]
            cid = row["corpus-id"]
            golden.setdefault(qid, []).append(cid)

    missing_text = [qid for qid in golden if qid not in qtext]
    if missing_text:
        print(f"  WARNING: {len(missing_text)} qrels query-ids have no query text", file=sys.stderr)

    query_records = [
        {"query_id": qid, "query": qtext.get(qid, ""), "golden_doc_ids": golden[qid]}
        for qid in sorted(golden)
    ]
    _write_jsonl(query_records, OUT_DIR / "queries.jsonl")

    print("[3/3] done.")
    print(f"  corpus : {len(corpus_records)} docs")
    print(f"  queries: {len(query_records)} eval queries (split={args.split})")


if __name__ == "__main__":
    main()