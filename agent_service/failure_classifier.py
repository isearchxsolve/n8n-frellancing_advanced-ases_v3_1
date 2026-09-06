"""
ASES - Learned Failure Classifier (v3.0)
=========================================
Replaces the keyword-scored is_design_level_failure() heuristic with a
per-tenant logistic regression classifier trained from journal data.

Problem with the keyword heuristic (design_regenerator.py v2.10):
    score_design_failure() assigns fixed weights to keyword lists.
    "modal is clipped" always scores 0.4 + layout penalty.
    But whether clipping is design-level or code-level depends on the
    tenant's codebase — a React-portal-aware codebase fixes clipping
    in code; a CSS-only codebase needs a spec fix.
    The keyword weights are wrong for every tenant except the imaginary
    one Anthropic tuned them for.

Solution — learned classifier:
    1. After each iteration, journal records contain ground-truth labels:
         - visual_approved=False AND design regen was triggered → design-level
         - visual_approved=False AND coder fixed it next iter → code-level
    2. Train a simple logistic regression per tenant on (feature_vector → label).
    3. Features: TF-IDF bag-of-words on failure description + 5 heuristic
       features from score_design_failure() as priors.
    4. Persist model weights in Postgres (tenant_classifiers table, JSONB column).
    5. Inference: load weights, score, compare against threshold (default 0.5).
    6. Cold start: falls back to score_design_failure() when < MIN_TRAINING_SAMPLES.

Training is fire-and-forget: called at the end of each iteration via
train_classifier_from_journal(), never blocks the main loop.

Inference is called from agent_loop.py as:
    from failure_classifier import is_design_level_failure_learned
    result = await is_design_level_failure_learned(
        failure, config.tenant_id, pool, threshold=config.design_failure_threshold
    )
    if result is None:
        result = is_design_level_failure(failure, config.design_failure_threshold)

Table DDL (add to migration):
    CREATE TABLE IF NOT EXISTS tenant_classifiers (
        tenant_id     TEXT        NOT NULL,
        classifier    JSONB       NOT NULL,   -- {weights, intercept, vocab, trained_at, n_samples}
        updated_at    TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (tenant_id)
    );

    CREATE TABLE IF NOT EXISTS classifier_training_data (
        id            BIGSERIAL   PRIMARY KEY,
        tenant_id     TEXT        NOT NULL,
        description   TEXT        NOT NULL,
        label         SMALLINT    NOT NULL,   -- 1 = design-level, 0 = code-level
        source        TEXT,                   -- "visual_regen" | "coder_fixed" | "interaction_regen"
        created_at    TIMESTAMPTZ DEFAULT NOW()
    );

Requirements (already present in requirements.txt):
    numpy>=1.26  — for logistic regression math
"""

import json
import math
import re
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import structlog

from design_regenerator import score_design_failure

logger = structlog.get_logger()

# Minimum training samples before classifier is used instead of heuristic
MIN_TRAINING_SAMPLES = 20

# Feature dimensions
N_HEURISTIC_FEATURES = 5   # from score_design_failure sub-scores
MAX_VOCAB = 300             # TF-IDF vocabulary cap (keeps model small in JSONB)


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    """Lowercase, strip punctuation, split on whitespace."""
    return re.findall(r'[a-z]+', text.lower())


def _heuristic_features(description: str) -> List[float]:
    """
    5 sub-scores derived from the keyword lists in score_design_failure.
    These act as informative priors so the classifier starts from a
    better-than-random baseline even with few training samples.
    """
    d = description.lower()

    strong_design = ["z-index", "clip", "overflow", "position", "layout",
                     "stacking", "modal clipped", "hidden behind", "obscured by"]
    medium_design = ["contrast", "color", "typography", "font", "spacing",
                     "breakpoint", "responsive", "grid", "flex", "padding",
                     "margin", "alignment", "border", "radius", "shadow"]
    weak_design   = ["state", "animation", "transition", "hover", "focus",
                     "visible", "hidden", "display"]
    code_signals  = ["syntax", "import", "undefined", "typeerror", "referenceerror",
                     "cannot read", "is not a function", "unexpected token",
                     "module not found", "missing dependency"]

    return [
        sum(1.0 for kw in strong_design if kw in d),    # f0: strong design hits
        sum(1.0 for kw in medium_design if kw in d),    # f1: medium design hits
        sum(1.0 for kw in weak_design   if kw in d),    # f2: weak design hits
        sum(1.0 for kw in code_signals  if kw in d),    # f3: code counter-signals
        score_design_failure({"description": description}),  # f4: composite heuristic
    ]


def _build_features(
    description: str,
    vocab: Dict[str, int],   # word → column index
) -> List[float]:
    """
    Build the full feature vector for one sample:
    [N_HEURISTIC_FEATURES heuristic floats] + [MAX_VOCAB TF-IDF floats]
    """
    hf = _heuristic_features(description)

    tokens = _tokenize(description)
    tf: Counter = Counter(tokens)
    total = max(len(tokens), 1)

    tfidf = [0.0] * MAX_VOCAB
    for word, count in tf.items():
        if word in vocab:
            tfidf[vocab[word]] = count / total   # raw TF (IDF not stored; vocab acts as filter)

    return hf + tfidf


# ---------------------------------------------------------------------------
# Logistic regression (pure Python + stdlib, no sklearn dependency)
# ---------------------------------------------------------------------------

def _sigmoid(x: float) -> float:
    # Clamp to avoid overflow in exp
    x = max(-500.0, min(500.0, x))
    return 1.0 / (1.0 + math.exp(-x))


def _logistic_predict(weights: List[float], intercept: float, x: List[float]) -> float:
    """Return P(label=1) for feature vector x."""
    dot = intercept + sum(w * xi for w, xi in zip(weights, x))
    return _sigmoid(dot)


def _train_logistic(
    X: List[List[float]],
    y: List[int],
    lr: float = 0.1,
    epochs: int = 200,
    l2: float = 0.01,
) -> Tuple[List[float], float]:
    """
    Mini-batch SGD logistic regression.

    Returns (weights, intercept).
    l2 regularization prevents overfitting on small datasets.
    """
    if not X or not y:
        return [], 0.0

    n_features = len(X[0])
    weights = [0.0] * n_features
    intercept = 0.0
    n = len(X)

    for _ in range(epochs):
        for i in range(n):
            xi = X[i]
            yi = y[i]
            p = _logistic_predict(weights, intercept, xi)
            err = p - yi                        # gradient of log-loss

            for j in range(n_features):
                weights[j] -= lr * (err * xi[j] + l2 * weights[j])
            intercept -= lr * err

    return weights, intercept


# ---------------------------------------------------------------------------
# Vocabulary building
# ---------------------------------------------------------------------------

def _build_vocab(descriptions: List[str]) -> Dict[str, int]:
    """
    Build a TF-IDF vocabulary from training descriptions.
    Keeps the top MAX_VOCAB most-frequent words (excluding very rare/common ones).
    """
    counter: Counter = Counter()
    for d in descriptions:
        counter.update(_tokenize(d))

    # Exclude tokens that appear in every sample (noise) or only once (too sparse)
    n = max(len(descriptions), 1)
    filtered = {
        word: count for word, count in counter.items()
        if 1 < count < n * 0.9 and len(word) > 2
    }

    top_words = sorted(filtered, key=lambda w: -filtered[w])[:MAX_VOCAB]
    return {word: idx for idx, word in enumerate(top_words)}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

async def _load_classifier(pool, tenant_id: str) -> Optional[Dict[str, Any]]:
    """Load serialized classifier from Postgres. Returns None if not found."""
    try:
        row = await pool.fetchrow(
            "SELECT classifier FROM tenant_classifiers WHERE tenant_id = $1",
            tenant_id,
        )
        if row:
            return json.loads(row["classifier"])
    except Exception as e:
        logger.warning("classifier.load_failed", tenant_id=tenant_id, error=str(e))
    return None


async def _save_classifier(pool, tenant_id: str, clf: Dict[str, Any]) -> None:
    """Upsert serialized classifier to Postgres."""
    try:
        await pool.execute(
            """
            INSERT INTO tenant_classifiers (tenant_id, classifier, updated_at)
            VALUES ($1, $2::jsonb, NOW())
            ON CONFLICT (tenant_id) DO UPDATE
                SET classifier = EXCLUDED.classifier,
                    updated_at = NOW()
            """,
            tenant_id,
            json.dumps(clf),
        )
    except Exception as e:
        logger.warning("classifier.save_failed", tenant_id=tenant_id, error=str(e))


async def _load_training_data(
    pool, tenant_id: str
) -> Tuple[List[str], List[int]]:
    """
    Load labeled training samples from classifier_training_data.
    Returns (descriptions, labels).
    """
    try:
        rows = await pool.fetch(
            """
            SELECT description, label
            FROM classifier_training_data
            WHERE tenant_id = $1
            ORDER BY created_at DESC
            LIMIT 2000
            """,
            tenant_id,
        )
        descriptions = [r["description"] for r in rows]
        labels = [int(r["label"]) for r in rows]
        return descriptions, labels
    except Exception as e:
        logger.warning("classifier.load_data_failed", tenant_id=tenant_id, error=str(e))
        return [], []


async def store_training_sample(
    pool,
    tenant_id: str,
    description: str,
    label: int,         # 1 = design-level, 0 = code-level
    source: str = "",   # "visual_regen" | "coder_fixed" | "interaction_regen"
) -> None:
    """
    Store one labeled training sample. Fire-and-forget — never raises.

    Call sites in agent_loop:
      - When design regen is triggered: label=1, source="visual_regen"
      - When visual fails but coder fixes next iter (no regen): label=0, source="coder_fixed"
    """
    try:
        await pool.execute(
            """
            INSERT INTO classifier_training_data
                (tenant_id, description, label, source)
            VALUES ($1, $2, $3, $4)
            """,
            tenant_id, description[:1000], label, source,
        )
    except Exception as e:
        logger.warning("classifier.store_sample_failed", tenant_id=tenant_id, error=str(e))


# ---------------------------------------------------------------------------
# Training entry point — called from agent_loop after each iteration
# ---------------------------------------------------------------------------

async def train_classifier_from_journal(
    pool,
    tenant_id: str,
    execution_id: str,
) -> None:
    """
    Retrain the per-tenant classifier from all accumulated labeled samples.

    Designed to be called fire-and-forget (wrap in asyncio.create_task).
    Never raises — any failure is logged and swallowed.

    Training is skipped when:
      - Fewer than MIN_TRAINING_SAMPLES labeled samples exist
      - The existing classifier was trained on the same number of samples
        (avoids redundant retraining on the hot path)
    """
    try:
        descriptions, labels = await _load_training_data(pool, tenant_id)
        n = len(descriptions)

        if n < MIN_TRAINING_SAMPLES:
            logger.info(
                "classifier.training_skipped",
                tenant_id=tenant_id,
                reason="insufficient_samples",
                have=n,
                need=MIN_TRAINING_SAMPLES,
            )
            return

        # Skip if already trained on this exact corpus size (idempotent)
        existing = await _load_classifier(pool, tenant_id)
        if existing and existing.get("n_samples") == n:
            logger.info(
                "classifier.training_skipped",
                tenant_id=tenant_id,
                reason="already_current",
                n_samples=n,
            )
            return

        logger.info("classifier.training_start", tenant_id=tenant_id, n_samples=n)

        vocab = _build_vocab(descriptions)
        X = [_build_features(d, vocab) for d in descriptions]
        weights, intercept = _train_logistic(X, labels)

        # Compute training accuracy for the log
        correct = sum(
            1 for xi, yi in zip(X, labels)
            if round(_logistic_predict(weights, intercept, xi)) == yi
        )
        accuracy = correct / n if n > 0 else 0.0

        clf = {
            "weights": weights,
            "intercept": intercept,
            "vocab": vocab,
            "n_samples": n,
            "trained_at": datetime.utcnow().isoformat(),
            "train_accuracy": round(accuracy, 4),
        }

        await _save_classifier(pool, tenant_id, clf)

        logger.info(
            "classifier.trained",
            tenant_id=tenant_id,
            execution_id=execution_id,
            n_samples=n,
            vocab_size=len(vocab),
            train_accuracy=round(accuracy, 4),
        )

    except Exception as e:
        logger.warning(
            "classifier.training_failed",
            tenant_id=tenant_id,
            execution_id=execution_id,
            error=str(e),
        )


# ---------------------------------------------------------------------------
# Inference entry point
# ---------------------------------------------------------------------------

async def is_design_level_failure_learned(
    failure: Dict[str, Any],
    tenant_id: str,
    pool,
    threshold: float = 0.5,
) -> Optional[bool]:
    """
    Classify a failure as design-level (True) or code-level (False) using the
    per-tenant learned classifier.

    Returns None when the classifier is unavailable or undertrained — the
    caller should fall back to is_design_level_failure() from design_regenerator.

    Args:
        failure:   {description, severity, component, ...}
        tenant_id: Slug identifying the tenant.
        pool:      Asyncpg connection pool.
        threshold: Classification threshold (default 0.5, tunable per tenant).

    Example (agent_loop.py):
        result = await is_design_level_failure_learned(
            failure, config.tenant_id, pool, config.design_failure_threshold
        )
        if result is None:
            result = is_design_level_failure(failure, config.design_failure_threshold)
    """
    description = failure.get("description", "")
    if not description:
        return None

    try:
        clf = await _load_classifier(pool, tenant_id)
        if clf is None:
            return None

        n_samples = clf.get("n_samples", 0)
        if n_samples < MIN_TRAINING_SAMPLES:
            return None

        vocab   = clf["vocab"]
        weights = clf["weights"]
        intercept = clf["intercept"]

        x = _build_features(description, vocab)

        # Dimension guard: model weights and feature vector must match
        if len(weights) != len(x):
            logger.warning(
                "classifier.dim_mismatch",
                tenant_id=tenant_id,
                model_dims=len(weights),
                feature_dims=len(x),
            )
            return None

        p = _logistic_predict(weights, intercept, x)

        logger.info(
            "classifier.inference",
            tenant_id=tenant_id,
            p=round(p, 4),
            threshold=threshold,
            result=p >= threshold,
            n_samples=n_samples,
            description_prefix=description[:60],
        )

        return p >= threshold

    except Exception as e:
        logger.warning(
            "classifier.inference_failed",
            tenant_id=tenant_id,
            error=str(e),
        )
        return None
