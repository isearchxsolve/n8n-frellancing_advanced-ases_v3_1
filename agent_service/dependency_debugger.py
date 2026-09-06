"""
ASES - Dependency Debugger (Gap Fix #5)
=========================================
Solves: When a test fails because file A's interface changed and file B
wasn't updated, the raw error message only shows the symptom in file B,
not the cause in file A.

Problem in v2.5:
    Error: "TypeError: login is not a function at routes.js:14"
    Coder reads this and tries to fix routes.js — but the actual problem
    is that auth.js renamed login() to authenticate() in this iteration.

    The coder has no view of the dependency graph, so it:
    - Often "fixes" the wrong file
    - Introduces compensating hacks instead of fixing the root cause
    - Sometimes oscillates between broken states across iterations

Solution:
    DependencyDebugger enriches raw test error output with:
    1. Which file the error originated in
    2. Which files that file imports from
    3. Whether any of those dependency files changed in this iteration
    4. The specific interface change that likely caused the error

    The enriched error is passed as previous_errors to the coder,
    giving it the root cause, not just the symptom.

Integration:
    In agent_loop.py _dev_pipeline(), when test fails:

        enriched_errors = await dependency_debugger.enrich(
            error_output=test_results["stderr"] or test_results["stdout"],
            files=all_files,
            diff_report=diff_report,    # from SemanticDiffer (Fix #2)
            execution_id=execution_id,
        )
        previous_errors = enriched_errors

    DependencyDebugger can be used standalone (without SemanticDiffer)
    if you only want dependency graph annotation without regression detection.
"""

import re
from typing import Dict, Any, List, Optional, Set, Tuple

import structlog

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Error parsing — extract file + line from common test output formats
# ---------------------------------------------------------------------------

ERROR_PATTERNS = [
    # Node/Jest:  "at Object.<anonymous> (src/routes.js:14:5)"
    re.compile(r'at\s+\S+\s+\(([^)]+\.(?:js|ts|jsx|tsx)):(\d+)'),
    # Python:     "File "src/auth.py", line 42"
    re.compile(r'File\s+"([^"]+\.py)",\s+line\s+(\d+)'),
    # Go:         "FAIL: routes_test.go:28"
    re.compile(r'FAIL[:\s]+(\S+\.go):(\d+)'),
    # Rust:       "error[E0425]: thread.rs:15:5"
    re.compile(r'error\[E\d+\].*?(\S+\.rs):(\d+)'),
    # Generic:    "Error in src/db.js:77"
    re.compile(r'[Ee]rror\s+in\s+([^\s:]+\.(?:js|ts|py|go|rs)):(\d+)'),
]


def _extract_error_locations(error_text: str) -> List[Tuple[str, int]]:
    """Extract (file_path, line_number) from raw test output."""
    locations = []
    seen = set()
    for pattern in ERROR_PATTERNS:
        for match in pattern.finditer(error_text):
            path = match.group(1).strip()
            try:
                line = int(match.group(2))
            except (IndexError, ValueError):
                line = 0
            # Normalise path separators and strip workspace prefix
            path = path.replace("\\", "/").lstrip("/")
            for prefix in ("workspace/", "/workspace/", "./"):
                if path.startswith(prefix):
                    path = path[len(prefix):]
            key = (path, line)
            if key not in seen:
                seen.add(key)
                locations.append(key)
    return locations


# ---------------------------------------------------------------------------
# Dependency graph builder
# ---------------------------------------------------------------------------

def _build_dep_graph(files: List[Dict[str, str]]) -> Dict[str, Set[str]]:
    """
    Build a map of {file -> set of files it imports from}.
    Uses the same extraction logic as SemanticDiffer but standalone.
    """
    # Import locally to avoid circular dependency with semantic_differ.py
    try:
        from semantic_differ import _extract_interface
        graph: Dict[str, Set[str]] = {}
        for f in files:
            iface = _extract_interface(f["path"], f["content"])
            if iface is None:
                continue
            deps: Set[str] = set()
            for rel_module in iface.imports:
                # Resolve to best-guess file path
                base = "/".join(f["path"].split("/")[:-1])
                rel = rel_module.lstrip("./")
                for ext in (".js", ".ts", ".py", "/index.js"):
                    candidate = f"{base}/{rel}{ext}" if base else f"{rel}{ext}"
                    actual = next(
                        (x["path"] for x in files if x["path"] == candidate), None
                    )
                    if actual:
                        deps.add(actual)
                        break
            graph[f["path"]] = deps
        return graph
    except ImportError:
        return {}


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class DependencyDebugger:
    """
    Enriches raw test/runtime error messages with dependency graph context
    so the coder can identify root causes rather than just symptoms.
    """

    async def enrich(
        self,
        error_output: str,
        files: List[Dict[str, str]],
        execution_id: str,
        diff_report=None,        # Optional DiffReport from SemanticDiffer
        config=None,             # Optional TenantConfig for LLM root-cause
    ) -> str:
        """
        Returns an enriched error string:
        - Original error preserved
        - Dependency annotations prepended where relevant
        - Root cause hypothesis if diff_report available
        """
        if not error_output:
            return error_output

        dep_graph = _build_dep_graph(files)
        error_locations = _extract_error_locations(error_output)

        annotations: List[str] = []

        for error_path, error_line in error_locations[:5]:   # cap at 5 error sites
            # Find which files this erroring file imports from
            deps = dep_graph.get(error_path, set())
            if not deps:
                continue

            dep_list = sorted(deps)
            annotations.append(
                f"\nDEPENDENCY CONTEXT for {error_path}:{error_line}:"
            )
            annotations.append(f"  Imports from: {', '.join(dep_list)}")

            # Cross-reference with diff report if available
            if diff_report:
                changed_deps = [
                    d for d in dep_list
                    if any(cf.path == d and cf.interface_removed
                           for cf in diff_report.changed_files)
                ]
                if changed_deps:
                    annotations.append("  ⚠ These dependencies changed their interface this iteration:")
                    for dep in changed_deps:
                        changed_file = next(
                            cf for cf in diff_report.changed_files
                            if cf.path == dep
                        )
                        annotations.append(
                            f"    {dep}: removed={changed_file.interface_removed}, "
                            f"added={changed_file.interface_added}"
                        )
                    annotations.append(
                        "  → Fix the import in {error_path} to use the new interface, "
                        "OR restore the removed export in the dependency file."
                    )

        # LLM root-cause hypothesis (only when graph annotations found AND config available)
        if annotations and config and diff_report and diff_report.broken_imports:
            hypothesis = await self._llm_root_cause(
                error_output, dep_graph, diff_report, config, execution_id
            )
            if hypothesis:
                annotations.insert(0, f"\nROOT CAUSE HYPOTHESIS:\n  {hypothesis}\n")

        if not annotations:
            return error_output

        header = "=== DEPENDENCY ANALYSIS ==="
        footer = "=== END DEPENDENCY ANALYSIS ===\n\nORIGINAL ERROR OUTPUT:"
        return header + "\n".join(annotations) + "\n\n" + footer + "\n" + error_output

    async def _llm_root_cause(
        self,
        error_output: str,
        dep_graph: Dict[str, Set[str]],
        diff_report,
        config,
        execution_id: str,
    ) -> Optional[str]:
        """
        One-shot cheap LLM call to hypothesise the root cause when the
        dependency graph and diff report both have signals.
        """
        try:
            from agent_loop import call_model

            changed_summary = "\n".join(
                f"  {cf.path}: removed={cf.interface_removed}, added={cf.interface_added}"
                for cf in diff_report.changed_files
                if cf.interface_removed
            )[:600]

            broken_summary = "\n".join(diff_report.broken_imports[:3])[:400]

            prompt = f"""A test suite is failing. Based on the interface changes and error output below,
state the single most likely root cause in ONE sentence.

Interface changes this iteration:
{changed_summary or '(none detected)'}

Broken import relationships detected:
{broken_summary or '(none)'}

Error output (first 400 chars):
{error_output[:400]}

Respond with ONLY one sentence starting with "Root cause:"."""

            content, _, _ = await call_model(
                model=config.reviewer_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=120,
                execution_id=execution_id,
                call_type="reviewer",
            )
            return content.strip()
        except Exception as e:
            logger.warning("dep_debugger.llm_failed", error=str(e))
            return None
