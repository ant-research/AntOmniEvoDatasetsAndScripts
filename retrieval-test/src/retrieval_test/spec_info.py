"""Print a human-readable summary of a spec_dir (for a Proposer's context).

Surface the whole editable spec — DAG, params, node files, extension rules,
prompt files — because the optimized object is the pipeline.
"""

from __future__ import annotations

import argparse
import logging

from retrieval_test.spec_loader import render_spec_schema

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(
        description="Render a spec_dir as a human-readable schema summary."
    )
    parser.add_argument("--spec-dir", required=True, help="Path to the spec directory.")
    args = parser.parse_args()
    print(render_spec_schema(args.spec_dir))


if __name__ == "__main__":
    main()
