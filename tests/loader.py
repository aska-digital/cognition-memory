"""Hermetic loader for cognition acceptance tests.

Loads plugin modules by file path under a synthetic package (so relative
imports work) — no repo-wide imports, no network, no hermes home. Runs under
any Python 3.9+ interpreter.

The ``agent.memory_provider`` base class (``MemoryProvider``) is a real Hermes
test dependency and must resolve to the real Hermes contract. Resolution
order: an already-imported module, then the normal import system (an
installed hermes-agent package or a hermes-agent checkout on ``PYTHONPATH``),
then the documented ``HERMES_AGENT_ROOT`` env var pointing at a hermes-agent
checkout (``$HERMES_AGENT_ROOT/agent/memory_provider.py``). There is
deliberately no assumed ex-in-tree relative layout and no silent stub: when
the dependency cannot be found, loading fails fast with a deterministic
``RuntimeError`` naming the missing module and how to provide it.
"""

import importlib
import importlib.util
import os
import sys
import types

COG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "cogtest_under_test"

#: Env var pointing at a hermes-agent checkout (repo root containing
#: ``agent/memory_provider.py``). Explicit and documented — never guessed
#: from this repo's own location.
HERMES_AGENT_ROOT_ENV = "HERMES_AGENT_ROOT"

_MISSING_DEPENDENCY = (
    "cognition tests need the real Hermes dependency "
    "'agent.memory_provider' (MemoryProvider base class): %s. Provide it via "
    "an installed hermes-agent package, PYTHONPATH including a hermes-agent "
    "checkout, or the %s env var pointing at one "
    "($%s/agent/memory_provider.py). Stubbing the base class is not allowed "
    "— tests must exercise the real provider contract."
)


def _load_from_root(root: str):
    """Load ``agent.memory_provider`` from a hermes-agent checkout root."""
    agent_dir = os.path.join(root, "agent")
    path = os.path.join(agent_dir, "memory_provider.py")
    if not os.path.isfile(path):
        return None
    if "agent" not in sys.modules:
        pkg = types.ModuleType("agent")
        pkg.__path__ = [agent_dir]
        sys.modules["agent"] = pkg
    spec = importlib.util.spec_from_file_location(
        "agent.memory_provider", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["agent.memory_provider"] = module
    spec.loader.exec_module(module)
    return module


def _ensure_agent_dependency():
    if "agent.memory_provider" in sys.modules:
        return sys.modules["agent.memory_provider"]
    try:
        return importlib.import_module("agent.memory_provider")
    except ImportError as exc:
        root = os.environ.get(HERMES_AGENT_ROOT_ENV, "").strip()
        if root:
            loaded = _load_from_root(root)
            if loaded is not None:
                return loaded
        raise RuntimeError(
            _MISSING_DEPENDENCY % (exc, HERMES_AGENT_ROOT_ENV, HERMES_AGENT_ROOT_ENV)
        ) from exc


def _ensure_pkg():
    if PKG not in sys.modules:
        pkg = types.ModuleType(PKG)
        pkg.__path__ = [COG_DIR]
        sys.modules[PKG] = pkg


def load(name):
    """Load cognition module ``name`` (e.g. 'queue') by file path."""
    _ensure_agent_dependency()
    _ensure_pkg()
    full = PKG + "." + name
    if full in sys.modules:
        return sys.modules[full]
    path = os.path.join(COG_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location(full, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module
