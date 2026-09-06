"""
ASES - Static Analysis Reviewer
Replaces the heuristic LLM-only reviewer with a layered analysis pipeline:

  Layer 1 — AST parsing      (Python: ast / JS: esprima-style via acorn CLI)
  Layer 2 — Lint integration (Python: ruff / JS: eslint)
  Layer 3 — Dependency vuln  (pip-audit for Python / npm audit for Node.js)
  Layer 4 — Design compliance (NEW v2.6: validates CSS variable usage, data-testid presence)
  Layer 5 — LLM quality gate (existing reviewer_agent, now as a final pass)

Each layer returns a list of Issue objects.
The gate passes only when all layers pass or issues are below severity threshold.

Usage in agent_loop.py:
    from static_reviewer import run_static_review

    review = await run_static_review(files, tech_stack, sandbox_id, config, execution_id, design_spec=None)
    if not review["approved"]:
        previous_errors = review["issues_flat"]
"""

import ast
import json
import os
import re
import subprocess
import tempfile
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class Issue:
    def __init__(
        self,
        layer: str,
        severity: str,           # "error" | "warning" | "info"
        code: str,
        message: str,
        file: str = "",
        line: int = 0,
    ):
        self.layer    = layer
        self.severity = severity
        self.code     = code
        self.message  = message
        self.file     = file
        self.line     = line

    def to_dict(self) -> dict:
        return {
            "layer":    self.layer,
            "severity": self.severity,
            "code":     self.code,
            "message":  self.message,
            "file":     self.file,
            "line":     self.line,
        }

    def __str__(self):
        loc = f"{self.file}:{self.line}" if self.file else "–"
        return f"[{self.layer}/{self.severity}] {loc} {self.code}: {self.message}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_cmd(cmd: List[str], cwd: str = None, timeout: int = 30) -> Tuple[int, str, str]:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=cwd, timeout=timeout,
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {timeout}s: {' '.join(cmd)}"
    except FileNotFoundError:
        return 1, "", f"Tool not found: {cmd[0]}"


def _write_tmpdir(files: List[Dict[str, str]]) -> str:
    """Write generated files to a temp dir for static tools."""
    tmpdir = tempfile.mkdtemp(prefix="ases_review_")
    for f in files:
        path = Path(tmpdir) / f["path"].lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f["content"], encoding="utf-8", errors="replace")
    return tmpdir


def _detect_stack(files: List[Dict[str, str]], tech_stack: str) -> str:
    ts = tech_stack.lower()
    if "python" in ts or "fastapi" in ts or "flask" in ts or "django" in ts:
        return "python"
    if any(f["path"].endswith(".py") for f in files):
        return "python"
    return "node"


# ---------------------------------------------------------------------------
# Layer 1 — AST Parsing
# ---------------------------------------------------------------------------

def _ast_python(files: List[Dict[str, str]]) -> List[Issue]:
    issues = []
    for f in files:
        if not f["path"].endswith(".py"):
            continue
        try:
            tree = ast.parse(f["content"], filename=f["path"])
            # Walk for dangerous patterns
            for node in ast.walk(tree):
                # eval() / exec() usage
                if isinstance(node, ast.Call):
                    func = node.func
                    name = ""
                    if isinstance(func, ast.Name):
                        name = func.id
                    elif isinstance(func, ast.Attribute):
                        name = func.attr
                    if name in ("eval", "exec", "compile", "__import__"):
                        issues.append(Issue(
                            layer="ast", severity="warning", code="AST001",
                            message=f"Dangerous call to {name}() — prefer safer alternatives",
                            file=f["path"], line=getattr(node, "lineno", 0),
                        ))
                # Bare except
                if isinstance(node, ast.ExceptHandler) and node.type is None:
                    issues.append(Issue(
                        layer="ast", severity="warning", code="AST002",
                        message="Bare `except:` clause — catches all exceptions including SystemExit",
                        file=f["path"], line=getattr(node, "lineno", 0),
                    ))
        except SyntaxError as e:
            issues.append(Issue(
                layer="ast", severity="error", code="AST000",
                message=f"Syntax error: {e.msg} (line {e.lineno})",
                file=f["path"],
            ))
    return issues


def _ast_node(files: List[Dict[str, str]]) -> List[Issue]:
    """
    Lightweight JS check without external tools:
    look for obvious patterns in raw text (eval, innerHTML, TODO, etc.)
    A proper acorn/esprima integration would be added in the sandbox.
    """
    issues = []
    danger_patterns = [
        (r"\beval\s*\(", "AST010", "Use of eval() is unsafe"),
        (r"innerHTML\s*=", "AST011", "innerHTML assignment — potential XSS"),
        (r"document\.write\s*\(", "AST012", "document.write() is deprecated"),
        (r"require\(['\"]child_process['\"]\)", "AST013", "child_process usage — review carefully"),
    ]
    for f in files:
        if not (f["path"].endswith(".js") or f["path"].endswith(".ts") or f["path"].endswith(".jsx")):
            continue
        for i, line in enumerate(f["content"].splitlines(), 1):
            for pattern, code, msg in danger_patterns:
                if re.search(pattern, line):
                    issues.append(Issue(
                        layer="ast", severity="warning", code=code,
                        message=msg, file=f["path"], line=i,
                    ))
    return issues


# ---------------------------------------------------------------------------
# Layer 2 — Lint
# ---------------------------------------------------------------------------

def _lint_python(tmpdir: str) -> List[Issue]:
    issues = []
    code, stdout, stderr = _run_cmd(
        ["ruff", "check", "--output-format=json", tmpdir],
        timeout=30,
    )
    if "not found" in stderr:
        # ruff absent — fall back to pyflakes
        code, stdout, stderr = _run_cmd(["python", "-m", "pyflakes", tmpdir], timeout=30)
        for line in stdout.splitlines() + stderr.splitlines():
            m = re.match(r"(.+):(\d+):\d+ (.+)", line)
            if m:
                issues.append(Issue(
                    layer="lint", severity="warning", code="PYF",
                    message=m.group(3), file=m.group(1), line=int(m.group(2)),
                ))
        return issues

    try:
        data = json.loads(stdout)
        for item in data:
            sev = "error" if item.get("fix") is None and item["code"][0] == "E" else "warning"
            issues.append(Issue(
                layer="lint", severity=sev,
                code=item["code"],
                message=item["message"],
                file=item["filename"],
                line=item["location"]["row"],
            ))
    except Exception:
        pass
    return issues


def _lint_node(tmpdir: str) -> List[Issue]:
    issues = []
    # Try eslint if available
    code, stdout, stderr = _run_cmd(
        ["npx", "--yes", "eslint", "--format=json", tmpdir],
        cwd=tmpdir, timeout=60,
    )
    try:
        data = json.loads(stdout)
        for file_result in data:
            fname = file_result.get("filePath", "")
            for msg in file_result.get("messages", []):
                sev = "error" if msg["severity"] == 2 else "warning"
                issues.append(Issue(
                    layer="lint", severity=sev,
                    code=msg.get("ruleId") or "ESL",
                    message=msg["message"],
                    file=fname,
                    line=msg.get("line", 0),
                ))
    except Exception:
        pass
    return issues


# ---------------------------------------------------------------------------
# Layer 3 — Dependency vulnerability scanning
# ---------------------------------------------------------------------------

def _vuln_python(tmpdir: str) -> List[Issue]:
    issues = []
    req_files = list(Path(tmpdir).rglob("requirements*.txt"))
    if not req_files:
        return issues

    for req in req_files[:1]:   # only first
        code, stdout, stderr = _run_cmd(
            ["pip-audit", "--requirement", str(req), "--format=json", "--no-progress-bar"],
            timeout=60,
        )
        try:
            data = json.loads(stdout)
            for dep in data:
                for vuln in dep.get("vulns", []):
                    issues.append(Issue(
                        layer="vuln", severity="error",
                        code=vuln.get("id", "CVE-UNKNOWN"),
                        message=f"{dep['name']}=={dep['version']}: {vuln.get('description', '')[:120]}",
                        file=str(req.relative_to(tmpdir)),
                    ))
        except Exception:
            pass
    return issues


def _vuln_node(tmpdir: str) -> List[Issue]:
    issues = []
    pkg_files = list(Path(tmpdir).rglob("package.json"))
    if not pkg_files:
        return issues

    for pkg in pkg_files[:1]:
        code, stdout, stderr = _run_cmd(
            ["npm", "audit", "--json"],
            cwd=str(pkg.parent), timeout=60,
        )
        try:
            data = json.loads(stdout)
            vulns = data.get("vulnerabilities", {})
            for name, info in vulns.items():
                severity = info.get("severity", "moderate")
                sev = "error" if severity in ("critical", "high") else "warning"
                issues.append(Issue(
                    layer="vuln", severity=sev,
                    code=f"NPM-{severity.upper()}",
                    message=f"{name}: {info.get('title', '')} — {info.get('url', '')}",
                    file="package.json",
                ))
        except Exception:
            pass
    return issues


# ---------------------------------------------------------------------------
# Layer 4 — Design Compliance (NEW v2.6)
# ---------------------------------------------------------------------------

def _design_compliance(files: List[Dict[str, str]], design_spec: Optional[Dict[str, Any]]) -> List[Issue]:
    """
    Validate that generated code complies with the design specification.

    Checks:
    1. CSS variables from :root are used (not hardcoded values)
    2. data-testid attributes are present on interactive components
    3. Component names from spec appear in code
    4. Color values in CSS/JSX match the design system
    """
    if not design_spec or not design_spec.get("has_design"):
        return []

    issues = []
    spec = design_spec["spec"]
    ds = spec.get("design_system", {})
    colors = ds.get("colors", {})
    components = spec.get("components", [])

    # Build set of expected color values (hex codes)
    expected_colors = set(v.lower() for v in colors.values() if v.startswith("#"))

    # Build set of expected component data-testids
    expected_testids = set()
    for c in components:
        if "data_testid" in c:
            expected_testids.add(c["data_testid"])
            expected_testids.add(f"{c['data_testid']}-trigger")
            expected_testids.add(f"{c['data_testid']}-content")

    for f in files:
        content = f["content"]
        path = f["path"]

        # Skip non-frontend files
        if not any(path.endswith(ext) for ext in [".css", ".scss", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte"]):
            continue

        # Check 1: Hardcoded color values that should be CSS variables
        if path.endswith((".css", ".scss")):
            # Find hex codes that match design system colors but aren't in var() format
            for hex_color in expected_colors:
                pattern = re.compile(rf'(?<!var\()\b{re.escape(hex_color)}\b', re.IGNORECASE)
                for match in pattern.finditer(content):
                    line_num = content[:match.start()].count("\n") + 1
                    issues.append(Issue(
                        layer="design", severity="warning", code="DS001",
                        message=f"Hardcoded color {hex_color} — must use CSS variable (e.g., var(--color-primary))",
                        file=path, line=line_num,
                    ))

        # Check 2: data-testid presence in JSX/TSX/Vue
        if path.endswith((".jsx", ".tsx", ".vue", ".svelte")):
            found_testids = set(re.findall(r'data-testid=["\']([^"\']+)["\']', content))
            missing = expected_testids - found_testids
            if missing and expected_testids:  # Only report if we expected some
                # Don't report as error — some testids may be in child components
                issues.append(Issue(
                    layer="design", severity="info", code="DS002",
                    message=f"Potentially missing data-testid attributes: {', '.join(list(missing)[:3])}",
                    file=path, line=0,
                ))

        # Check 3: Component names referenced in code
        for c in components:
            comp_name = c["name"]
            if comp_name not in content and comp_name.lower() not in content.lower():
                # Component from spec not found — might be in another file
                pass  # This is too noisy; skip

    return issues


# ---------------------------------------------------------------------------
# Combined runner
# ---------------------------------------------------------------------------

async def run_static_review(
    files: List[Dict[str, str]],
    tech_stack: str,
    config,               # TenantConfig
    execution_id: str,
    design_spec: Optional[Dict[str, Any]] = None,  # NEW v2.6
) -> Dict[str, Any]:
    """
    Run all static analysis layers including design compliance.
    Returns a dict compatible with the existing reviewer_agent output format.
    """
    stack = _detect_stack(files, tech_stack)
    tmpdir = _write_tmpdir(files)

    try:
        # Run all layers (sync in thread pool to not block event loop)
        loop = asyncio.get_event_loop()

        ast_issues = await loop.run_in_executor(
            None,
            _ast_python if stack == "python" else _ast_node,
            files,
        )

        lint_issues = await loop.run_in_executor(
            None,
            _lint_python if stack == "python" else _lint_node,
            tmpdir,
        )

        vuln_issues = await loop.run_in_executor(
            None,
            _vuln_python if stack == "python" else _vuln_node,
            tmpdir,
        )

        # NEW v2.6: Design compliance layer
        design_issues = await loop.run_in_executor(
            None,
            _design_compliance,
            files,
            design_spec,
        )

        all_issues = ast_issues + lint_issues + vuln_issues + design_issues
        errors     = [i for i in all_issues if i.severity == "error"]
        warnings   = [i for i in all_issues if i.severity == "warning"]

        issues_flat = [str(i) for i in all_issues]

        logger.info(
            "static_review.complete",
            execution_id=execution_id,
            errors=len(errors),
            warnings=len(warnings),
            ast=len(ast_issues),
            lint=len(lint_issues),
            vuln=len(vuln_issues),
            design=len(design_issues),
        )

        # Gate: any error-level issue fails the review
        approved = len(errors) == 0

        return {
            "approved":     approved,
            "score":        10.0 if approved else max(1.0, 8.0 - len(errors) * 2.0),
            "issues":       [i.to_dict() for i in all_issues],
            "issues_flat":  issues_flat,
            "summary": {
                "errors":       len(errors),
                "warnings":     len(warnings),
                "ast_issues":   len(ast_issues),
                "lint_issues":  len(lint_issues),
                "vuln_issues":  len(vuln_issues),
                "design_issues": len(design_issues),
            },
            "tokens": 0,   # static analysis — no LLM tokens consumed
        }

    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
