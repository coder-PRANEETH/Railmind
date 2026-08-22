import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "agents"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    # Tests must never hit the real LLM API just because the developer has
    # a key exported; the LLM path is exercised via monkeypatched stubs only.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TWIN_BASE_URL", raising=False)
    # all_agent captures TWIN_CANDIDATES at import time (during collection),
    # so the delenv above cannot stop twin probing by itself — neutralize the
    # captured list too so no test ever probes a live twin over the network.
    # Tests that need probing monkeypatch TWIN_CANDIDATES (or _twin_base /
    # _twin_base_cache) themselves. Lazy import: agents/ is on sys.path above.
    import all_agent
    monkeypatch.setattr(all_agent, "TWIN_CANDIDATES", [None], raising=False)
