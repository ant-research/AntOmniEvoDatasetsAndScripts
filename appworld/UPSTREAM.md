# AppWorld (vendored fork)

This directory is a vendored copy of [AppWorld](https://github.com/StonyBrookNLP/appworld),
with local experiment changes made for AntOmniEvo.

- **Upstream**: https://github.com/StonyBrookNLP/appworld
- **Local changes**: batch/full run scripts, task configs, GEPA integration,
  `skill_react_code_agent` prompt variants, pruning of legacy tasks/docs
- **Data**: the large generated `data/` directory (task environments, DBs) is not
  tracked; run the upstream setup/generation scripts to produce it.
