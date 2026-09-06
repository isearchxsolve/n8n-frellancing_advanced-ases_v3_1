"""
ASES - Code Extraction Parser
Extracts FILE: blocks from LLM output into structured file objects.
"""

import re
from typing import List, Dict, Any


def extract_files(text: str) -> List[Dict[str, str]]:
    """
    Extracts file blocks from LLM output.

    Supports formats:
    FILE: path/to/file.js
    ```javascript
    code
    ```

    Or:
    ### file: path/to/file.js
    ```js
    code
    ```
    """
    files = []

    # Pattern 1: FILE: path
```lang
code
```
    pattern1 = r'FILE:\s*(.+?)\n```(?:\w+)?\n(.*?)```'
    matches1 = re.findall(pattern1, text, re.DOTALL)

    for path, content in matches1:
        files.append({
            "path": path.strip(),
            "content": content.strip()
        })

    # Pattern 2: ### file: path
```lang
code
```
    pattern2 = r'###\s*file:\s*(.+?)\n```(?:\w+)?\n(.*?)```'
    matches2 = re.findall(pattern2, text, re.DOTALL)

    for path, content in matches2:
        files.append({
            "path": path.strip(),
            "content": content.strip()
        })

    # Deduplicate by path (keep last)
    seen = {}
    for f in files:
        seen[f["path"]] = f

    return list(seen.values())


def validate_file_structure(files: List[Dict[str, str]], tech_stack: str) -> Dict[str, Any]:
    """
    Validates that the extracted files form a coherent project.
    """
    paths = [f["path"] for f in files]
    issues = []

    # Check for entry point
    has_entry = any(
        p in paths for p in ["src/index.js", "src/main.py", "index.js", "app.py", "main.py"]
    )
    if not has_entry:
        issues.append("No clear entry point found")

    # Check for dependency file
    has_deps = any(
        p in paths for p in ["package.json", "requirements.txt", "Cargo.toml", "go.mod"]
    )
    if not has_deps:
        issues.append("No dependency file found")

    # Check for tests
    has_tests = any("test" in p.lower() or "spec" in p.lower() for p in paths)
    if not has_tests:
        issues.append("No test files found")

    return {
        "valid": len(issues) == 0,
        "issues": issues,
        "file_count": len(files),
        "entry_point": has_entry,
        "has_dependencies": has_deps,
        "has_tests": has_tests
    }


def sanitize_path(path: str) -> str:
    """
    Prevent directory traversal attacks.
    """
    # Remove leading slashes and parent directory references
    path = path.lstrip("/")
    parts = path.split("/")
    safe_parts = [p for p in parts if p != ".." and p != ""]
    return "/".join(safe_parts)
