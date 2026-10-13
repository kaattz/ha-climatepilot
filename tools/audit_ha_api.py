"""Audit ClimatePilot's Home Assistant API usage against a real HA release.

The integration is developed and tested against whatever HA is installed in the
local test venv, but it runs in production on a *newer* release. That gap is not
hypothetical: Home Assistant removes and relocates APIs between minor versions
(``ServiceCall`` left ``homeassistant.const``, ``DataUpdateCoordinator`` began
reporting the ContextVar config-entry pattern, ``EntityRegistry.entities`` was
reshaped), and every one of those broke this integration while the local suite
stayed green.

This script closes that gap without needing the target version installed: it
takes an extracted HA source tree and checks, symbol by symbol, that everything
ClimatePilot imports actually exists there.

Usage:
    python tools/audit_ha_api.py <path-to-extracted-homeassistant-parent>

The path should contain ``homeassistant/`` (i.e. an unpacked wheel), not
necessarily an importable environment - the checks are static (AST + module
attributes read from source where needed).
"""

from __future__ import annotations

import ast
import contextlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT = REPO_ROOT / "custom_components" / "climatepilot"


def collect_imports() -> list[tuple[str, str, str, int]]:
    """Return (module, symbol, file, line) for every HA import we make."""
    found: list[tuple[str, str, str, int]] = []
    for path in sorted(COMPONENT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if not node.module.startswith("homeassistant"):
                    continue
                for alias in node.names:
                    found.append((node.module, alias.name, path.name, node.lineno))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("homeassistant"):
                        found.append((alias.name, "", path.name, node.lineno))
    return found


def module_path(ha_root: Path, module: str) -> Path | None:
    """Resolve a dotted module to a file inside the extracted tree.

    ``ha_root`` already points at the ``homeassistant`` package directory, so the
    leading component of every dotted path is stripped before joining.
    """
    parts = module.split(".")
    if parts and parts[0] == "homeassistant":
        parts = parts[1:]
    if not parts:
        return ha_root / "__init__.py"

    base = ha_root.joinpath(*parts)
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    candidate = base.with_suffix(".py")
    if candidate.is_file():
        return candidate
    return None


def _parse(source: str) -> ast.Module | None:
    """Parse module source, tolerating syntax newer than the running Python.

    Home Assistant tracks the newest CPython, so a wheel for a release ahead of
    the local interpreter can contain syntax this process cannot parse - for
    example PEP 758's unparenthesised ``except X, Y:`` in ``config_entries.py``.
    That is a limitation of the *auditing* interpreter, not an incompatibility
    of the integration, so the caller falls back to textual checks rather than
    reporting a false failure.
    """
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def defined_names(source: str) -> tuple[set[str], set[str]]:
    """Return (top-level names, __all__ entries) defined in a module source.

    Falls back to a token scan when the source cannot be parsed by this
    interpreter, so a newer-syntax module still yields a usable name set.
    """
    tree = _parse(source)
    if tree is None:
        return _names_by_scan(source), set()

    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.TypeAlias):
            # PEP 695 ``type X = ...`` (used by Home Assistant core).
            if isinstance(node.name, ast.Name):
                names.add(node.name.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.If):
            # Conditional definitions, e.g. TYPE_CHECKING blocks.
            for sub in node.body + node.orelse:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(sub.name)

    exported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    with contextlib.suppress(ValueError, SyntaxError):
                        exported = set(ast.literal_eval(node.value))
    return names, exported


def _names_by_scan(source: str) -> set[str]:
    """Extract likely top-level names with a line scan.

    Only used for modules the running interpreter cannot parse. Home Assistant
    targets the newest CPython, so its core modules routinely use syntax ahead
    of the auditing interpreter - PEP 758's ``except X, Y:`` and PEP 695's
    ``type X = ...`` both appear in ``core.py`` for 2026.9.

    It is deliberately generous: the audit exists to catch a *missing* symbol,
    and a false "present" is far less harmful here than a false "missing"
    (which would send someone chasing an incompatibility that does not exist).
    """
    import re

    names: set[str] = set()
    for line in source.splitlines():
        # class / def / async def
        match = re.match(r"^(?:class|def|async def)\s+(\w+)", line)
        if match:
            names.add(match.group(1))
        # PEP 695 type alias: ``type Foo = ...``
        match = re.match(r"^type\s+(\w+)", line)
        if match:
            names.add(match.group(1))
        # plain or annotated assignment
        match = re.match(r"^(\w+)(?:\s*:\s*[^=]+)?\s*=", line)
        if match:
            names.add(match.group(1))
        # from x import a, b as c
        match = re.match(r"^from\s+\S+\s+import\s+(.+)$", line)
        if match:
            for part in match.group(1).split(","):
                token = part.strip().split(" as ")[-1].strip()
                if token.isidentifier():
                    names.add(token)
    return names


def reexported(ha_root: Path, module: str, symbol: str, seen: set[str]) -> bool:
    """Follow ``from x import y`` re-exports to see if ``symbol`` is reachable."""
    if module in seen:
        return False
    seen.add(module)

    path = module_path(ha_root, module)
    if path is None:
        return False

    source = path.read_text(encoding="utf-8", errors="replace")
    names, exported = defined_names(source)
    if symbol in names or symbol in exported:
        return True

    # Star imports and explicit re-exports.
    tree = _parse(source)
    if tree is None:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if any(a.name == "*" for a in node.names):
                if reexported(ha_root, node.module, symbol, seen):
                    return True
            elif any((a.asname or a.name) == symbol for a in node.names):
                # Resolve through the source module.
                full = node.module
                if node.level:
                    parent = module.rsplit(".", node.level)[0]
                    full = f"{parent}.{node.module}" if node.module else parent
                return reexported(ha_root, full, a_name(node, symbol), seen)
    return False


def a_name(node: ast.ImportFrom, symbol: str) -> str:
    for alias in node.names:
        if (alias.asname or alias.name) == symbol:
            return alias.name
    return symbol


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2

    ha_root = Path(argv[1]) / "homeassistant"
    if not ha_root.is_dir():
        print(f"not an extracted HA tree: {ha_root}")
        return 2

    imports = collect_imports()
    missing: list[tuple[str, str, str, int]] = []
    unresolved_modules: list[tuple[str, str, int]] = []

    for module, symbol, filename, lineno in imports:
        path = module_path(ha_root, module)
        if path is None:
            # ``from homeassistant.helpers import selector`` names a submodule,
            # not a symbol, so resolve those as modules rather than members.
            if symbol and module_path(ha_root, f"{module}.{symbol}") is not None:
                continue
            unresolved_modules.append((module, filename, lineno))
            continue
        if not symbol:
            continue
        if not reexported(ha_root, module, symbol, set()):
            # Same submodule case reached via a plain package __init__.
            if module_path(ha_root, f"{module}.{symbol}") is not None:
                continue
            missing.append((module, symbol, filename, lineno))

    print(f"HA tree      : {ha_root}")
    print(f"HA imports   : {len(imports)} across {len({i[2] for i in imports})} files")
    print()

    if unresolved_modules:
        print(f"MISSING MODULES ({len(unresolved_modules)}):")
        for module, filename, lineno in unresolved_modules:
            print(f"  {filename}:{lineno}  {module}")
        print()

    if missing:
        print(f"MISSING SYMBOLS ({len(missing)}):")
        for module, symbol, filename, lineno in missing:
            print(f"  {filename}:{lineno}  {module}.{symbol}")
        print()

    ok = not missing and not unresolved_modules
    print("VERDICT:", "all HA APIs present" if ok else "INCOMPATIBLE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
