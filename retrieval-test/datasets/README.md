# datasets/

NFCorpus retrieval benchmark, in this project's schema.

```
datasets/
├── nfcorpus/                 # BEIR NFCorpus — the real benchmark
│   ├── corpus.jsonl          # {"_id", "title", "text"}        3,633 docs
│   ├── queries.jsonl         # {"query_id","query","golden_doc_ids"} 323 queries (test split)
│   └── _raw/                 # cached raw downloads (parquet + qrels tsv)
├── corpus.jsonl              # toy 5-doc placeholder (kept for quick smoke tests)
└── queries.jsonl             # toy 3-query placeholder
```

## Source

[BeIR/nfcorpus](https://huggingface.co/datasets/BeIR/nfcorpus) on Hugging Face,
pulled via the **hf-mirror.com** CN mirror (huggingface.co is unreachable from
this network). Relevance labels come from the sibling repo
[BeIR/nfcorpus-qrels](https://huggingface.co/datasets/BeIR/nfcorpus-qrels).

To (re)download + convert:

```bash
python tools/prepare_nfcorpus.py              # test split (default)
python tools/prepare_nfcorpus.py --split dev  # or train
```

The script caches raw files under `nfcorpus/_raw/` so re-runs are idempotent.
It needs `pyarrow` (already in this project's venv) to read the parquet corpus.

## Schema

- **corpus.jsonl** keeps BEIR-native field names (`_id`, `title`, `text`).
  The loader (`generate._load_corpus` / `evaluate._load_corpus`) maps `_id → doc_id`
  and `text → doc text`, so this drops straight into `--corpus`.
- **queries.jsonl** is `{query_id, query, golden_doc_ids}` — exactly what
  `generate`/`evaluate` expect. `golden_doc_ids` are the docs labelled relevant
  for that query in the chosen qrels split.

## Running

```bash
# generate predictions on the full test split
retrieval-test-generate --spec-dir specs/default \
    --queries datasets/nfcorpus/queries.jsonl \
    --corpus  datasets/nfcorpus/corpus.jsonl \
    --output-dir output/nfcorpus

# evaluate
retrieval-test-evaluate --spec-dir specs/default \
    --queries datasets/nfcorpus/queries.jsonl \
    --corpus  datasets/nfcorpus/corpus.jsonl \
    --predictions output/nfcorpus/predictions.jsonl \
    --output-dir output/nfcorpus -k 10
```

## Note on the toy scorer

The baseline `recall_bm25` node ships a toy term-overlap scorer
(`specs/default/nodes/recall_bm25.py`). On real NFCorpus it only matches exact
whitespace-split tokens, so many medical queries return zero hits — a first-20q
sample scored ~0.025 nDCG@10. That's the scorer's floor, not a data problem:
the dataset is sound (all 323 queries carry gold labels, all gold doc_ids exist
in the corpus). Replacing the toy scorer with real BM25 (Pyserini / rank_bm25,
as documented in the node's `init`) is the intended next optimization.