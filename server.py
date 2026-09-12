"""
LinkedIn Job Scraper + Resume Matcher — Flask Backend
=====================================================

A Flask API server that:
  1. Scrapes LinkedIn jobs via the Apify platform.
  2. Parses uploaded DOCX resumes to extract skills and experience.
  3. Matches resume skills against job descriptions using a hybrid
     keyword + TF-IDF cosine-similarity scoring algorithm.

Author : AI Research
Created: 2026-06-06
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from glob import glob
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from docx import Document
from pypdf import PdfReader
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# Try to import local sentence-transformers optionally
try:
    from sentence_transformers import SentenceTransformer
    HAS_LOCAL_ST = True
except ImportError:
    HAS_LOCAL_ST = False

_local_model = None


# ---------------------------------------------------------------------------
# Constants & paths
# ---------------------------------------------------------------------------

BASE_DIR: Path = Path(__file__).resolve().parent
DATA_DIR: Path = BASE_DIR / "data"
CONFIG_PATH: Path = BASE_DIR / "config.json"
RESUME_PROFILE_PATH: Path = DATA_DIR / "resume_profile.json"
CAREER_SITES_PATH: Path = DATA_DIR / "career_sites.json"
RECRUITERS_INDEX_PATH: Path = DATA_DIR / "recruiters_index.json"

_career_sites_cache: Optional[Dict[str, str]] = None
_recruiters_index_cache: Optional[Dict[str, List[Dict[str, Any]]]] = None

APIFY_BASE_URL: str = "https://api.apify.com/v2"
POLL_INTERVAL_SECONDS: int = 5
MAX_POLL_ATTEMPTS: int = 120  # 10-minute ceiling

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("server")

# ---------------------------------------------------------------------------
# Flask application factory
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")
CORS(app)

# ---------------------------------------------------------------------------
# In-memory resume store
# ---------------------------------------------------------------------------

resume_profile: Dict[str, Any] = {
    "skills": [],
    "experience": [],
    "education": [],
    "full_text": "",
}

# ---------------------------------------------------------------------------
# Quota & Rate-Limit Tracking Store
# ---------------------------------------------------------------------------

_hf_quota_tracker: Dict[str, Any] = {
    "remaining_requests": 10000,
    "limit_requests": 10000,
    "requests_used_today": 0,
    "reset_seconds": 0,
    "model_status": "ready"
}


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _load_config() -> Dict[str, Any]:
    """Load config from environment variables or fallback to config.json if available.

    Returns:
        Dict containing config values.
    """
    # 1. Start with env variables
    config = {
        "apify_token": os.environ.get("APIFY_TOKEN"),
        "actor_id": os.environ.get("APIFY_ACTOR_ID", "curious_coder/linkedin-jobs-scraper"),
        "default_keywords": os.environ.get("DEFAULT_KEYWORDS", ""),
        "default_location": os.environ.get("DEFAULT_LOCATION", ""),
        "default_max_items": int(os.environ.get("DEFAULT_MAX_ITEMS", "25")),
    }

    # 2. Read config.json if it exists to load file configurations (like hf_api_token)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                file_config = json.load(fh)
                for k, v in file_config.items():
                    # Keep env vars if set, but load hf_api_token and other missing keys
                    if k == "hf_api_token" or not config.get(k):
                        config[k] = v
        except Exception as exc:
            logger.warning("Could not load config.json: %s", exc)

    return config


def _ensure_data_dir() -> None:
    """Create the ``data/`` directory if it does not exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _save_json(data: Any, filepath: Path) -> None:
    """Serialise *data* as pretty-printed JSON to *filepath*.

    Args:
        data: JSON-serialisable Python object.
        filepath: Destination path.
    """
    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def _load_json(filepath: Path) -> Any:
    """Read and return parsed JSON from *filepath*.

    Args:
        filepath: Source path.

    Returns:
        Parsed JSON object.
    """
    with open(filepath, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _latest_jobs_cache() -> Optional[Path]:
    """Return the path to the most recently written job-cache file.

    Returns:
        A ``Path`` to the newest ``data/jobs_*.json`` file, or ``None``
        if no cache files exist.
    """
    files = sorted(glob(str(DATA_DIR / "jobs_*.json")))
    return Path(files[-1]) if files else None


def _get_recent_cached_jobs(
    limit: int = 25,
    keywords: Optional[str] = None,
    location: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Aggregate, deduplicate, and rank the most recent unique jobs across cached scrape files.
    
    Instead of only reading the single latest file (which might contain very few jobs),
    this traverses recent scrape cache files in reverse chronological order to compile
    a rich pool of unique jobs (up to `limit`). If keywords or location context are provided,
    it prioritizes contextually relevant listings.
    """
    files = sorted(glob(str(DATA_DIR / "jobs_*.json")), reverse=True)
    if not files:
        return []

    seen_keys = set()
    all_jobs: List[Dict[str, Any]] = []

    kw_terms = [k.strip().lower() for k in (keywords or "").split(",") if k.strip()]
    loc_terms = [l.strip().lower() for l in (location or "").split(";") if l.strip()]

    matched_jobs: List[Dict[str, Any]] = []
    other_jobs: List[Dict[str, Any]] = []

    for filepath in files:
        try:
            items = _load_json(Path(filepath))
            if not isinstance(items, list):
                continue
            for j in items:
                jid = j.get("id") or j.get("link") or f"{j.get('title')}_{j.get('companyName') or j.get('company')}"
                if jid in seen_keys:
                    continue
                seen_keys.add(jid)

                # Contextual relevance check
                title = (j.get("title") or "").lower()
                desc = (j.get("descriptionText") or j.get("description") or "").lower()
                j_loc = (j.get("location") or "").lower()

                matches_kw = True
                if kw_terms:
                    matches_kw = any(term in title or term in desc for term in kw_terms)

                matches_loc = True
                if loc_terms:
                    matches_loc = any(term in j_loc for term in loc_terms)

                if (kw_terms or loc_terms) and (matches_kw and matches_loc):
                    matched_jobs.append(j)
                else:
                    other_jobs.append(j)

                # Stop scanning if we have accumulated enough recent unique jobs
                if len(matched_jobs) + len(other_jobs) >= max(limit * 2, 60):
                    break
        except Exception as exc:
            logger.warning("Could not read cache file %s: %s", filepath, exc)

        if len(matched_jobs) + len(other_jobs) >= max(limit * 2, 60):
            break

    # Prioritize contextual matches first, then backfill with the most recent unique jobs
    pool = matched_jobs + other_jobs
    return pool[:limit]


def _get_hf_token() -> Optional[str]:
    """Retrieve the HF API token from environment or config.json."""
    token = os.environ.get("HF_API_TOKEN")
    if not token:
        try:
            config = _load_config()
            token = config.get("hf_api_token")
        except Exception:
            pass
    return token


_BOILERPLATE_PATTERNS = [
    r"equal opportunity employer",
    r"affirmative action",
    r"disability.*veteran",
    r"race,?\s*color,?\s*religion",
    r"benefits include",
    r"401\s*k",
    r"reasonable accommodation",
    r"all qualified applicants will receive consideration",
    r"background check",
    r"drug screen",
    r"salary range",
    r"privacy policy",
]

def _is_boilerplate_sentence(sent: str) -> bool:
    """Filter out boilerplate or trivial sentences from job descriptions."""
    s = sent.strip()
    if len(s) < 15:
        return True
    s_lower = s.lower()
    for pattern in _BOILERPLATE_PATTERNS:
        if re.search(pattern, s_lower):
            return True
    return False

def _split_into_sentences(text: str) -> List[str]:
    """Split text into sentences while filtering out empty or boilerplate items."""
    raw_sentences = re.split(r'[\r\n]+|[.!?]+(?:\s+|$)', text)
    valid_sentences = []
    for s in raw_sentences:
        clean = " ".join(s.split())
        if clean and not _is_boilerplate_sentence(clean):
            valid_sentences.append(clean)
    return valid_sentences

def _get_embeddings(texts: List[str]) -> List[List[float]]:
    """Generate dense embeddings for a list of texts using Hugging Face Inference API or local fallback."""
    if not texts:
        return []

    # Clean and truncate texts to 1000 characters to prevent payload/context errors
    cleaned_texts = []
    for t in texts:
        cleaned = re.sub(r'<[^>]+>', ' ', t)
        cleaned = " ".join(cleaned.split())
        cleaned_texts.append(cleaned[:1000])

    # 1. Attempt Hugging Face Inference API if token is configured
    hf_token = _get_hf_token()
    if hf_token:
        logger.info("HF_API_TOKEN detected. Fetching embeddings from Hugging Face Inference API...")
        url = "https://router.huggingface.co/hf-inference/models/sentence-transformers/all-MiniLM-L6-v2/pipeline/feature-extraction"
        headers = {"Authorization": f"Bearer {hf_token}"}
        
        import requests as req_lib
        for attempt in range(1, 6):
            try:
                response = req_lib.post(
                    url, 
                    headers=headers, 
                    json={"inputs": cleaned_texts, "options": {"wait_for_model": True}}, 
                    timeout=30
                )
                if response.status_code == 200:
                    _hf_quota_tracker["requests_used_today"] = _hf_quota_tracker.get("requests_used_today", 0) + 1
                    rem = response.headers.get("x-ratelimit-remaining-requests-day") or response.headers.get("x-ratelimit-remaining")
                    lim = response.headers.get("x-ratelimit-limit-requests-day") or response.headers.get("x-ratelimit-limit")
                    reset = response.headers.get("x-ratelimit-reset")
                    if rem:
                        try:
                            _hf_quota_tracker["remaining_requests"] = int(rem)
                        except (ValueError, TypeError):
                            pass
                    else:
                        # Router endpoint does not forward rate-limit headers; decrement tracked daily quota
                        _hf_quota_tracker["remaining_requests"] = max(
                            0, _hf_quota_tracker["limit_requests"] - _hf_quota_tracker["requests_used_today"]
                        )

                    if lim:
                        try:
                            _hf_quota_tracker["limit_requests"] = int(lim)
                        except (ValueError, TypeError):
                            pass
                    if reset:
                        try:
                            _hf_quota_tracker["reset_seconds"] = int(reset)
                        except (ValueError, TypeError):
                            pass
                    _hf_quota_tracker["model_status"] = "active"

                    embeddings = response.json()
                    if isinstance(embeddings, list) and len(embeddings) == len(texts):
                        if all(isinstance(emb, list) for emb in embeddings):
                            logger.info("Successfully fetched %d embeddings from HF Inference API.", len(texts))
                            return embeddings
                elif response.status_code == 401:
                    logger.error("Hugging Face Error: Invalid or expired API token.")
                    raise RuntimeError("Hugging Face Error: Invalid or expired API token.")
                elif response.status_code == 429:
                    logger.error("Hugging Face Error: Rate limit exceeded. Please try again later.")
                    raise RuntimeError("Hugging Face Error: Rate limit exceeded. Please try again later.")
                elif response.status_code == 503:
                    err_json = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                    est_time = err_json.get("estimated_time", 15)
                    logger.warning("HF Model loading (503). Retrying in %s seconds...", est_time)
                    time.sleep(min(max(est_time, 5), 20))
                else:
                    logger.warning("HF API error: %d %s", response.status_code, response.text)
                    break
            except RuntimeError:
                raise
            except Exception as exc:
                logger.warning("HF API connection failed on attempt %d: %s", attempt, exc)
                time.sleep(2)
        logger.warning("HF Inference API failed. Trying fallback options...")

    # 2. Local Sentence-Transformers Fallback
    global _local_model
    if HAS_LOCAL_ST:
        logger.info("Using local sentence-transformers fallback...")
        try:
            if _local_model is None:
                logger.info("Loading local SentenceTransformer model 'all-MiniLM-L6-v2'...")
                _local_model = SentenceTransformer("all-MiniLM-L6-v2")
            embeddings = _local_model.encode(cleaned_texts, show_progress_bar=False)
            return embeddings.tolist()
        except Exception as exc:
            logger.exception("Failed to compute embeddings with local model: %s", exc)

    raise RuntimeError("Embeddings generation failed: No HF token and local sentence-transformers not available/failed.")


def _batch_get_embeddings(texts: List[str], batch_size: int = 32) -> List[List[float]]:
    """Chunk texts into smaller sub-batches to prevent HTTP 413 and timeouts."""
    if not texts:
        return []
    all_embeddings: List[List[float]] = []
    for i in range(0, len(texts), batch_size):
        chunk = texts[i:i + batch_size]
        chunk_embeddings = _get_embeddings(chunk)
        all_embeddings.extend(chunk_embeddings)
    return all_embeddings


def _get_embedding(text: str) -> List[float]:
    """Generate dense embedding for a single string."""
    res = _get_embeddings([text])
    return res[0] if res else []



def _load_resume_profile() -> None:
    """Populate the in-memory *resume_profile* from disk if available."""
    global resume_profile
    if RESUME_PROFILE_PATH.exists():
        try:
            resume_profile = _load_json(RESUME_PROFILE_PATH)
            logger.info("Loaded resume profile from %s", RESUME_PROFILE_PATH)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load resume profile: %s", exc)


# ---------------------------------------------------------------------------
# India Remote Target Companies matching
# ---------------------------------------------------------------------------

_india_remote_companies: List[Dict[str, Any]] = []


def _load_india_remote_companies() -> List[Dict[str, Any]]:
    """Parse india_remote_companies.md and return list of companies.
    Caches results in a global variable for performance.
    """
    global _india_remote_companies
    if _india_remote_companies:
        return _india_remote_companies

    companies = []
    file_path = BASE_DIR / "india_remote_companies.md"
    if not file_path.exists():
        logger.warning("india_remote_companies.md not found at %s", file_path)
        return companies

    # Regex to match: 1. [vituity.com](https://careers.vituity.com)
    pattern = re.compile(r'^\d+\.\s+\[([^\]]+)\]\(([^)]+)\)')

    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            for line in fh:
                m = pattern.match(line.strip())
                if m:
                    name_or_domain = m.group(1).strip()
                    url = m.group(2).strip()

                    domain = name_or_domain.lower()
                    for prefix in ["www.", "careers.", "jobs.", "mycareer.", "internal-careers."]:
                        if domain.startswith(prefix):
                            domain = domain[len(prefix):]

                    base_name = domain
                    if "." in domain:
                        parts = domain.split(".")
                        if len(parts) > 1:
                            base_name = parts[0]

                    companies.append({
                        "original": name_or_domain,
                        "domain": domain,
                        "base_name": base_name,
                        "url": url
                    })
        _india_remote_companies = companies
        logger.info("Loaded %d companies from india_remote_companies.md", len(companies))
    except Exception as exc:
        logger.warning("Failed to parse india_remote_companies.md: %s", exc)

    return _india_remote_companies


def _clean_company_name(name: str) -> str:
    """Clean company name by removing punctuation, underscores, and corporate suffixes."""
    if not name:
        return ""
    name = name.lower().strip()
    name = re.sub(r"[_\-]+", " ", name)
    name = re.sub(r'[^\w\s]', ' ', name)
    words = name.split()
    suffixes = {
        "inc", "llc", "ltd", "co", "corp", "corporation", "pvt", "private",
        "limited", "solutions", "services", "technologies", "technology",
        "careers", "career", "group"
    }
    cleaned_words = [w for w in words if w not in suffixes]
    return " ".join(cleaned_words)


def _load_career_and_recruiter_indices() -> tuple[Dict[str, str], Dict[str, List[Dict[str, Any]]]]:
    """Loads and caches career portal sites and recruiter index in memory."""
    global _career_sites_cache, _recruiters_index_cache

    if _career_sites_cache is None:
        if CAREER_SITES_PATH.exists():
            try:
                with open(CAREER_SITES_PATH, "r", encoding="utf-8") as f:
                    _career_sites_cache = json.load(f)
            except Exception as e:
                logger.warning("Could not read career_sites.json: %s", e)
                _career_sites_cache = {}
        else:
            _career_sites_cache = {}

    if _recruiters_index_cache is None:
        if RECRUITERS_INDEX_PATH.exists():
            try:
                with open(RECRUITERS_INDEX_PATH, "r", encoding="utf-8") as f:
                    _recruiters_index_cache = json.load(f)
            except Exception as e:
                logger.warning("Could not read recruiters_index.json: %s", e)
                _recruiters_index_cache = {}
        else:
            _recruiters_index_cache = {}

    return _career_sites_cache, _recruiters_index_cache


def _enrich_jobs_with_companies(jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Enriches jobs with career portal URLs, active recruiter contacts, and partner flags."""
    career_sites, recruiters_index = _load_career_and_recruiter_indices()
    india_remote_companies = _load_india_remote_companies()

    enriched_jobs = []
    for job in jobs:
        company_name = job.get("companyName") or job.get("company") or ""
        cleaned_job = _clean_company_name(company_name)
        job_tokens = set(cleaned_job.split()) if cleaned_job else set()

        career_url: Optional[str] = None
        recruiters: List[Dict[str, Any]] = []
        is_partner = False
        partner_info = None

        if cleaned_job:
            # 1. Match Career Site Portal URL (Direct, Token match e.g. 'aaa', or Substring)
            if cleaned_job in career_sites:
                career_url = career_sites[cleaned_job]
            else:
                for c_name, u in career_sites.items():
                    if c_name in job_tokens or (len(c_name) >= 4 and (c_name in cleaned_job or cleaned_job in c_name)):
                        career_url = u
                        break

            # 2. Match Active Recruiters (Direct, Token match e.g. 'aaa', or Substring)
            if cleaned_job in recruiters_index:
                recruiters = recruiters_index[cleaned_job]
            else:
                for r_comp, r_list in recruiters_index.items():
                    if r_comp in job_tokens or (len(r_comp) >= 4 and (r_comp in cleaned_job or cleaned_job in r_comp)):
                        recruiters = r_list
                        break

            # 3. Match India Remote Target Companies
            for comp in india_remote_companies:
                base = comp["base_name"].lower().replace("-", "")
                base_clean = _clean_company_name(base)
                
                # Direct match or Token match
                if (
                    cleaned_job == base
                    or cleaned_job == base_clean
                    or base in job_tokens
                    or base_clean in job_tokens
                ):
                    is_partner = True
                    partner_info = comp
                    break

                # Substring match (base in job_company or vice-versa)
                if len(base) >= 4 and (base in cleaned_job or cleaned_job in base):
                    is_partner = True
                    partner_info = comp
                    break

                # Check collapsed spaces match
                raw_company_clean = re.sub(r'[^\w\s]', '', company_name.lower()).replace(" ", "")
                if base in raw_company_clean or raw_company_clean in base:
                    if len(base) >= 4 or base == raw_company_clean:
                        is_partner = True
                        partner_info = comp
                        break

        enriched = {**job}
        enriched["career_site_url"] = career_url
        enriched["active_recruiters"] = recruiters[:3] if recruiters else []
        enriched["has_active_recruiters"] = bool(recruiters)
        enriched["is_partner_company"] = is_partner
        enriched["partner_company_info"] = partner_info

        enriched_jobs.append(enriched)

    return enriched_jobs


# ---------------------------------------------------------------------------
# Resume parsing helpers
# ---------------------------------------------------------------------------

# Section headings that typically contain skills information.
_SKILLS_HEADINGS: re.Pattern = re.compile(
    r"^(?:skills|technical\s+skills|core\s+competencies|"
    r"key\s+skills|areas\s+of\s+expertise|technologies|proficiencies)\s*$",
    re.IGNORECASE,
)

_EXPERIENCE_HEADINGS: re.Pattern = re.compile(
    r"^(?:experience|work\s+experience|professional\s+experience|"
    r"employment\s+history|career\s+history)\s*$",
    re.IGNORECASE,
)

_EDUCATION_HEADINGS: re.Pattern = re.compile(
    r"^(?:education|academic\s+background|qualifications|certifications?)\s*$",
    re.IGNORECASE,
)


def _extract_resume_sections(text: str) -> Dict[str, Any]:
    """Parse raw resume text into skills, experience, and education sections.

    The parser walks through each line, detects section headings, and
    collects the content lines belonging to each section.  Skills are
    further split on common delimiters (commas, pipes, semicolons, bullets)
    to yield a flat list.

    Args:
        text: The full plaintext content of the resume.

    Returns:
        A dict with keys ``skills``, ``experience``, ``education``, and
        ``full_text``.
    """
    lines: List[str] = [ln.strip() for ln in text.splitlines()]

    skills_lines: List[str] = []
    experience_lines: List[str] = []
    education_lines: List[str] = []
    current_section: Optional[str] = None

    for line in lines:
        if not line:
            continue

        # Detect section transitions
        if _SKILLS_HEADINGS.match(line):
            current_section = "skills"
            continue
        if _EXPERIENCE_HEADINGS.match(line):
            current_section = "experience"
            continue
        if _EDUCATION_HEADINGS.match(line):
            current_section = "education"
            continue

        # Accumulate content into the active section
        if current_section == "skills":
            skills_lines.append(line)
        elif current_section == "experience":
            experience_lines.append(line)
        elif current_section == "education":
            education_lines.append(line)

    # Split skills on common delimiters (including newlines and colons) to produce a flat list
    raw_skills_text = "\n".join(skills_lines)
    skills: List[str] = [
        s.strip()
        for s in re.split(r"[,;|:•·▪►\n]", raw_skills_text)
        if s.strip()
    ]

    return {
        "skills": skills,
        "experience": experience_lines,
        "education": education_lines,
        "full_text": text,
    }


# ---------------------------------------------------------------------------
# Matching algorithm
# ---------------------------------------------------------------------------


def _semantic_skill_match(
    skill_name: str,
    job_description_lower: str,
    skill_embedding: Optional[List[float]] = None,
    job_sentence_embeddings: Optional[List[List[float]]] = None,
    threshold: float = 0.75,
    partial_threshold: float = 0.55
) -> Dict[str, Any]:
    """Semantic skill matcher combining exact-match fast path with embedding cosine similarity.
    
    1. Exact Match Fast Path: If skill appears literally with word boundary check, return matched.
    2. Vector Cosine Similarity: Compare skill vector to JD sentence vectors.
    """
    skill_lower = skill_name.lower().strip()
    if not skill_lower:
        return {"matched": False, "partial": False, "score": 0.0, "reason": "empty"}

    # 1. Exact Match Fast Path
    pattern = r'\b' + re.escape(skill_lower) + r'\b'
    if re.search(pattern, job_description_lower):
        return {
            "matched": True,
            "partial": False,
            "score": 1.0,
            "reason": "exact_match",
            "semantic": False
        }

    # 2. Vector Cosine Similarity Fallback
    if skill_embedding and job_sentence_embeddings:
        try:
            import numpy as np
            skill_vec = np.array(skill_embedding).reshape(1, -1)
            sentence_vecs = np.array(job_sentence_embeddings)
            sims = cosine_similarity(skill_vec, sentence_vecs)[0]
            max_sim = float(np.max(sims)) if len(sims) > 0 else 0.0

            if max_sim >= threshold:
                return {
                    "matched": True,
                    "partial": False,
                    "score": round(max_sim, 4),
                    "reason": "semantic_match",
                    "semantic": True
                }
            elif max_sim >= partial_threshold:
                return {
                    "matched": False,
                    "partial": True,
                    "score": round(max_sim, 4),
                    "reason": "semantic_partial",
                    "semantic": True
                }
            else:
                return {
                    "matched": False,
                    "partial": False,
                    "score": round(max_sim, 4),
                    "reason": "semantic_low",
                    "semantic": False
                }
        except Exception as exc:
            logger.warning("Error calculating semantic skill match for '%s': %s", skill_name, exc)

    # 3. Token-based fallback if no embeddings or vector similarity below thresholds
    skill_clean = re.sub(r'[^\w\s\+\#\-\.\/]', ' ', skill_lower)
    tokens = [t.strip() for t in skill_clean.split() if t.strip()]
    if not tokens:
        return {"matched": False, "partial": False, "score": 0.0, "reason": "no_tokens"}

    stop_words = {'and', 'or', 'of', 'in', 'with', 'for', 'a', 'an', 'the', 'to', 'at', 'by', 'on', 'using', 'experience'}
    filtered_tokens = [t for t in tokens if t not in stop_words] or tokens

    matched_count = 0
    token_details = []
    for token in filtered_tokens:
        tok_pattern = r'\b' + re.escape(token) + r'\b'
        tok_matched = bool(re.search(tok_pattern, job_description_lower))
        if tok_matched:
            matched_count += 1
        token_details.append({"token": token, "matched": tok_matched})

    match_ratio = matched_count / len(filtered_tokens) if filtered_tokens else 0.0
    if match_ratio == 1.0:
        return {"matched": True, "partial": False, "score": 0.85, "reason": "token_match", "tokens": token_details}
    elif match_ratio >= 0.5:
        return {"matched": False, "partial": True, "score": round(match_ratio * 0.7, 2), "reason": "partial_match", "tokens": token_details}

    return {"matched": False, "partial": False, "score": 0.0, "reason": "missing_tokens", "tokens": token_details}


def _analyze_skill_match(
    skill: str,
    description_lower: str,
    skill_embedding: Optional[List[float]] = None,
    job_sentence_embeddings: Optional[List[List[float]]] = None
) -> Dict[str, Any]:
    """Analyze a skill match against a job description using exact matching and semantic embeddings."""
    return _semantic_skill_match(
        skill_name=skill,
        job_description_lower=description_lower,
        skill_embedding=skill_embedding,
        job_sentence_embeddings=job_sentence_embeddings
    )


def _skill_matches_job(skill: str, description: str) -> bool:
    """Legacy helper returning simple True/False match flag."""
    return _analyze_skill_match(skill, description.lower())["matched"]


def _compute_match_scores(
    jobs: List[Dict[str, Any]],
    skills: List[str],
    full_resume_text: str,
    core_skills: List[str] = None,
    mode: str = "semantic"
) -> List[Dict[str, Any]]:
    """Score and rank *jobs* against the applicant's profile.

    The final score is a weighted combination:

        ``final = 0.6 × keyword_score + 0.4 × similarity_score``

    Both component scores are normalised to the 0–100 range.

    Args:
        jobs: List of job dicts; each must contain a ``description`` field.
        skills: List of skill strings extracted from the resume.
        full_resume_text: The full plaintext of the resume.
        core_skills: List of starred core skill strings.
        mode: The matching intelligence mode ('semantic' or 'tfidf').

    Returns:
        A copy of *jobs* augmented with ``match_score``, ``keyword_score``,
        ``tfidf_score`` (representing similarity score), ``match_method``,
        ``matched_skills``, ``partial_skills``, and ``skills_analysis`` fields.
    """
    if not jobs:
        return []

    core_skills_lower = [c.lower() for c in core_skills] if core_skills else []

    # Pre-compute skill and job sentence embeddings if running in semantic mode
    skill_embeddings_map: Dict[str, List[float]] = {}
    job_sentences_embeddings_map: Dict[int, List[List[float]]] = {}

    if mode == "semantic":
        try:
            # 1. Batch generate embeddings for all resume skills
            if skills:
                clean_skills = [s.strip() for s in skills if s.strip()]
                skill_vectors = _batch_get_embeddings(clean_skills, batch_size=32)
                for s, vec in zip(clean_skills, skill_vectors):
                    skill_embeddings_map[s] = vec

            # 2. Extract and batch generate embeddings for sentences across all jobs
            job_sentence_slices = []
            all_sentences = []
            for idx, job in enumerate(jobs):
                desc = (job.get("descriptionText") or job.get("description") or "")
                sentences = _split_into_sentences(desc)
                # Keep up to 30 most relevant non-boilerplate sentences per job to preserve compute
                sentences = sentences[:30]
                start_idx = len(all_sentences)
                all_sentences.extend(sentences)
                end_idx = len(all_sentences)
                job_sentence_slices.append((idx, start_idx, end_idx))

            if all_sentences:
                all_sent_embeddings = _batch_get_embeddings(all_sentences, batch_size=32)
                for idx, start_idx, end_idx in job_sentence_slices:
                    job_sentences_embeddings_map[idx] = all_sent_embeddings[start_idx:end_idx]
        except Exception as exc:
            logger.warning("Could not pre-compute sentence/skill embeddings for semantic matching: %s", exc)

    # ---- Keyword matching ----
    keyword_results: List[Dict[str, Any]] = []
    for job_idx, job in enumerate(jobs):
        description: str = (job.get("descriptionText") or job.get("description") or "").lower()
        job_sent_embs = job_sentences_embeddings_map.get(job_idx)
        
        matched_skills = []
        partial_skills = []
        skills_analysis = {}
        kw_score_accumulator = 0.0
        total_weight = 0.0
        
        for skill in skills:
            skill_emb = skill_embeddings_map.get(skill)
            analysis = _analyze_skill_match(
                skill=skill,
                description_lower=description,
                skill_embedding=skill_emb,
                job_sentence_embeddings=job_sent_embs
            )
            skills_analysis[skill] = analysis
            
            # Weighted scoring: core skills have weight 2.0, normal skills have 1.0
            weight = 2.0 if skill.lower() in core_skills_lower else 1.0
            total_weight += weight
            
            if analysis["matched"]:
                matched_skills.append(skill)
                kw_score_accumulator += weight * 1.0
            elif analysis["partial"]:
                partial_skills.append(skill)
                kw_score_accumulator += weight * 0.5  # Half credit for partial match
                
        kw_score = kw_score_accumulator / total_weight if total_weight else 0.0
        keyword_results.append({
            "matched_skills": matched_skills,
            "partial_skills": partial_skills,
            "skills_analysis": skills_analysis,
            "keyword_score": kw_score,
        })
        
        # Log matching diagnostics for easy monitoring
        logger.info(
            "Matcher Diagnostics — Job: '%s' | Matched: %d | Partial: %d | Weighted Score: %.2f",
            job.get("title"),
            len(matched_skills),
            len(partial_skills),
            kw_score * 100
        )

    # ---- Similarity scoring ----
    similarity_scores: List[float] = [0.0] * len(jobs)
    match_method = "TF-IDF Similarity"
    use_semantic = (mode == "semantic")

    if use_semantic:
        try:
            # 1. Resolve resume embedding
            resume_emb = resume_profile.get("embedding")
            if not resume_emb:
                logger.info("Resume embedding not cached. Generating now...")
                text_to_embed = full_resume_text.strip() if full_resume_text.strip() else " ".join(skills)
                resume_emb = _get_embedding(text_to_embed)
                # Persist embedding to resume profile
                resume_profile["embedding"] = resume_emb
                _ensure_data_dir()
                _save_json(resume_profile, RESUME_PROFILE_PATH)
            
            # 2. Resolve job embeddings
            job_embs = []
            jobs_to_embed = []
            jobs_to_embed_indices = []
            
            for idx, job in enumerate(jobs):
                emb = job.get("embedding")
                if isinstance(emb, list) and emb:
                    job_embs.append((idx, emb))
                else:
                    desc = job.get("descriptionText") or job.get("description") or ""
                    jobs_to_embed.append(desc)
                    jobs_to_embed_indices.append(idx)
            
            if jobs_to_embed:
                logger.info("Calculating embeddings on-the-fly for %d jobs...", len(jobs_to_embed))
                new_embs = _batch_get_embeddings(jobs_to_embed, batch_size=32)
                for idx_in_new, idx_in_jobs in enumerate(jobs_to_embed_indices):
                    emb = new_embs[idx_in_new]
                    job_embs.append((idx_in_jobs, emb))
                    # Backport embedding to the active job cache in memory
                    jobs[idx_in_jobs]["embedding"] = emb
            
            # Restore original sorting index
            job_embs.sort(key=lambda x: x[0])
            vectors = [x[1] for x in job_embs]
            
            # 3. Compute Cosine Similarity
            import numpy as np
            resume_vector = np.array(resume_emb).reshape(1, -1)
            job_vectors = np.array(vectors)
            cos_sims = cosine_similarity(resume_vector, job_vectors)[0]
            
            # 4. Scale embedding similarities [0.35, 0.80] -> [0.0, 1.0]
            for idx, sim in enumerate(cos_sims):
                scaled_sim = min(max((sim - 0.35) / 0.45, 0.0), 1.0)
                similarity_scores[idx] = scaled_sim
            
            match_method = "Semantic Similarity"
            logger.info("Successfully calculated similarity scores using Hugging Face embeddings.")
            
        except Exception as exc:
            logger.warning("Semantic embedding similarity calculation failed: %s. Falling back to TF-IDF.", exc)
            use_semantic = False

    if not use_semantic:
        # Fallback to TF-IDF Cosine Similarity
        if full_resume_text.strip():
            descriptions: List[str] = [
                job.get("descriptionText") or job.get("description") or "" for job in jobs
            ]
            corpus: List[str] = [full_resume_text] + descriptions
            vectorizer = TfidfVectorizer(stop_words="english")
            tfidf_matrix = vectorizer.fit_transform(corpus)
            cos_sim = cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:])
            raw_tfidf = cos_sim[0].tolist()
            # Scale TF-IDF similarity to fit 0-100 better
            similarity_scores = [min(score * 2.5, 1.0) for score in raw_tfidf]
        else:
            similarity_scores = [0.0] * len(jobs)
        match_method = "TF-IDF Similarity"

    # ---- Combine scores ----
    scored_jobs: List[Dict[str, Any]] = []
    for idx, job in enumerate(jobs):
        kw_score_100 = round(keyword_results[idx]["keyword_score"] * 100)
        sim_score_100 = round(similarity_scores[idx] * 100)
        
        final_score = round(
            0.6 * keyword_results[idx]["keyword_score"] * 100
            + 0.4 * similarity_scores[idx] * 100
        )

        enriched = {**job}
        enriched["match_score"] = min(final_score, 100)
        enriched["keyword_score"] = kw_score_100
        enriched["tfidf_score"] = sim_score_100  # Map to existing tfidf_score key for UI compatibility
        enriched["match_method"] = match_method  # Add matching method details
        enriched["matched_skills"] = keyword_results[idx]["matched_skills"]
        enriched["partial_skills"] = keyword_results[idx]["partial_skills"]
        enriched["skills_analysis"] = keyword_results[idx]["skills_analysis"]
        scored_jobs.append(enriched)

    scored_jobs.sort(key=lambda j: j["match_score"], reverse=True)

    # ---- Accuracy & Debugging Summary ----
    logger.info("=" * 80)
    logger.info("📊 MATCH ACCURACY DEBUG REPORT  (mode=%s, method=%s)", mode, match_method)
    logger.info("=" * 80)
    logger.info(
        "Resume Profile  → Skills: %d | Core Skills: %d | Resume Text Length: %d chars",
        len(skills), len(core_skills_lower), len(full_resume_text)
    )
    logger.info("Jobs Evaluated  → %d total", len(scored_jobs))

    # Score distribution
    scores = [j["match_score"] for j in scored_jobs]
    if scores:
        import statistics
        avg_score = statistics.mean(scores)
        median_score = statistics.median(scores)
        high_fit = sum(1 for s in scores if s >= 60)
        mid_fit = sum(1 for s in scores if 30 <= s < 60)
        low_fit = sum(1 for s in scores if s < 30)
        logger.info(
            "Score Distribution → Avg: %.1f%% | Median: %.1f%% | High(≥60%%): %d | Mid(30-59%%): %d | Low(<30%%): %d",
            avg_score, median_score, high_fit, mid_fit, low_fit
        )

    # Top-10 ranked jobs detail
    logger.info("-" * 80)
    logger.info("%-4s %-45s %-8s %-8s %-8s %-5s %-5s %-5s",
                "Rank", "Job Title", "Final%", "KeyW%", "Sim%", "Match", "Part", "Gaps")
    logger.info("-" * 80)
    for rank, job in enumerate(scored_jobs[:10], 1):
        title = (job.get("title") or "")[:44]
        matched_count = len(job.get("matched_skills", []))
        partial_count = len(job.get("partial_skills", []))
        gap_count = len(skills) - matched_count - partial_count
        logger.info(
            "%-4d %-45s %-8d %-8d %-8d %-5d %-5d %-5d",
            rank, title,
            job["match_score"], job["keyword_score"], job["tfidf_score"],
            matched_count, partial_count, gap_count
        )

    # Log skill coverage for the #1 ranked job
    if scored_jobs:
        top = scored_jobs[0]
        logger.info("-" * 80)
        logger.info("🏆 Top Match Deep Dive: '%s' at %s", top.get("title"), top.get("companyName") or top.get("company"))
        logger.info("   ✅ Matched Skills (%d): %s", len(top.get("matched_skills", [])),
                     ", ".join(top.get("matched_skills", [])[:15]) or "(none)")
        logger.info("   🟡 Partial Skills (%d): %s", len(top.get("partial_skills", [])),
                     ", ".join(top.get("partial_skills", [])[:10]) or "(none)")
        gaps = [s for s in skills if s not in (top.get("matched_skills", []) + top.get("partial_skills", []))]
        logger.info("   ❌ Missing Skills (%d): %s", len(gaps),
                     ", ".join(gaps[:10]) or "(none)")

    logger.info("=" * 80)

    return scored_jobs


# ---------------------------------------------------------------------------
# Routes — Static files
# ---------------------------------------------------------------------------


@app.route("/")
def serve_index():
    """Serve the front-end ``index.html`` from the project root."""
    return send_from_directory(str(BASE_DIR), "index.html")


@app.route("/index.css")
def serve_css():
    """Serve the front-end stylesheet."""
    return send_from_directory(str(BASE_DIR), "index.css")


@app.route("/app.js")
def serve_js():
    """Serve the front-end JavaScript bundle."""
    return send_from_directory(str(BASE_DIR), "app.js")


# ---------------------------------------------------------------------------
# Routes — Job scraping
# ---------------------------------------------------------------------------


@app.route("/api/scrape", methods=["POST"])
def scrape_jobs():
    """Start an Apify LinkedIn-jobs scraping run and return the results.

    **Request body (JSON)**::

        {
          "keywords": "ServiceNow Developer",
          "location": "United States",
          "maxItems": 25,
          "datePosted": "past-week",
          "jobType": ["full-time"],
          "experienceLevel": ["mid-senior-level"],
          "workplaceType": ["remote"]
        }

    **Response (JSON)**: array of job objects on success, or an error
    object with an ``error`` key on failure.
    """
    try:
        config = _load_config()
        token: Optional[str] = config.get("apify_token")
        actor_id: str = config.get("actor_id", "curious_coder/linkedin-jobs-scraper")

        if not token or token == "YOUR_APIFY_TOKEN_HERE":
            return jsonify({"error": "Apify token not configured. Please set the APIFY_TOKEN environment variable or configure config.json."}), 400

        body: Dict[str, Any] = request.get_json(silent=True) or {}

        # Build LinkedIn job search URLs from the user's parameters.
        # Support multiple locations (semicolon-separated) and multiple keywords (comma-separated)
        keywords: str = body.get("keywords", config.get("default_keywords", ""))
        location: str = body.get("location", config.get("default_location", ""))
        max_items: int = body.get("maxItems", config.get("default_max_items", 25))

        kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
        if not kw_list:
            kw_list = [""]

        loc_list = [l.strip() for l in location.split(";") if l.strip()]
        if not loc_list:
            loc_list = [""]

        # Map filter values to LinkedIn URL parameters
        date_posted = body.get("datePosted", "")
        date_map = {
            "past24hours": "r86400",
            "past3Days":   "r259200",
            "pastWeek":    "r604800",
            "pastMonth":   "r2592000",
        }
        job_type = body.get("jobType", "")
        jt_map = {
            "full-time": "F",
            "part-time": "P",
            "contract": "C",
            "internship": "I",
        }
        exp_level = body.get("experienceLevel", "")
        exp_map = {
            "entry": "2",
            "associate": "3",
            "mid-senior": "4",
            "director": "5",
            "executive": "6",
        }
        workplace = body.get("workplaceType", "")
        wp_map = {
            "on-site": "1",
            "remote": "2",
            "hybrid": "3",
        }

        from urllib.parse import quote_plus
        search_urls: List[str] = []
        for kw in kw_list:
            for loc in loc_list:
                search_params = []
                if kw:
                    search_params.append(f"keywords={quote_plus(kw)}")
                if loc:
                    search_params.append(f"location={quote_plus(loc)}")

                if date_posted and date_posted in date_map:
                    search_params.append(f"f_TPR={date_map[date_posted]}")
                if job_type and job_type in jt_map:
                    search_params.append(f"f_JT={jt_map[job_type]}")
                if exp_level and exp_level in exp_map:
                    search_params.append(f"f_E={exp_map[exp_level]}")
                if workplace and workplace in wp_map:
                    search_params.append(f"f_WT={wp_map[workplace]}")

                search_urls.append("https://www.linkedin.com/jobs/search/?" + "&".join(search_params))

        actor_input: Dict[str, Any] = {
            "urls": search_urls,
            "count": max_items,
        }

        logger.info(
            "Starting Apify actor run with %d URL configurations (Keywords: %s | Locations: %s | Max items: %d)",
            len(search_urls),
            kw_list,
            loc_list,
            max_items
        )

        # 1. Start the actor run ------------------------------------------------
        # Apify API requires tilde (~) separator in URL paths, not slash (/)
        actor_id_url = actor_id.replace("/", "~")
        start_url = f"{APIFY_BASE_URL}/acts/{actor_id_url}/runs?token={token}"
        start_resp = requests.post(start_url, json=actor_input, timeout=30)
        
        if start_resp.status_code == 401:
            return jsonify({"error": "Invalid or expired Apify token."}), 401
        elif start_resp.status_code == 402:
            return jsonify({"error": "Monthly compute units exhausted / Payment required."}), 402
        elif start_resp.status_code == 429:
            return jsonify({"error": "Rate limit exceeded on Apify. Please try again later."}), 429
        elif not start_resp.ok:
            return jsonify({"error": f"Failed to start Apify actor run: {start_resp.text}"}), start_resp.status_code

        run_data: Dict[str, Any] = start_resp.json()["data"]
        run_id: str = run_data["id"]
        logger.info("Actor run started — run_id=%s", run_id)

        # 2. Poll for completion ------------------------------------------------
        poll_url = f"{APIFY_BASE_URL}/actor-runs/{run_id}?token={token}"
        for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
            time.sleep(POLL_INTERVAL_SECONDS)
            poll_resp = requests.get(poll_url, timeout=15)
            if poll_resp.status_code == 401:
                return jsonify({"error": "Invalid or expired Apify token."}), 401
            elif poll_resp.status_code == 429:
                return jsonify({"error": "Rate limit exceeded on Apify. Please try again later."}), 429
            poll_resp.raise_for_status()
            status: str = poll_resp.json()["data"]["status"]
            logger.info("  Poll #%d — status=%s", attempt, status)

            if status == "SUCCEEDED":
                break
            if status in ("FAILED", "TIMED-OUT", "ABORTED"):
                return jsonify({"error": f"Apify run ended: {status}. Check your actor configuration."}), 502
        else:
            return jsonify({"error": "Apify request timed out."}), 504

        # 3. Fetch dataset items ------------------------------------------------
        dataset_id: str = poll_resp.json()["data"]["defaultDatasetId"]
        items_url = f"{APIFY_BASE_URL}/datasets/{dataset_id}/items?token={token}"
        items_resp = requests.get(items_url, timeout=30)
        if items_resp.status_code == 401:
            return jsonify({"error": "Invalid or expired Apify token."}), 401
        elif items_resp.status_code == 429:
            return jsonify({"error": "Rate limit exceeded on Apify. Please try again later."}), 429
        items_resp.raise_for_status()
        raw_jobs: List[Dict[str, Any]] = items_resp.json()

        # Deduplicate job items from overlapping search queries
        seen_ids = set()
        jobs: List[Dict[str, Any]] = []
        for j in raw_jobs:
            jid = j.get("id") or j.get("link") or f"{j.get('title')}_{j.get('companyName')}"
            if jid not in seen_ids:
                seen_ids.add(jid)
                jobs.append(j)

        logger.info("Fetched %d job items (deduplicated to %d unique listings) from dataset %s", len(raw_jobs), len(jobs), dataset_id)

        # ---- Scrape Data Quality Debug Report ----
        logger.info("=" * 80)
        logger.info("🔍 SCRAPE DATA QUALITY REPORT")
        logger.info("=" * 80)
        has_title = sum(1 for j in jobs if j.get("title"))
        has_company = sum(1 for j in jobs if j.get("companyName") or j.get("company"))
        has_desc = sum(1 for j in jobs if (j.get("descriptionText") or j.get("description") or "").strip())
        has_location = sum(1 for j in jobs if j.get("location"))
        has_link = sum(1 for j in jobs if j.get("applyUrl") or j.get("link"))
        has_date = sum(1 for j in jobs if j.get("postedAt") or j.get("postedDate"))
        has_emp_type = sum(1 for j in jobs if j.get("employmentType") or j.get("jobType"))
        has_applicants = sum(1 for j in jobs if j.get("applicantsCount"))

        total = len(jobs) or 1
        logger.info("Field Completeness (out of %d jobs):", len(jobs))
        logger.info("   Title:          %d/%d (%.0f%%)", has_title, len(jobs), has_title / total * 100)
        logger.info("   Company:        %d/%d (%.0f%%)", has_company, len(jobs), has_company / total * 100)
        logger.info("   Description:    %d/%d (%.0f%%)", has_desc, len(jobs), has_desc / total * 100)
        logger.info("   Location:       %d/%d (%.0f%%)", has_location, len(jobs), has_location / total * 100)
        logger.info("   Apply Link:     %d/%d (%.0f%%)", has_link, len(jobs), has_link / total * 100)
        logger.info("   Posted Date:    %d/%d (%.0f%%)", has_date, len(jobs), has_date / total * 100)
        logger.info("   Employment Type:%d/%d (%.0f%%)", has_emp_type, len(jobs), has_emp_type / total * 100)
        logger.info("   Applicants:     %d/%d (%.0f%%)", has_applicants, len(jobs), has_applicants / total * 100)

        # Flag jobs with missing descriptions (these will produce poor match scores)
        empty_desc_jobs = [j.get("title", "Untitled") for j in jobs if not (j.get("descriptionText") or j.get("description") or "").strip()]
        if empty_desc_jobs:
            logger.warning("⚠️  %d jobs have EMPTY descriptions (match accuracy will be low):", len(empty_desc_jobs))
            for title in empty_desc_jobs[:5]:
                logger.warning("      → %s", title)

        # Average description length
        desc_lengths = [len((j.get("descriptionText") or j.get("description") or "").strip()) for j in jobs]
        if desc_lengths:
            avg_len = sum(desc_lengths) / len(desc_lengths)
            min_len = min(desc_lengths)
            max_len = max(desc_lengths)
            logger.info("Description Length → Avg: %d chars | Min: %d | Max: %d", avg_len, min_len, max_len)

        logger.info("=" * 80)

        # Enrich jobs before saving
        jobs = _enrich_jobs_with_companies(jobs)

        # Fetch and cache embeddings for job descriptions at scrape time
        try:
            descriptions = [
                j.get("descriptionText") or j.get("description") or "" for j in jobs
            ]
            if descriptions:
                logger.info("Generating dense embeddings for %d job descriptions...", len(jobs))
                embeddings = _get_embeddings(descriptions)
                for idx, j in enumerate(jobs):
                    j["embedding"] = embeddings[idx]
                logger.info("Successfully generated and cached embeddings for all scraped jobs.")
        except Exception as exc:
            logger.warning("Could not pre-calculate embeddings at scrape time: %s", exc)

        # 4. Cache results to disk ----------------------------------------------
        _ensure_data_dir()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        cache_path = DATA_DIR / f"jobs_{timestamp}.json"
        _save_json(jobs, cache_path)
        logger.info("Cached job results to %s", cache_path)

        return jsonify(jobs), 200

    except FileNotFoundError:
        logger.exception("config.json not found")
        return jsonify({"error": "config.json not found. Create it in the project root."}), 500
    except requests.HTTPError as exc:
        logger.exception("HTTP error during Apify interaction")
        status_code = exc.response.status_code if exc.response is not None else 502
        if status_code == 401:
            return jsonify({"error": "Invalid or expired Apify token."}), 401
        elif status_code == 402:
            return jsonify({"error": "Monthly compute units exhausted / Payment required."}), 402
        elif status_code == 429:
            return jsonify({"error": "Rate limit exceeded on Apify. Please try again later."}), 429
        elif status_code == 504:
            return jsonify({"error": "Apify request timed out."}), 504
        return jsonify({"error": f"Apify HTTP error ({status_code}): {exc}"}), status_code
    except requests.RequestException as exc:
        logger.exception("Network error during Apify interaction")
        return jsonify({"error": f"Network error communicating with Apify: {exc}"}), 502
    except Exception as exc:
        logger.exception("Unexpected error in /api/scrape")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/jobs", methods=["GET"])
def get_jobs():
    """Return the most recently cached job results.

    **Response (JSON)**: array of job objects, or an empty array if no
    cache exists.
    """
    try:
        limit = int(request.args.get("limit", 25))
        keywords = request.args.get("keywords")
        location = request.args.get("location")

        jobs = _get_recent_cached_jobs(limit=limit, keywords=keywords, location=location)
        jobs = _enrich_jobs_with_companies(jobs)
        logger.info("Returning %d recent cached jobs (limit=%d, keywords=%s, location=%s)", len(jobs), limit, keywords, location)
        return jsonify(jobs), 200

    except Exception as exc:
        logger.exception("Error reading cached jobs")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/limits", methods=["GET"])
def get_api_limits():
    """Return real-time quota and rate-limit statistics for Hugging Face and Apify."""
    config = _load_config()
    apify_token = config.get("apify_token")
    hf_token = _get_hf_token()

    report = {
        "apify": {"connected": False, "remaining_usd": 0.0, "total_usd": 5.0, "scrapes_left": 0},
        "huggingface": {
            "connected": bool(hf_token),
            "remaining_requests": _hf_quota_tracker["remaining_requests"],
            "limit_requests": _hf_quota_tracker["limit_requests"],
            "model_status": _hf_quota_tracker["model_status"]
        }
    }

    if apify_token and apify_token != "YOUR_APIFY_TOKEN_HERE":
        try:
            r = requests.get(f"{APIFY_BASE_URL}/users/me/limits?token={apify_token}", timeout=6)
            if r.status_code == 200:
                payload = r.json().get("data", {})
                limits_data = payload.get("limits", {})
                current_data = payload.get("current", {})

                # Check both nested Apify v2 schema and top-level fallback
                limit = float(limits_data.get("maxMonthlyUsageUsd") or payload.get("monthlyUsageLimitUsd") or 5.0)
                used = float(current_data.get("monthlyUsageUsd") or payload.get("currentMonthlyUsageUsd") or 0.0)
                remaining = max(0.0, limit - used)

                report["apify"] = {
                    "connected": True,
                    "used_usd": round(used, 2),
                    "remaining_usd": round(remaining, 2),
                    "total_usd": round(limit, 2),
                    "percent_used": round((used / limit) * 100, 1) if limit else 0,
                    "scrapes_left": max(0, int(remaining / 0.10))  # ~ $0.10 per search run
                }
        except Exception as e:
            logger.warning("Apify limits fetch error: %s", e)

    return jsonify(report), 200


# ---------------------------------------------------------------------------
# Routes — Resume management
# ---------------------------------------------------------------------------


@app.route("/api/resume/upload", methods=["POST"])
def upload_resume():
    """Upload a DOCX resume and extract structured profile data.

    Expects a ``multipart/form-data`` request with a file field named
    ``resume``.

    **Response (JSON)**: the extracted profile containing ``skills``,
    ``experience``, ``education``, and ``full_text``.
    """
    global resume_profile

    try:
        if "resume" not in request.files:
            return jsonify({"error": "No file part named 'resume' in the request."}), 400

        file = request.files["resume"]
        if file.filename == "":
            return jsonify({"error": "No file selected."}), 400

        filename = file.filename.lower()
        if not (filename.endswith(".docx") or filename.endswith(".pdf")):
            return jsonify({"error": "Only .pdf and .docx files are supported."}), 400

        full_text = ""
        if filename.endswith(".pdf"):
            reader = PdfReader(file)
            full_text = "\n".join(
                page.extract_text() or "" for page in reader.pages
            )
        elif filename.endswith(".docx"):
            doc = Document(file)
            full_text = "\n".join(
                paragraph.text for paragraph in doc.paragraphs if paragraph.text.strip()
            )

        if not full_text.strip():
            return jsonify({"error": "The uploaded document appears to be empty."}), 400

        logger.info("Extracted %d characters from uploaded resume", len(full_text))

        # Parse sections
        resume_profile = _extract_resume_sections(full_text)

        # Calculate and cache resume embedding
        try:
            logger.info("Generating dense embedding for uploaded resume...")
            # Fallback text if full_text is empty
            text_to_embed = full_text.strip() if full_text.strip() else " ".join(resume_profile.get("skills", []))
            resume_profile["embedding"] = _get_embedding(text_to_embed)
            logger.info("Successfully generated and cached resume embedding.")
        except Exception as exc:
            logger.warning("Could not calculate resume embedding at upload time: %s", exc)

        # Persist to disk
        _ensure_data_dir()
        _save_json(resume_profile, RESUME_PROFILE_PATH)
        logger.info("Saved resume profile to %s", RESUME_PROFILE_PATH)

        return jsonify(resume_profile), 200

    except Exception as exc:
        logger.exception("Error processing resume upload")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/resume", methods=["DELETE"])
def delete_resume():
    """Remove the uploaded resume, clear in-memory cache and delete persisted profile file."""
    global resume_profile
    try:
        resume_profile = {
            "skills": [],
            "core_skills": [],
            "experience": [],
            "education": [],
            "full_text": "",
        }
        if RESUME_PROFILE_PATH.exists():
            RESUME_PROFILE_PATH.unlink()
            logger.info("Deleted persisted resume profile file at %s", RESUME_PROFILE_PATH)

        return jsonify({"message": "Resume attachment removed and cache cleared successfully."}), 200
    except Exception as exc:
        logger.exception("Error clearing resume profile")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/resume/skills", methods=["GET"])
def get_skills():
    """Return the current list of extracted/edited resume skills and core skills.

    **Response (JSON)**::

        {"skills": ["Python", "ServiceNow", ...], "core_skills": ["ServiceNow"]}
    """
    return jsonify({
        "skills": resume_profile.get("skills", []),
        "core_skills": resume_profile.get("core_skills", [])
    }), 200


@app.route("/api/resume/skills", methods=["PUT"])
def update_skills():
    """Replace the resume skills and core skills list.

    **Request body (JSON)**::

        {"skills": ["Python", "ServiceNow", ...], "core_skills": ["ServiceNow"]}

    **Response (JSON)**: the updated skills object.
    """
    global resume_profile

    try:
        body = request.get_json(silent=True) or {}
        new_skills: List[str] = body.get("skills", [])
        new_core: List[str] = body.get("core_skills", [])

        if not isinstance(new_skills, list) or not isinstance(new_core, list):
            return jsonify({"error": "'skills' and 'core_skills' must be arrays of strings."}), 400

        resume_profile["skills"] = new_skills
        resume_profile["core_skills"] = new_core

        # Recalculate/update embedding if full_text is empty
        full_text = resume_profile.get("full_text", "")
        if not full_text.strip() and new_skills:
            try:
                logger.info("Generating dense embedding for updated skills list...")
                resume_profile["embedding"] = _get_embedding(" ".join(new_skills))
                logger.info("Successfully updated skills-based resume embedding.")
            except Exception as exc:
                logger.warning("Could not calculate embedding for updated skills: %s", exc)

        # Persist changes
        _ensure_data_dir()
        _save_json(resume_profile, RESUME_PROFILE_PATH)
        logger.info("Updated skills list (%d skills, %d core)", len(new_skills), len(new_core))

        return jsonify({
            "skills": new_skills,
            "core_skills": new_core
        }), 200

    except Exception as exc:
        logger.exception("Error updating skills")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Routes — Matching
# ---------------------------------------------------------------------------


@app.route("/api/match", methods=["POST"])
def match_jobs():
    """Score jobs against the resume profile and return ranked results.

    **Request body (JSON, optional)**::

        {
          "jobs": [...],     // if omitted, uses cached jobs
          "skills": [...],   // if omitted, uses stored resume skills
          "core_skills": []  // if omitted, uses stored core skills
        }

    **Response (JSON)**: array of job objects sorted by ``match_score``
    (descending), each augmented with match scoring details.
    """
    try:
        body: Dict[str, Any] = request.get_json(silent=True) or {}

        # Resolve jobs: from request body or from cache
        jobs: List[Dict[str, Any]] = body.get("jobs", [])
        if not jobs:
            limit = int(body.get("maxItems", 25))
            jobs = _get_recent_cached_jobs(limit=limit)
            if jobs:
                logger.info("Using %d recent cached jobs for matching", len(jobs))
            else:
                return jsonify({"error": "No jobs provided and no cached jobs found. Scrape jobs first."}), 400

        # Resolve skills: from request body or from stored profile
        skills: List[str] = body.get("skills", [])
        if not skills:
            skills = resume_profile.get("skills", [])
        if not skills:
            return jsonify({"error": "No skills provided and no resume profile found. Upload a resume first."}), 400

        # Resolve core skills
        core_skills: List[str] = body.get("core_skills", resume_profile.get("core_skills", []))

        # Resolve full resume text
        full_text: str = resume_profile.get("full_text", "")
        if not full_text:
            # Fallback: join skills as a minimal document
            full_text = " ".join(skills)

        logger.info(
            "Matching %d jobs against %d skills (%d core) …",
            len(jobs),
            len(skills),
            len(core_skills),
        )

        mode = body.get("mode", "semantic")
        scored = _compute_match_scores(jobs, skills, full_text, core_skills, mode=mode)
        scored = _enrich_jobs_with_companies(scored)

        return jsonify(scored), 200

    except Exception as exc:
        logger.exception("Error in /api/match")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Application entry-point
# ---------------------------------------------------------------------------


def _startup() -> None:
    """Run one-time initialisation tasks before the first request."""
    _ensure_data_dir()
    _load_resume_profile()
    _load_india_remote_companies()
    _load_career_and_recruiter_indices()


# Run startup initialization immediately so gunicorn workers initialize properly
_startup()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    banner = f"""
    +-------------------------------------------------------+
    |   LinkedIn Job Scraper + Resume Matcher API Server     |
    |                                                       |
    |   -> http://localhost:{port}                             |
    +-------------------------------------------------------+
    """
    print(banner)

    app.run(host="0.0.0.0", port=port, debug=False)
