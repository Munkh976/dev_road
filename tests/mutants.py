"""Deliberate breaks: load a module with one line of its source changed.

A test that passes against correct code proves little until it is also seen to
fail against code that is wrong in exactly the way it claims to guard against.
`load_mutant` gives that wrong version without editing the repo, and refuses
to run if the text it was told to break is not there (a silently unapplied
mutation would make the "breaks" test vacuous).
"""

from __future__ import annotations

import importlib.util
import itertools
import sys
import types
from pathlib import Path

_counter = itertools.count()


def load_mutant(module_name: str, old: str, new: str) -> types.ModuleType:
    spec = importlib.util.find_spec(module_name)
    path = Path(spec.origin)
    source = path.read_text(encoding="utf-8")
    assert source.count(old) == 1, (
        f"mutation target must appear exactly once in {module_name}: {old!r} "
        f"(found {source.count(old)})"
    )
    name = f"{module_name}__mutant{next(_counter)}"
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module          # dataclasses resolve annotations via sys.modules
    try:
        exec(compile(source.replace(old, new), str(path), "exec"), module.__dict__)
    finally:
        sys.modules.pop(name, None)
    return module
