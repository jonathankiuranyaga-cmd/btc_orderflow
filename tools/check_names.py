"""Scope-aware undefined-name and unused-import checker.

Uses deferred binding (like pyflakes): every scope is fully populated with its
bindings before any name is resolved, so a function may reference a name that
is defined later in the source.

Usage:
    python tools/check_names.py app.py backtest.py forecast.py

Reports:
  - UNDEFINED: a name is read but never bound in any enclosing scope
  - UNUSED-IMPORT: an imported name is never referenced
Exit code is 1 when any UNDEFINED is found.
"""

from __future__ import annotations

import ast
import builtins
import sys
from pathlib import Path

BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__"}


class Scope:
    def __init__(self, kind: str, parent: "Scope | None" = None) -> None:
        self.kind = kind
        self.parent = parent
        self.names: set[str] = set()
        self.globals: set[str] = set()

    def resolve(self, name: str) -> bool:
        scope: Scope | None = self
        while scope is not None:
            if name in scope.names or name in scope.globals:
                return True
            scope = scope.parent
        return False


def build_parents(tree: ast.AST) -> dict[int, ast.AST]:
    """Map id(child) -> parent node so scope nesting can be resolved."""
    parents: dict[int, ast.AST] = {}
    stack: list[tuple[ast.AST, ast.AST | None]] = [(tree, None)]
    while stack:
        node, parent = stack.pop()
        if parent is not None:
            parents[id(node)] = parent
        for child in ast.iter_child_nodes(node):
            stack.append((child, node))
    return parents


class Collector(ast.NodeVisitor):
    """Phase 1: create scopes and bind every name each scope introduces."""

    def __init__(self) -> None:
        self.module = Scope("module")
        self.scopes: dict[int, Scope] = {0: self.module}
        self.order: list[Scope] = [self.module]
        self.parents: dict[int, ast.AST] = {}

    def new_scope(self, node: ast.AST, kind: str) -> Scope:
        scope = Scope(kind, self.enclosing_scope(node))
        self.scopes[id(node)] = scope
        self.order.append(scope)
        return scope

    def enclosing_scope(self, node: ast.AST) -> Scope:
        current = self.parents.get(id(node))
        while current is not None:
            scope = self.scopes.get(id(current))
            if scope is not None:
                return scope
            current = self.parents.get(id(current))
        return self.module

    @staticmethod
    def bind_target(target: ast.AST, scope: Scope) -> None:
        if isinstance(target, ast.Name):
            scope.names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                Collector.bind_target(element, scope)
        elif isinstance(target, ast.Starred):
            Collector.bind_target(target.value, scope)

    def bind_body(self, body: list[ast.stmt], scope: Scope) -> None:
        for statement in body:
            self.bind_statement(statement, scope)

    def bind_statement(self, node: ast.stmt, scope: Scope) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope.names.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                scope.names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                scope.names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                self.bind_target(target, scope)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            self.bind_target(node.target, scope)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            self.bind_target(node.target, scope)
            self.bind_body(node.body, scope)
            self.bind_body(node.orelse, scope)
        elif isinstance(node, (ast.While, ast.If)):
            self.bind_body(node.body, scope)
            self.bind_body(node.orelse, scope)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    self.bind_target(item.optional_vars, scope)
            self.bind_body(node.body, scope)
        elif isinstance(node, ast.Try):
            self.bind_body(node.body, scope)
            for handler in node.handlers:
                if handler.name:
                    scope.names.add(handler.name)
                self.bind_body(handler.body, scope)
            self.bind_body(node.orelse, scope)
            self.bind_body(node.finalbody, scope)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.bind_body(node.body, scope)

    def visit_Module(self, node: ast.Module) -> None:
        self.bind_body(node.body, self.module)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        scope = self.new_scope(node, "function")
        scope.names.update(a.arg for a in node.args.args)
        scope.names.update(a.arg for a in node.args.kwonlyargs)
        if node.args.vararg:
            scope.names.add(node.args.vararg.arg)
        if node.args.kwarg:
            scope.names.add(node.args.kwarg.arg)
        self.bind_body(node.body, scope)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        scope = self.new_scope(node, "class")
        self.bind_body(node.body, scope)
        self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        scope = self.new_scope(node, "function")
        scope.names.update(a.arg for a in node.args.args)
        if node.args.vararg:
            scope.names.add(node.args.vararg.arg)
        if node.args.kwarg:
            scope.names.add(node.args.kwarg.arg)
        self.generic_visit(node)

    def _visit_comprehension(self, node: ast.AST) -> None:
        scope = self.new_scope(node, "comprehension")
        for generator in node.generators:
            self.bind_target(generator.target, scope)
        self.generic_visit(node)

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension
    visit_DictComp = _visit_comprehension


class Resolver(ast.NodeVisitor):
    """Phase 2: resolve every name read against the collected scopes."""

    def __init__(self, collector: Collector) -> None:
        self.collector = collector
        self.scope = collector.module
        self.problems: list[tuple[int, str]] = []

    def scope_for(self, node: ast.AST) -> Scope:
        return self.collector.scopes.get(id(node), self.collector.module)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in node.args.defaults + [d for d in node.args.kw_defaults if d]:
            self.visit(default)
        outer = self.scope
        self.scope = self.scope_for(node)
        for statement in node.body:
            self.visit(statement)
        self.scope = outer

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        outer = self.scope
        self.scope = self.scope_for(node)
        for statement in node.body:
            self.visit(statement)
        self.scope = outer

    def visit_Lambda(self, node: ast.Lambda) -> None:
        outer = self.scope
        self.scope = self.scope_for(node)
        self.visit(node.body)
        self.scope = outer

    def _visit_comprehension(self, node: ast.AST) -> None:
        outer = self.scope
        self.scope = self.scope_for(node)
        self.generic_visit(node)
        self.scope = outer

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension
    visit_DictComp = _visit_comprehension

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            if node.id in BUILTINS:
                return
            if not self.scope.resolve(node.id):
                self.problems.append((node.lineno, node.id))

    def visit_Global(self, node: ast.Global) -> None:
        self.scope.globals.update(node.names)


def check(path: Path) -> int:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    collector = Collector()
    collector.parents = build_parents(tree)
    collector.visit(tree)

    resolver = Resolver(collector)
    resolver.visit(tree)

    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            base = node
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                used.add(base.id)
        elif isinstance(node, ast.alias):
            used.discard(None)

    undefined = 0
    seen: set[tuple[int, str]] = set()
    for lineno, name in resolver.problems:
        if (lineno, name) in seen:
            continue
        seen.add((lineno, name))
        print(f"{path}:{lineno}: UNDEFINED: name '{name}' is not defined")
        undefined += 1

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound not in used:
                    print(f"{path}:{node.lineno}: UNUSED-IMPORT: '{alias.name}' imported but never used")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                if bound not in used and node.module != "__future__":
                    source_name = f"{node.module}.{alias.name}"
                    print(f"{path}:{node.lineno}: UNUSED-IMPORT: '{source_name}' imported but never used")

    return undefined


def main() -> int:
    targets = [Path(arg) for arg in sys.argv[1:]] or [Path("app.py")]
    total = 0
    for target in targets:
        total += check(target)
    if total:
        print(f"\n{total} undefined name(s) found")
        return 1
    print("No undefined names found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
