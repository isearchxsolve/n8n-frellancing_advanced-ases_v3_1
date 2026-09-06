"""
ASES - Semantic Differ (Gap Fix #2)
=====================================
Solves: Silent regressions when file B breaks because file A's interface changed.

Problem in v2.5:
    All files are replaced wholesale on every iteration. If iteration 2 fixes
    a failing test by modifying auth.js but accidentally changes the function
    signature that routes.js depends on, the test might still fail — but the
    error message says "TypeError: login is not a function" with no indication
    that routes.js is calling a now-renamed function.

    The coder gets the error but has no context about WHAT CHANGED between
    iterations to cause it. It often re-introduces the same regression.

Solution:
    SemanticDiffer runs between iterations and produces:
    1. A file-level change summary (which files changed, how much)
    2. Interface-level diff for JS/Python (exported functions, classes, signatures)
    3. A dependency graph mapping which files import from which
    4. A regression annotation: "routes.js imports login() from auth.js,
       which was renamed to authenticate() in iteration 2"

    This annotation is prepended to previous_errors so the coder knows exactly
    which cross-file relationship broke.

Integration:
    In agent_loop.py _dev_pipeline(), after test failure:

        if not test_results["success"] and len(journal.records) > 1:
            diff_report = differ.diff(prev_files, current_files)
            regression_note = diff_report.regression_annotation(
                test_results["stderr"]
            )
            if regression_note:
                previous_errors = regression_note + "\\n\\n" + previous_errors
"""

import ast
import re
import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import structlog

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FileInterface:
    """Public interface of a source file — exported names and signatures."""
    path: str
    exports: List[str]          # function/class names exported or defined at module level
    imports: Dict[str, List[str]]  # {module_path: [imported_names]}


@dataclass
class FileDiff:
    path: str
    changed: bool
    lines_before: int
    lines_after: int
    change_ratio: float         # 0.0 = identical, 1.0 = completely rewritten
    interface_removed: List[str]   # names that existed before but not now
    interface_added: List[str]     # names that are new


@dataclass
class DiffReport:
    changed_files: List[FileDiff]
    new_files: List[str]
    deleted_files: List[str]
    dependency_graph: Dict[str, Set[str]]   # {file: set of files it imports from}
    broken_imports: List[str]               # human-readable regression annotations

    def regression_annotation(self, test_stderr: str) -> str:
        """
        Produces a coder-readable annotation explaining likely cross-file
        regressions based on the diff + dependency graph + test error.
        """
        if not self.broken_imports:
            return ""

        lines = ["CROSS-FILE REGRESSION DETECTED — fix these relationships:"]
        for note in self.broken_imports:
            lines.append(f"  • {note}")

        lines.append("")
        lines.append("These regressions were caused by interface changes between iterations.")
        lines.append("Fix the interface mismatch, not just the symptom in the error below.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Interface extraction
# ---------------------------------------------------------------------------

def _extract_python_interface(path: str, content: str) -> FileInterface:
    exports = []
    imports: Dict[str, List[str]] = {}
    try:
        tree = ast.parse(content, filename=path)
        for node in ast.walk(tree):
            # Top-level function and class definitions
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not node.name.startswith("_"):   # skip private
                    exports.append(node.name)
            # Import statements
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("."):
                    mod = node.module
                    names = [alias.name for alias in node.names]
                    imports.setdefault(mod, []).extend(names)
    except SyntaxError:
        pass
    return FileInterface(path=path, exports=exports, imports=imports)


def _extract_js_interface(path: str, content: str) -> FileInterface:
    exports = []
    imports: Dict[str, List[str]] = {}

    # Named exports: export function foo, export const foo, export class Foo
    export_pattern = re.compile(
        r'export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|let|var)\s+(\w+)'
    )
    exports = export_pattern.findall(content)

    # module.exports = { foo, bar }
    module_exports = re.findall(r'module\.exports\s*=\s*\{([^}]+)\}', content)
    for block in module_exports:
        exports += [n.strip().split(":")[0].strip() for n in block.split(",") if n.strip()]

    # require() imports
    require_pattern = re.compile(
        r'(?:const|let|var)\s+\{([^}]+)\}\s*=\s*require\([\'"]([^"\']+)[\'"]\)'
    )
    for names_block, module in require_pattern.findall(content):
        names = [n.strip() for n in names_block.split(",") if n.strip()]
        rel = module if module.startswith(".") else None
        if rel:
            imports.setdefault(rel, []).extend(names)

    # ES import { foo } from './bar'
    es_import = re.compile(
        r'import\s+\{([^}]+)\}\s+from\s+[\'"]([^"\']+)[\'"]'
    )
    for names_block, module in es_import.findall(content):
        names = [n.strip().split(" as ")[0].strip() for n in names_block.split(",") if n.strip()]
        if module.startswith("."):
            imports.setdefault(module, []).extend(names)

    return FileInterface(path=path, exports=list(set(exports)), imports=imports)


def _extract_interface(path: str, content: str) -> Optional[FileInterface]:
    if path.endswith(".py"):
        return _extract_python_interface(path, content)
    if path.endswith((".js", ".ts", ".jsx", ".tsx")):
        return _extract_js_interface(path, content)
    return None


# ---------------------------------------------------------------------------
# Main differ
# ---------------------------------------------------------------------------

class SemanticDiffer:
    """
    Compares two snapshots of a file set and identifies interface-level
    regressions between them.

    Usage:
        differ = SemanticDiffer()
        report = differ.diff(files_iteration_1, files_iteration_2)
        annotation = report.regression_annotation(test_stderr)
    """

    def diff(
        self,
        before: List[Dict[str, str]],
        after: List[Dict[str, str]],
    ) -> DiffReport:
        before_map = {f["path"]: f["content"] for f in before}
        after_map  = {f["path"]: f["content"] for f in after}

        before_ifaces = {
            p: _extract_interface(p, c)
            for p, c in before_map.items()
        }
        after_ifaces = {
            p: _extract_interface(p, c)
            for p, c in after_map.items()
        }

        # File-level diffs
        changed_files: List[FileDiff] = []
        for path in set(before_map) | set(after_map):
            b_content = before_map.get(path, "")
            a_content = after_map.get(path, "")
            if b_content == a_content:
                continue

            b_lines = b_content.splitlines()
            a_lines = a_content.splitlines()
            b_hash = set(b_lines)
            a_hash = set(a_lines)
            changed_ratio = len(b_hash.symmetric_difference(a_hash)) / max(len(b_hash | a_hash), 1)

            b_iface = before_ifaces.get(path)
            a_iface = after_ifaces.get(path)
            removed = []
            added = []
            if b_iface and a_iface:
                b_exports = set(b_iface.exports)
                a_exports = set(a_iface.exports)
                removed = list(b_exports - a_exports)
                added   = list(a_exports - b_exports)

            changed_files.append(FileDiff(
                path=path,
                changed=True,
                lines_before=len(b_lines),
                lines_after=len(a_lines),
                change_ratio=round(changed_ratio, 2),
                interface_removed=removed,
                interface_added=added,
            ))

        new_files = [p for p in after_map if p not in before_map]
        deleted_files = [p for p in before_map if p not in after_map]

        # Build dependency graph from the AFTER snapshot
        dep_graph: Dict[str, Set[str]] = {}
        for path, iface in after_ifaces.items():
            if iface is None:
                continue
            dep_graph[path] = set()
            for rel_module in iface.imports:
                # Resolve relative path
                resolved = self._resolve_relative(path, rel_module)
                if resolved:
                    dep_graph[path].add(resolved)

        # Detect broken imports
        broken: List[str] = []
        for diff in changed_files:
            if not diff.interface_removed:
                continue
            # Find files in the AFTER snapshot that depend on this file
            dependents = [
                p for p, deps in dep_graph.items()
                if diff.path in deps and p != diff.path
            ]
            if not dependents:
                continue
            for removed_name in diff.interface_removed:
                # Check if any dependent imports this removed name
                for dep_path in dependents:
                    dep_iface = after_ifaces.get(dep_path)
                    if dep_iface is None:
                        continue
                    for mod, names in dep_iface.imports.items():
                        if removed_name in names:
                            broken.append(
                                f"{dep_path} imports `{removed_name}` from {diff.path}, "
                                f"but `{removed_name}` was removed/renamed in this iteration. "
                                f"New exports from {diff.path}: {diff.interface_added or ['(none)']}"
                            )

        logger.info(
            "differ.complete",
            changed=len(changed_files),
            new=len(new_files),
            deleted=len(deleted_files),
            broken_imports=len(broken),
        )

        return DiffReport(
            changed_files=changed_files,
            new_files=new_files,
            deleted_files=deleted_files,
            dependency_graph=dep_graph,
            broken_imports=broken,
        )

    def _resolve_relative(self, source_path: str, rel_import: str) -> Optional[str]:
        """Resolve a relative import string to an absolute file path guess."""
        parts = source_path.split("/")
        base = "/".join(parts[:-1]) if len(parts) > 1 else ""
        rel = rel_import.lstrip("./")
        candidates = [
            f"{base}/{rel}.js",
            f"{base}/{rel}.ts",
            f"{base}/{rel}.py",
            f"{base}/{rel}/index.js",
        ]
        return candidates[0]   # return best guess; exact match not required
