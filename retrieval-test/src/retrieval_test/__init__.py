"""retrieval_test — an editable-spec retrieval pipeline.

An editable spec = a directory tree (pipeline.json + nodes/ + prompt/) that
configures a retrieval DAG. `generate` and `evaluate` both take `--spec-dir`,
load the spec directory's pipeline config into the system, then run it. Hard
constraints live inside each node's `run` as plain Python guards — there is no
separate invariants layer.
"""

__version__ = "0.1.0"