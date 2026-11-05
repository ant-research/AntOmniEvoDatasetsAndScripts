"""AppWorld CLI entry points.

Installed as console_scripts by the appworld-agents package:
  - appworld-list-tasks
  - appworld-run-batch
  - appworld-eval-batch
"""

from __future__ import annotations


def list_tasks_main() -> None:
    from appworld_agents.list_tasks import main
    main()


def run_batch_main() -> None:
    from appworld_agents.run_batch import main
    main()


def eval_batch_main() -> None:
    from appworld_agents.eval_batch import main
    main()