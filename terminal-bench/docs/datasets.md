# Datasets

Datasets are not vendored in this repository — clone them as instructed below.
The pinned versions have been verified against this project (all 89 tasks match
exactly).

## Terminal-Bench 2.1 (task dataset, required)

- Repository: <https://github.com/laude-institute/terminal-bench-2>
- Version: `2fd12b8` (2026-04-29, i.e. TB2.1)

```bash
cd terminal-bench   # this project's root
git clone https://github.com/laude-institute/terminal-bench-2.git dataset2.1
cd dataset2.1 && git checkout 2fd12b8
```

`generate.py` reads `<project root>/dataset2.1` by default; point elsewhere with
`-p <path>`, or symlink: `ln -s <actual path> dataset2.1`.

## Terminal-Bench v1 (original tasks + official harness, optional reference)

- Repository: <https://github.com/laude-institute/terminal-bench>
- Version: `d28711d` (2026-07-10)

```bash
git clone https://github.com/laude-institute/terminal-bench.git
cd terminal-bench && git checkout d28711d
```

Note: the cloned directory is also named `terminal-bench` — clone it OUTSIDE
this project to avoid confusion. The generate/evaluate flow does not depend on
that repository; only the v1 task definitions (`original-tasks/`,
`registry.json`) are useful as reference.
