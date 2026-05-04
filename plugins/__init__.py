# SPDX-License-Identifier: MIT
# __init__.py --- Plugin discovery
# Copyright (c) 2026 Jakob Kastelic

import importlib
import pkgutil
import sys


def load_all(reload=False):
    """Import every sibling module and collect DevicePlugin subclasses.

    Returns ``{plugin.name: plugin_instance}``.

    Pass ``reload=True`` to actually re-execute already-loaded plugin
    modules from disk (used by SIGHUP). Without that, Python's import
    cache returns the prior version of the file and the operator's
    edits aren't picked up.
    """
    from plugin import DevicePlugin
    plugins = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        modname = f"{__name__}.{info.name}"
        if reload and modname in sys.modules:
            mod = importlib.reload(sys.modules[modname])
        else:
            mod = importlib.import_module(modname)
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if (isinstance(obj, type)
                    and obj is not DevicePlugin
                    and issubclass(obj, DevicePlugin)):
                inst = obj()
                if inst.name:
                    plugins[inst.name] = inst
    return plugins
