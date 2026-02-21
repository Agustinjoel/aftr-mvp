"""
Lógica de negocio: Poisson, evaluación de mercados, construcción de candidatos.
Sin dependencias de I/O (DB, HTTP, disco); solo datos en memoria.
"""
from core.poisson import (
    build_candidates,
    estimate_xg,
    match_probs,
    poisson_pmf,
)
from core.evaluation import evaluate_market

__all__ = [
    "poisson_pmf",
    "match_probs",
    "estimate_xg",
    "build_candidates",
    "evaluate_market",
]
