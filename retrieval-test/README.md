# retrieval-test

An **editable-spec retrieval pipeline**. The optimized system is a retrieval
DAG: an editable spec directory configures a pipeline that produces ranked
candidates per query.

## Layout

```
retrieval-test/
├── src/retrieval_test/
│   ├── spec_loader.py     # load_pipeline(spec_dir): materialize + validate a spec
│   ├── generate.py        # retrieval-test-generate --spec-dir ... (produce predictions)
│   ├── evaluate.py        # retrieval-test-evaluate --spec-dir ... (nDCG@k, Recall@k)
│   ├── spec_info.py       # retrieval-test-spec-info --spec-dir ... (render the spec)
│   └── io_utils.py
├── specs/
│   └── default/           # baseline spec
│       ├── pipeline.json  # the DAG: ordered nodes + params
│       ├── nodes/         # node implementations: recall_bm25, recall_dense, fuse_rrf, rerank_cross, truncate (editable .py)
│       └── prompt/        # prompt files nodes read at run time
├── datasets/              # (place dataset splits here)
└── tools/
    └── run_test.py        # convenience: generate + evaluate in one shot
```

## A spec directory = the optimized object

A spec dir is a directory tree. Three layers, in increasing code cost to mutate:

- `pipeline.json` — the DAG: which nodes, in what order, enabled/disabled, and
  each node's params. **Primary mutation surface**: a Proposer reorders/relabels
  nodes or retunes params here, in plain JSON, with no `.py` edit.
- `prompt/*.md` — prompt / resource files. `load_pipeline` reads every file in
  `prompt/` into `ctx.resources["prompts"]`, keyed by **filename stem**
  (`prompt/rewrite.md` → `"rewrite"`), on every load. This layer is
  **non-advisory**: the files are read by the system at load time and made
  available to nodes at run time, so they are part of execution, not just
  Proposer context. A node that has been **wired to consume** its prompt reads
  `ctx.resources.get("prompts", {}).get("<stem>")`, substitutes `{query}` (the
  conventional slot a prompt must expose), and calls the model — at which point
  editing the `.md` changes behavior with no `.py` edit. When the consumer node
  is still a skeleton (e.g. `query_rewrite` today returns the query unchanged
  for every non-`identity` mode), editing the `.md` has **no output effect yet**;
  the prompt is in `ctx` but no node reads it. Non-advisory means "loaded and
  reachable at run time", not "automatically affects output" — that requires the
  consumer node's `run` to actually read and act on the prompt.
- `nodes/*.py` — node implementations. Edited for code-level mutation (e.g.
  wiring a node to consume a prompt, or changing a retriever's algorithm). Hard
  constraints (e.g. "truncate k must be >= 1") live inside each node's `run` as
  plain Python guards — there is no separate invariants layer; the author
  enforces them at the point where the relevant state actually exists.

`load_pipeline(spec_dir)` reads `pipeline.json`, dynamically imports each
node's `run` from `nodes/`, reads `prompt/*` into `ctx.resources["prompts"]`,
and returns a `Pipeline` ready to drive.

### How to modify the system — by layer, lowest cost first

1. **Params / DAG (`pipeline.json`)** — reorder/relabel nodes, flip `enabled`,
   retune params. Pure JSON; no code change; takes effect on next load.
2. **Prompt files (`prompt/*.md`)** — edit the text (keep the `{query}` slot).
   Takes effect on next load **only if the consuming node is already wired**;
   otherwise this is preparation for a step-3 edit, not a behavior change on its
   own.
3. **Node code (`nodes/*.py`)** — anything params/prompts cannot express:
   wiring a node to consume a prompt (the step that makes a prompt file
   behavior-active), changing an algorithm, adding a guard. This is the only
   layer that moves behavior a param/prompt edit cannot.

   Node signatures are a fixed contract: `def init(params: dict, ctx:
   "PipelineCtx") -> None` and `def run(query: str, hits: list[dict], params:
   dict, ctx: "PipelineCtx") -> list[dict]` (import `PipelineCtx` under
   `TYPE_CHECKING` from `retrieval_test.spec_loader` — runtime import would
   cycle). A model-bearing node MUST follow the failure contract: a replayable
   transient from a client call raises `RateLimitError` (`retrieval_test.errors`)
   and the node **re-raises it** (never catches it into a silent fallback) so the
   harness can replay the whole DAG; only deterministic failures (no corpus
   text, empty corpus, no key) may fall back to a `status: "fallback"` note.
   See `rerank_cross.py` / `recall_dense.py` for the pattern.

### NOT in the spec — runner-level invariants a Proposer cannot edit

Some constraints live in runner source code (`src/retrieval_test/`), not in the
spec dir, so **they are not part of the mutation surface**: a Proposer cannot
change them by editing `pipeline.json` / `nodes/` / `prompt/`. They protect the
eval from degenerate specs a Proposer could otherwise produce:

- **`Pipeline.MAX_OUTPUT_HITS`** (`spec_loader.py`, currently **20**) — caps the
  pipeline's final hit count regardless of any recall/truncate `k` a spec sets.
  A spec that asks for a large return-k would otherwise emit the entire corpus,
  and with a large eval `-k` that makes `recall@k` trivially saturate to 1.0.
  When a run breaches the cap, the pipeline truncates to it and appends an
  `output_cap` warning span to the per-query trajectory (requested vs capped
  count). Raising it is a **source-code change**, not a spec edit.
- **`MAX_RETRIES` (10) / `RETRY_BACKOFF_CAP_S` (60)** (`generate.py`) — the
  rate-limit replay budget. When a model node (`recall_dense` / `rerank_cross` /
  LLM rewrite) hits a replayable transient (HTTP 429 / 5xx / network) after
  exhausting the client's own per-request retries, it raises `RateLimitError`
  (`errors.py`) **instead of silently degrading**. The harness then replays the
  whole per-query DAG up to 10 times with exponential backoff
  (2, 4, 8, 16, 32, 60, 60, …s), recording a `pipeline_retry` span per attempt
  on that query's trajectory. A query still failing after 10 is recorded as a
  failure (it is OMITTED from `predictions.jsonl`, counted in the summary's
  `n_failed`) and the run **continues** — it never aborts the batch and never
  silently degrades to BM25-only output. The dense index is memoized across
  replays, so a replay re-runs query embed + bm25 + rerank, not the doc-index
  build. Neither constant is in the spec; raising either is a source-code change.

  Why a node re-raise instead of degrade: pre-change, a rate-limited rerank
  silently fell back to recall order (a `fallback` note, indistinguishable from a
  real low-score rerank on the hit list), and a rate-limited dense query embed
  aborted the entire batch (the loop had no try/except). The replay layer makes
  both cases observable and recoverable.

## Python environment

`generate` / `evaluate` run in this project's own virtualenv, isolated from any
sibling repo. Embedding and rerank call the HTTP endpoints directly (no SDK):

```bash
python3 -m venv .venv                                   # create the project venv (py3.12)

# internal PyPI source (Ant) — used for numpy and any internal wheels
cat > .venv/pip.conf <<'EOF'
[global]
index-url = https://pypi.antfin-inc.com/simple
EOF

.venv/bin/pip install -e .                              # editable install + numpy
export BAILING_API_KEY="your-key"                       # or OPENAI_API_KEY
```

Embedding/rerank backends (stdlib `urllib`, no SDK):
- `POST https://antchat.alipay.com/v1/embeddings` — `recall_dense` / `query_rewrite`; default model `bge_m3`. See `src/retrieval_test/embedding_client.py`.
- `POST https://antchat.alipay.com/v1/rerank` — `rerank_cross`; default model `Qwen3-Reranker-0.6B` (needs the prefix/suffix/instruct formatting encoded in `src/retrieval_test/rerank_client.py`). See `src/retrieval_test/rerank_client.py`.

The toy skeleton runs with no API key: `recall_dense` returns `[]` and the
pipeline falls back to BM25. Dense/rerank turn live once a key is set in env.

## CLI

```bash
# generate predictions by running the default spec
.venv/bin/retrieval-test-generate --spec-dir specs/default \
    --queries datasets/queries.jsonl --corpus datasets/corpus.jsonl \
    --output-dir output/default

# evaluate the SAME spec (re-loads it) against gold
.venv/bin/retrieval-test-evaluate --spec-dir specs/default \
    --queries datasets/queries.jsonl --corpus datasets/corpus.jsonl \
    --output output/default -k 10

# inspect a spec (Proposer-facing schema)
.venv/bin/retrieval-test-spec-info --spec-dir specs/default

# one-shot generate + evaluate
.venv/bin/python tools/run_test.py --spec-dir specs/default \
    --queries datasets/queries.jsonl --corpus datasets/corpus.jsonl -k 10
```

## Skeleton status

The default spec uses a **toy in-memory scorer** so the CLI runs end-to-end
with no BEIR/Pyserini dependency. To run a real retrieval benchmark:

1. `pip install beir sentence-transformers` (or use Pyserini).
2. Replace `recall_bm25.run`'s body with a real BM25/dense ranker and load the
   corpus in `recall_bm25.init`.
3. Point `--queries` / `--corpus` at your train/dev/test splits.

The `generate` / `evaluate` interfaces and `--spec-dir` contract do not change.
