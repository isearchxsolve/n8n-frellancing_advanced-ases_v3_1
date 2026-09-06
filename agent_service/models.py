"""
ASES - Data Models
"""

from typing import Optional, List
from pydantic import BaseModel, Field


class TenantConfig(BaseModel):
    tenant_id: str = "default"
    planner_model: str = "gpt-4o-mini"
    coder_model: str = "gpt-4o"
    reviewer_model: str = "gpt-4o-mini"
    score_threshold: float = 7.0
    design_failure_threshold: float = 0.5  # [v2.10] Classifier threshold for is_design_level_failure. Per-tenant tunable.
    max_iterations: int = 5
    token_budget: int = 50000
    cost_limit_usd: float = 1.00   # hard ceiling per job; overridable per-request
    allowed_stacks: List[str] = Field(default_factory=lambda: [
        "Node.js", "Python", "React", "Next.js", "FastAPI", "Express"
    ])


class ExecutionResult(BaseModel):
    success: bool
    execution_id: str
    tokens_used: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    error: Optional[str] = None
