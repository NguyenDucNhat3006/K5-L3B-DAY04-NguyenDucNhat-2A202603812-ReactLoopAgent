"""Re-run the original loop with the unchanged official eval runner."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'evals'))

from baseline.agent import run_agent
from env_config import load_env
import run_evals


if __name__ == '__main__':
    load_env()
    run_evals.run_agent = run_agent
    sys.exit(run_evals.main())
