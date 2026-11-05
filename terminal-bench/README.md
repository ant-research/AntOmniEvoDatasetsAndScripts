# terminal-bench

Generation & evaluation harness (`tbtest`) and agent artifacts for the
Terminal-Bench 2 experiments.

## Layout

```
terminal-bench/
├── pyproject.toml        # tbtest package; provides the `generate` / `evaluate` CLIs
├── src/tbtest/           # harness code: generate.py (run tasks, emit trajectories),
│                         #   evaluate.py (aggregate results), trajectory*.py
├── spec/kira/            # agent artifacts under optimization: KIRA agent code + skill
├── artifacts/            # evolved tunable artifacts (agent/ + skill/ directories)
├── test/                 # Harbor job configs (job-config-kira.yaml, ...)
├── patches/              # patches against dependencies (harbor-0.20.0-tmux-timeout.patch)
├── docs/                 # docs; dataset download instructions in docs/datasets.md
└── dataset2.1/           # (not vendored) Terminal-Bench 2.1 dataset,
                          #   see docs/datasets.md
```

## Quick start

```bash
uv venv && uv pip install -e .
# download the dataset first (see docs/datasets.md), then:
uv run generate -c test/job-config-kira.yaml --spec-dir spec/kira \
  -m <model> -i <task> -n 1 -l 1 --force-build
uv run evaluate <jobs_dir>
```

All `generate.py` defaults are relative to this directory (the project root):
dataset `dataset2.1/`, config `test/job-config-kira.yaml`, spec `spec/kira`.
