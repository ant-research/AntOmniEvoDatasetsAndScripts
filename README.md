# AntOmniEvoDatasetsAndScripts

This repository hosts the **datasets, evaluation scripts (harnesses), and artifacts** used by [AntOmniEvo](https://github.com/ant-research/AntOmniEvo), an auto-evolution framework.

## Clone

```bash
git clone git@code.alipay.com:lyuyubin.lyb/AntOmniEvoDatasetsAndScripts.git
cd AntOmniEvoDatasetsAndScripts
git submodule update --init   # fetch appworld
```

## Layout

```
├── appworld/          # AppWorld benchmark (git submodule, see "Branches")
├── retrieval-test/    # Editable-spec retrieval pipeline + datasets
└── terminal-bench/    # Terminal-Bench 2 harness + agent spec + artifacts
```

### retrieval-test — retrieval DAG experiments

The system under optimization is a retrieval DAG: `pipeline.json` (the DAG) +
`nodes/*.py` (node implementations) + `prompt/*.md` (prompts) together form the
editable tunable artifact.

- `src/retrieval_test/` — pipeline loading (`spec_loader`), prediction
  (`generate`), evaluation (`evaluate`, nDCG@k / Recall@k)
- `pipeline_specs/` — pipeline specs (the `default` baseline and evolved variants)
- `datasets/` — musique train/val/test splits are vendored here; more datasets
  can be prepared via `tools/prepare_*.py`
- `tools/` — data preparation and one-shot evaluation scripts

See `retrieval-test/README.md` for details.

### terminal-bench — Terminal-Bench 2 experiments

- `src/tbtest/` — evaluation harness: `generate` (run tasks, emit trajectories),
  `evaluate` (aggregate results)
- `spec/kira/` — the agent artifacts under optimization (KIRA agent code + skill)
- `artifacts/` — **evolved tunable artifacts** (`kira/`, `1a5bf0805ec0/`, ...;
  each contains `agent/` + `skill/`, where a skill is a complete
  SKILL.md + references + scripts directory)
- `test/` — Harbor job configs
- `docs/datasets.md` — **how to download the TB2.1 dataset (not vendored here)**
- `patches/` — patches against dependencies

See `terminal-bench/README.md` for details.

### appworld — AppWorld experiments (submodule)

AppWorld benchmark with local experimental agents. `.gitmodules` points to the
`appworld-local` branch of this repository.

## Datasets

| Dataset | Location / how to get |
|---|---|
| musique (train/val/test) | Vendored: `retrieval-test/datasets/musique/` |
| Terminal-Bench 2.1 | Not vendored — clone per `terminal-bench/docs/datasets.md` (version `2fd12b8`) |
| Terminal-Bench v1 (optional reference) | Same doc (version `d28711d`) |

## Branches

| Branch | Purpose |
|---|---|
| `master` | Main content: retrieval-test, terminal-bench, submodule references |
| `appworld-local` | Branch the appworld submodule points to; carries local experimental changes |

## Links

- AntOmniEvo framework: <https://github.com/ant-research/AntOmniEvo>
- Paper: *Mara Chain: Rethinking Failure as a Stepping Stone for AI System
  Auto-Evolution* (arXiv:2609.35855)

## License

Apache-2.0. Legal disclaimer: see `LEGAL.md`.
