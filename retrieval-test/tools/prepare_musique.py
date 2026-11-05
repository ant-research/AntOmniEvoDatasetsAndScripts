#!/usr/bin/env python3
"""Convert a MuSiQue jsonl into this project's BEIR-style retrieval schema.

MuSiQue is a multi-hop QA dataset whose raw records each carry:
    {id, question, paragraphs[{idx,title,paragraph_text,is_supporting}],
     question_decomposition, answer, answer_aliases, answerable}

This converter builds a GLOBAL retrieval corpus (one corpus, all queries
retrieve from it) — the standard form for production RAG and for IR benchmarks
(MS MARCO, Natural Questions, BEIR, ...). That differs from MuSiQue's original
"distractor setting", which retrieves each question only within its own ~20
pre-attached candidate paragraphs; the global form is harder and reflects a
real RAG system, so expect Recall@k well below the distractor-setting numbers.

Output (under datasets/musique/<split>/):
    corpus.jsonl   — {"_id": <id>__<idx>, "title":<para.title>, "text":<para.paragraph_text>}
                     one record per unique paragraph across ALL questions (dedup by _id).
    queries.jsonl  — {"query_id": <id>, "query": <question>,
                       "golden_doc_ids": [<id>__<idx> for paragraphs with is_supporting True]}

The project's corpus/queries loaders understand these fields directly, so the
files drop into `retrieval-test-generate --queries ... --corpus ...`.

Doc id convention: `<musique_id>__<paragraph_idx>` is globally unique because a
MuSiQue id uniquely scopes its paragraph idxs, so paragraphs from different
questions never collide even if they share an idx.

Usage:
    # by split (uses musique_full_v1.0_<split>.jsonl under --src-dir)
    python tools/prepare_musique.py --split dev --limit 50

    # by explicit file (preferred for the filtered *_4hop_*.jsonl shards —
    # e.g. to match the train/val choice in auto-opt's agentic_rag example)
    python tools/prepare_musique.py \
        --src-file /path/to/.../filtered/musique_full_v1.0_train_4hop_300.jsonl \
        --out-dir datasets/musique/train
    python tools/prepare_musique.py \
        --src-file /path/to/.../filtered/musique_full_v1.0_dev_4hop_100.jsonl \
        --out-dir datasets/musique/val

`--limit` caps the number of questions converted (handy to validate the flow
before converting the full set); when set it also restricts the global corpus
to paragraphs reachable from those questions.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC_DIR = Path("/Users/jacklv/work/files/rag-dataset/musique/data")
DEFAULT_OUT_ROOT = PROJECT_ROOT / "datasets" / "musique"

# File naming convention used by the local MuSiQue dump.
#   musique_full_v1.0_<split>.jsonl   (answerable questions; used here)
#   musique_ans_v1.0_<split>.jsonl    (answer subset)
SRC_TEMPLATE = "musique_full_v1.0_{split}.jsonl"


def _is_supporting(value) -> bool:
    """Read a paragraph's is_supporting flag, tolerating bool or string forms.

    BeIR/HF dumps sometimes serialize booleans as the strings "true"/"false"
    (any case). Accept both; treat anything truthy-and-not-clearly-false as
    supporting only when it is unambiguously True, so we never tag a distractor
    as gold.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    s = str(value).strip().lower()
    return s in ("true", "1", "yes")


def _doc_id(musique_id: str, paragraph_idx) -> str:
    """Globally-unique doc id: <musique_id>__<paragraph_idx>."""
    return f"{musique_id}__{paragraph_idx}"


def _iter_raw(src: Path):
    """Yield raw MuSiQue records from a jsonl, skipping blank lines."""
    with src.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def convert(src_path: Path, out_dir: Path, limit: int | None) -> tuple[int, int]:
    """Convert one MuSiQue jsonl split into corpus.jsonl + queries.jsonl.

    Returns (n_queries, n_corpus_docs).
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    corpus: dict[str, dict] = {}  # _id -> {document} (dedup)
    query_records: list[dict] = []
    n_questions = 0
    n_no_gold = 0
    n_raw = 0

    for rec in _iter_raw(src_path):
        n_raw += 1
        musique_id = str(rec.get("id", "")).strip()
        if not musique_id:
            continue
        question = str(rec.get("question", "")).strip()
        if not question:
            continue

        golden_doc_ids: list[str] = []
        for para in rec.get("paragraphs", []):
            idx = para.get("idx")
            doc_id = _doc_id(musique_id, idx)
            # Build/refresh the global corpus doc (dedup: same doc_id seen once).
            if doc_id not in corpus:
                corpus[doc_id] = {
                    "_id": doc_id,
                    "title": str(para.get("title", "") or ""),
                    "text": str(para.get("paragraph_text", "") or ""),
                }
            if _is_supporting(para.get("is_supporting")):
                golden_doc_ids.append(doc_id)

        if not golden_doc_ids:
            # A question with no supporting paragraphs (some dev/test items
            # leave is_supporting unset) cannot be scored for retrieval; skip it
            # with a count rather than producing a gold-less query that would
            # silently score 0 and drag down the mean.
            n_no_gold += 1
            continue

        query_records.append({
            "query_id": musique_id,
            "query": question,
            "golden_doc_ids": golden_doc_ids,
        })
        n_questions += 1
        if limit is not None and n_questions >= limit:
            break

    if n_no_gold:
        print(f"  note: skipped {n_no_gold} of {n_raw} raw questions with no "
              f"supporting paragraphs (cannot be scored for retrieval)",
              file=sys.stderr)

    # Write queries (sorted for stable diffs); corpus written in insertion order.
    # NB: when --limit is set, the corpus still contains paragraphs from ALL raw
    # records read (including the skipped no-gold ones), since paragraphs enter the
    # global pool before the gold check. That's fine — extra distractors only — but
    # means the corpus size is a function of raw records read, not just scored queries.
    query_records.sort(key=lambda r: r["query_id"])
    _write_jsonl(query_records, out_dir / "queries.jsonl")

    corpus_records = [corpus[k] for k in sorted(corpus)]
    _write_jsonl(corpus_records, out_dir / "corpus.jsonl")

    return len(query_records), len(corpus_records)


def _write_jsonl(records, path: Path) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    # Resolve to absolute before relativizing, so a relative --out-dir (run from
    # the project root) still prints a clean repo-relative path.
    rel = path.resolve().relative_to(PROJECT_ROOT.resolve())
    print(f"  wrote {rel} ({len(records)} records)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src-file", default=None,
                        help="Direct path to a MuSiQue jsonl (preferred for the "
                             "filtered *_4hop_*.jsonl shards). Overrides --split/--src-dir.")
    parser.add_argument("--src-dir", default=str(DEFAULT_SRC_DIR),
                        help="Directory containing musique_full_v1.0_<split>.jsonl "
                             "(default: the local MuSiQue dump). Ignored if --src-file is set.")
    parser.add_argument("--split", default="dev",
                        help="Split label (default dev). Used for printing and the "
                             "default out-dir fallback (datasets/musique/<split>). "
                             "Ignored if --src-file and --out-dir are both given. "
                             "Accepted values: train, dev, test, val, ...")
    parser.add_argument("--out-dir", default=None,
                        help="Output directory (default: datasets/musique/<split or src stem>).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap number of questions converted (default: all).")
    args = parser.parse_args()

    if args.src_file:
        src_path = Path(args.src_file)
        label = args.split  # used only for the printed label / default out-dir fallback
    else:
        src_path = Path(args.src_dir) / SRC_TEMPLATE.format(split=args.split)
        label = args.split
    if not src_path.exists():
        sys.exit(f"source not found: {src_path}")
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_OUT_ROOT / label

    print(f"converting {src_path} -> {out_dir}")
    n_queries, n_corpus = convert(src_path, out_dir, args.limit)

    print("done.")
    print(f"  corpus : {n_corpus} docs (global)")
    print(f"  queries: {n_queries} (label={label})")
    print("usage:")
    print(f"  retrieval-test-generate --spec-dir specs/default "
          f"--queries {out_dir/'queries.jsonl'} --corpus {out_dir/'corpus.jsonl'} "
          f"--output-dir output/musique_{label}")


if __name__ == "__main__":
    main()