# SPDX-License-Identifier: MIT
# __init__.py --- Plugin discovery
# Copyright (c) 2026 Jakob Kastelic

import importlib
import pkgutil


def load_all():
    """Import every sibling module and collect DevicePlugin subclasses.

    Returns ``{plugin.name: plugin_instance}``.
    """
    from plugin import DevicePlugin
    plugins = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{__name__}.{info.name}")
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if (isinstance(obj, type)
                    and obj is not DevicePlugin
                    and issubclass(obj, DevicePlugin)):
                inst = obj()
                if inst.name:
                    plugins[inst.name] = inst
    return plugins
