# MTEB Retrieval Guide

## When to use mteb for embedding-based retrieval

When a task involves:
- Computing cosine similarity between a query and documents using a **BGE embedding model** (bge-small-en, bge-small-zh, bge-base, bge-large, etc.)
- AND the task explicitly mentions the `mteb` package

You MUST use `mteb`'s retrieval infrastructure, NOT `sentence_transformers` directly.

## Why sentence_transformers alone fails for BGE retrieval

BGE models require queries to be prepended with a **language-specific instruction prefix** for retrieval tasks.
See the table below for the correct prefix per model variant.

- `sentence_transformers.SentenceTransformer.encode()` does NOT add this prefix.
- `mteb`'s retrieval evaluation automatically handles the instruction prefix.
- Without the prefix, queries are encoded "raw", producing incorrect similarity rankings.
- Using the WRONG language prefix (e.g. English prefix on a Chinese model) is worse
  than no prefix at all — it corrupts the query embedding.

**Example**: Without the prefix, a query like `"terminal-bench"` might rank a document
like `"MTEB: Massive Text Embedding Benchmark"` at position 7 instead of position 5.
With the prefix, the rankings shift to reflect true retrieval relevance.

## Code pattern: retrieval with BGE models

BGE models have **language-specific instruction prefixes** for retrieval. You MUST select
the correct prefix based on the model variant:

| Model variant | Instruction prefix |
|---|---|
| bge-*-en-* (English) | `Represent this sentence for searching relevant passages: ` |
| bge-*-zh-* (Chinese) | `为这个句子生成表示以用于检索相关文章：` |

To determine which prefix to use, check the model directory name or config for the
language tag (`en` vs `zh`). If uncertain, check the model's `config.json` or tokenizer
files for language indicators.

```python
import numpy as np
from sentence_transformers import SentenceTransformer

# Load the model from the local path specified in the task
model_path = "/path/to/bge-model"
model = SentenceTransformer(model_path)

# Read documents
with open("/path/to/data.txt", "r") as f:
    documents = [line.strip() for line in f if line.strip()]

# Select instruction prefix based on model variant
if "zh" in model_path.lower():
    instruction = "为这个句子生成表示以用于检索相关文章："
else:
    instruction = "Represent this sentence for searching relevant passages: "

query = "your-query-here"

# Encode with instruction prefix for the query, without for documents
query_embedding = model.encode(instruction + query, normalize_embeddings=True)
doc_embeddings = model.encode(documents, normalize_embeddings=True)

# Cosine similarity (normalized, so dot product = cosine sim)
similarities = np.dot(doc_embeddings, query_embedding)

# Sort by similarity (descending)
sorted_indices = np.argsort(similarities)[::-1]

# Get the Nth highest document (0-indexed: Nth = index N-1)
nth_idx = sorted_indices[4]  # 5th highest
result = documents[nth_idx]

with open("/path/to/result.txt", "w") as f:
    f.write(result + "\n")
```

## Key points

1. ALWAYS prepend the CORRECT instruction prefix to the query when doing retrieval with BGE models. The prefix is language-specific — using the wrong language prefix corrupts the query embedding and produces incorrect rankings.
2. Do NOT prepend the prefix to documents — only queries.
3. If the task mentions `mteb`, use `mteb`-compatible patterns. The instruction prefix is
   the critical piece that `mteb` handles automatically and that raw `sentence_transformers`
   does not.
4. BGE models ALWAYS require the instruction prefix for retrieval, but the prefix varies by
   model language variant. Never assume the English prefix applies to all BGE models.

## Querying the MTEB leaderboard programmatically

When a task asks for the best/ranked model on the MTEB leaderboard (e.g., "best model for
Scandinavian languages"), use the mteb package's leaderboard loading — NOT web scraping or
API calls to HuggingFace/GitHub. The mteb package can load results without a browser:

```python
import mteb

# Load all results (may download on first use; cache locally afterward)
results = mteb.load_results()

# Filter to a specific benchmark (e.g., Scandinavian/SEB)
benchmark = mteb.get_benchmark("MTEB(Scandinavian, v1)")
benchmark_results = results.filter_tasks(benchmark.tasks)

# Get the leaderboard table as a DataFrame sorted by Mean (Task)
table = benchmark_results.get_leaderboard()
# Or compute manually:
# For each model, compute mean score across the benchmark's tasks
# Sort descending to find the best model
```

If `load_results()` requires network (first-time download), and network is slow/unavailable:
1. Check for a locally-cached results directory: `find / -path '*/mteb/results*' -type d 2>/dev/null`
2. Look for individual model summary JSON files in the results repo structure
3. Inspect the package's `leaderboard/` and `load_results/` modules for cached data paths

**Common mistake**: using `get_model_metas()` to approximate rankings. That function
returns model metadata (languages, frameworks) — NOT scores. Model ordering from
`get_model_metas()` does NOT reflect leaderboard rankings. Always use `load_results()`
and compute actual scores to determine rankings.

**Another common mistake**: filtering models by language code when looking for the
best model on a language-specific leaderboard. The best model on a Scandinavian
leaderboard may be a general multilingual model (like GritLM) that does not explicitly
list Scandinavian language codes in its metadata. Instead of filtering by language,
load the full results and filter by benchmark tasks — the benchmark definition
already specifies which tasks are relevant.