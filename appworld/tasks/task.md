跑一下 test normal 和 test challenge（跑完全部数据，/Users/jacklv/work/appworld/data/datasets/test_normal.txt 和 /Users/jacklv/work/appworld/data/datasets/test_challenge.txt,注意一个一个跑，不要两个同时跑），
使用 /Users/jacklv/work/appworld/experiments/run_batch.py 和 /Users/jacklv/work/appworld/experiments/eval_batch.py
配置见 /Users/jacklv/work/appworld/env/config.yaml
skill dir 为 /Users/jacklv/work/appworld/experiments/prompts/skill_react_code_agent/empty-skill
模型为 DeepSeek-V4-Pro-0813，concurrenct 为 30
然后 evaluate 一下 concurrency 为 10，计算出 TGC 和 SGC
使用 /Users/jacklv/work/appworld/appworld_agents/code/simplified/skill_react_code_agent.py
输出到 /Users/jacklv/work/appworld/experiments/outputs，创建一个文件夹体现实验特点，时间为后缀

skill_{version}_{model}_{timestamp}/                        # 外层实验目录
├── results.md                   # 汇总结果报告
└── experiments/outputs/
    ├── antgroup_react_agent_and_auto_optimized_skill_test_normal/     
    │   ├── run_result.json
    │   ├── eval_result.json
    │   └── tasks/...
    └── antgroup_react_agent_and_auto_optimized_skill_test_challenge/ 
        ├── run_result.json
        ├── eval_result.json
        └── tasks/...

evaluate metrics 说明见 /Users/jacklv/work/appworld/docs/tgc_sgc_metrics.md