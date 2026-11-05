"""One-off: verify reflection input/output token tracking is recorded."""
import logging, os
import appworld_gepa as g

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
_orig = g.load_dataset
g.load_dataset = lambda: tuple(_orig()[i][:1] for i in (0, 1))
g.MAX_STEPS = 2
g.MAX_METRIC_CALLS = 12
g.REFLECTION_MINIBATCH_SIZE = 1
g.USE_MERGE = False
g.MAX_VAL_SIZE = 1
g.REFLECTION_MAX_TOKENS = 128
g.LOG_DIR = os.path.join(g.APPWORLD_ROOT, "experiments", "gepa_optim", "verify_tokens")
os.makedirs(g.LOG_DIR, exist_ok=True)
g.main()