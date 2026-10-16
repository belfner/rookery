"""Program registry for auto-discovery and access."""

from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path

from rookery.program import Program


# Module-level cache
_registry_cache: dict[str, type[Program]] | None = None


def discover_programs() -> dict[str, type[Program]]:
    """
    Auto-discover program classes from programs/ directory.

    Scans all Python files in src/rookery/programs/
    and finds Program subclasses.

    Returns
    -------
    dict[str, Type[Program]]
        Mapping of program names to Program classes.
    """
    global _registry_cache
    if _registry_cache is not None:
        return _registry_cache

    registry: dict[str, type[Program]] = {}
    programs_dir = Path(__file__).parent / "programs"

    if not programs_dir.exists():
        programs_dir.mkdir(parents=True, exist_ok=True)

    for py_file in programs_dir.glob("*.py"):
        if py_file.name.startswith("_"):
            continue

        module_name = f"rookery.programs.{py_file.stem}"
        try:
            module = importlib.import_module(module_name)
        except Exception as e:
            # A broken plugin is reported and left out, so the rest of the catalog loads
            print(f"rookery: skipping {module_name}: {e}", file=sys.stderr)
            continue

        # Concrete Program subclasses defined in this module; imported base classes
        # belong to the module that defines them
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if not issubclass(obj, Program) or obj.__module__ != module_name or inspect.isabstract(obj):
                continue
            # Instantiate to get name attribute
            try:
                instance = obj()
            except Exception as e:
                print(f"rookery: skipping {module_name}.{obj.__name__}: {e}", file=sys.stderr)
                continue
            registry[instance.name] = obj

    _registry_cache = registry
    return registry


def get_program(name: str) -> Program:
    """
    Get program instance by name.

    Parameters
    ----------
    name : str
        Program name.

    Returns
    -------
    Program
        Program instance.

    Raises
    ------
    KeyError
        If program not found in registry.
    """
    registry = discover_programs()
    if name not in registry:
        raise KeyError(f"Program '{name}' not found in registry")
    return registry[name]()


def list_programs() -> list[Program]:
    """
    Get all registered programs.

    Returns
    -------
    list[Program]
        List of all program instances sorted by name.
    """
    registry = discover_programs()
    return sorted([cls() for cls in registry.values()], key=lambda p: p.name)
