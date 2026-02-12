"""Program registry for auto-discovery and access."""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

from roost.program import Program


# Module-level cache
_registry_cache: dict[str, type[Program]] | None = None


def discover_programs() -> dict[str, type[Program]]:
    """
    Auto-discover program classes from programs/ directory.

    Scans all Python files in src/roost/programs/
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

        module_name = f"roost.programs.{py_file.stem}"
        try:
            module = importlib.import_module(module_name)

            # Find Program subclasses in module
            for _name, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, Program) and obj is not Program:
                    # Instantiate to get name attribute
                    try:
                        instance = obj()
                        registry[instance.name] = obj
                    except Exception:
                        # Skip classes that can't be instantiated
                        continue
        except Exception:
            # Skip modules that can't be imported
            continue

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
