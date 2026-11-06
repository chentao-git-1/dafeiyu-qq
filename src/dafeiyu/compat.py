"""Compatibility for old helper imports; never registers an AstrBot plugin."""
from importlib import import_module
import sys


def alias_module(legacy_name, canonical_name, namespace):
    """Use one module object so old monkeypatches and shared state still work.

    Export names as well for tools which execute a file spec directly rather
    than returning sys.modules[name]. New code must import the canonical module.
    """
    module = import_module(canonical_name)
    namespace.update({key: value for key, value in vars(module).items()
                      if not key.startswith("__")})
    sys.modules[legacy_name] = module
