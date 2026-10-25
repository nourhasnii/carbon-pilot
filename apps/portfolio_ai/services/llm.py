"""Génération de textes Gemini pour l'interface Portfolio AI."""

import logging
import os
import time
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Erreur utilisateur lors d'un appel au service de génération."""


@dataclass(frozen=True)
class LLMResult:
    text: str
    fallback: bool
    response_ms: int


def _generate(prompt):
    api_key = os.environ.get("GOOGLE_API_KEY", getattr(settings, "GOOGLE_API_KEY", ""))
    model = os.environ.get("GEMINI_MODEL", getattr(settings, "GEMINI_MODEL", ""))
    if not api_key:
        logger.error("GOOGLE_API_KEY absente de l'environnement.")
        raise LLMError("La clé Gemini n'est pas configurée.")
    if not model:
        logger.error("GEMINI_MODEL absent de l'environnement.")
        raise LLMError("Le modèle Gemini n'est pas configuré.")
    start = time.perf_counter()
    text = ""
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=30000),
        )
        try:
            interaction = client.interactions.create(model=model, input=prompt)
            text = getattr(interaction, "output_text", "") or ""
        except Exception:
            logger.exception("Gemini interactions.create a échoué (modèle=%s)", model)
        if not text.strip():
            response = client.models.generate_content(model=model, contents=prompt)
            text = response.text or ""
    except Exception as exc:
        logger.exception("Gemini indisponible (modèle=%s)", model)
        status_code = getattr(exc, "status_code", None)
        if status_code is None:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code == 429:
            raise LLMError("Le quota Gemini est temporairement atteint.") from None
        raise LLMError("Le service Gemini est momentanément indisponible.") from None
    response_ms = int((time.perf_counter() - start) * 1000)
    if not text.strip():
        raise LLMError("Gemini n'a fourni aucun texte.")
    text = text.replace("**", "").replace("##", "").strip()
    return LLMResult(text=text, fallback=False, response_ms=response_ms)


def generate_project_description(title):
    """Génère quatre lignes françaises sans inventer de chiffres."""
    prompt = (
        "Rédige exactement quatre lignes en français pour décrire un projet de "
        "décarbonation intitulé :\n"
        f"{title}\n"
        "N'invente aucun chiffre, coût, pourcentage, date ou quantité. "
        "Ne produis que la description, sans titre ni liste numérotée."
    )
    return _generate(prompt)


def summarize_portfolio(run):
    """Génère un résumé ou retourne l'explication de l'optimiseur en repli."""
    budget_remaining = run.budget - run.total_cost if run.total_cost is not None else run.budget
    selected_count = len(run.selected_project_ids or [])
    prompt = (
        "Rédige un résumé exécutif en français de cette optimisation. "
        "Utilise uniquement les chiffres fournis et n'invente aucun chiffre. "
        f"Projets retenus : {selected_count}. "
        f"Coût total : {run.total_cost}. Budget : {run.budget}. "
        f"Budget restant : {budget_remaining}. tCO2e totales : {run.total_tco2e}. "
        "N'utilise aucun formatage Markdown (pas d'astérisques, pas de titres). "
        "Ne mentionne aucune donnée absente."
    )
    start = time.perf_counter()
    try:
        result = _generate(prompt)
        return result
    except LLMError:
        return LLMResult(
            text=run.explanation or "Aucune explication n'est disponible.",
            fallback=True,
            response_ms=int((time.perf_counter() - start) * 1000),
        )