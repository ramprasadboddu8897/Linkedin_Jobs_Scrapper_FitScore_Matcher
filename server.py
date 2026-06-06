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
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# ---------------------------------------------------------------------------
# Constants & paths
# ---------------------------------------------------------------------------

BASE_DIR: Path = Path(__file__).resolve().parent
DATA_DIR: Path = BASE_DIR / "data"
CONFIG_PATH: Path = BASE_DIR / "config.json"
RESUME_PROFILE_PATH: Path = DATA_DIR / "resume_profile.json"

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
        "actor_id": os.environ.get("APIFY_ACTOR_ID", "apify/linkedin-jobs-scraper"),
        "default_keywords": os.environ.get("DEFAULT_KEYWORDS", ""),
        "default_location": os.environ.get("DEFAULT_LOCATION", ""),
        "default_max_items": int(os.environ.get("DEFAULT_MAX_ITEMS", "25")),
    }

    # 2. If token is not in env, try reading config.json
    if not config["apify_token"] and CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                file_config = json.load(fh)
                # Override none/defaults with file contents
                for k, v in file_config.items():
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
    """Clean company name by removing punctuation and corporate suffixes."""
    if not name:
        return ""
    name = name.lower()
    name = re.sub(r'[^\w\s]', '', name)
    words = name.split()
    suffixes = {"inc", "llc", "ltd", "co", "corp", "corporation", "pvt", "private", "limited", "solutions", "services", "technologies", "technology"}
    cleaned_words = [w for w in words if w not in suffixes]
    return " ".join(cleaned_words)


def _enrich_jobs_with_companies(jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add is_partner_company and partner_company_info to jobs that match target list."""
    companies = _load_india_remote_companies()
    if not companies:
        return jobs

    enriched_jobs = []
    for job in jobs:
        company_name = job.get("companyName") or job.get("company") or ""
        cleaned_job = _clean_company_name(company_name)

        is_match = False
        match_info = None

        if cleaned_job:
            for comp in companies:
                base = comp["base_name"].lower().replace("-", "")
                
                # Direct match
                if cleaned_job == base:
                    is_match = True
                    match_info = comp
                    break

                # Substring match (base in job_company or vice-versa)
                if len(base) >= 4 and (base in cleaned_job or cleaned_job in base):
                    is_match = True
                    match_info = comp
                    break

                # Check collapsed spaces match
                raw_company_clean = re.sub(r'[^\w\s]', '', company_name.lower()).replace(" ", "")
                if base in raw_company_clean or raw_company_clean in base:
                    if len(base) >= 4 or base == raw_company_clean:
                        is_match = True
                        match_info = comp
                        break

        enriched = {**job}
        if is_match:
            enriched["is_partner_company"] = True
            enriched["partner_company_info"] = match_info
        else:
            enriched["is_partner_company"] = False
            enriched["partner_company_info"] = None

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


# Synonym mapping for common tech terms, abbreviations, and alternate spellings.
# All keys and values must be lowercase.
_TECH_SYNONYMS: Dict[str, List[str]] = {
    "js": ["javascript", "js"],
    "javascript": ["javascript", "js"],
    "aws": ["aws", "amazon web services"],
    "amazon web services": ["aws", "amazon web services"],
    "node": ["node", "nodejs", "node.js"],
    "nodejs": ["node", "nodejs", "node.js"],
    "node.js": ["node", "nodejs", "node.js"],
    "react": ["react", "reactjs", "react.js"],
    "reactjs": ["react", "reactjs", "react.js"],
    "react.js": ["react", "reactjs", "react.js"],
    "servicenow": ["servicenow", "service now", "service-now"],
    "service now": ["servicenow", "service now", "service-now"],
    "service-now": ["servicenow", "service now", "service-now"],
    "db": ["db", "database", "databases"],
    "database": ["db", "database", "databases"],
    "rca": ["rca", "root cause analysis"],
    "root cause analysis": ["rca", "root cause analysis"],
    "api": ["api", "apis", "rest api", "restful api", "web service", "web services"],
    "apis": ["api", "apis", "rest api", "restful api", "web service", "web services"],
    "ci/cd": ["ci/cd", "cicd", "continuous integration", "continuous deployment"],
    "cicd": ["ci/cd", "cicd", "continuous integration", "continuous deployment"],
    "ui": ["ui", "user interface", "frontend", "front-end"],
    "frontend": ["ui", "user interface", "frontend", "front-end"],
    "front-end": ["ui", "user interface", "frontend", "front-end"],
    "ux": ["ux", "user experience"],
    "user experience": ["ux", "user experience"],
    "qa": ["qa", "quality assurance", "testing", "test engineer"],
    "kpi": ["kpi", "kpis", "key performance indicators"],
}


_CONCEPT_GROUPS: List[Dict[str, Any]] = [
    {
        "name": "API & Web Debugging",
        "terms": ["api", "apis", "rest api", "restful api", "http", "http protocols", "json", "payload", "api authentication", "api debugging", "api logs analysis", "web service", "web services", "postman"]
    },
    {
        "name": "Logging & Monitoring",
        "terms": ["logs", "logging", "monitoring", "grafana", "splunk", "dynatrace", "kibana", "elk", "cloudwatch", "prometheus", "alerting", "api logs analysis", "log analysis"]
    },
    {
        "name": "Database & Queries",
        "terms": ["sql", "mysql", "postgresql", "mongodb", "queries", "nosql", "database", "databases", "db", "querying", "mongodb queries"]
    },
    {
        "name": "ServiceNow & ITSM",
        "terms": ["servicenow", "service now", "service-now", "flow designer", "workflow", "workflows", "service catalog", "sla", "csm", "itsm", "incident management", "problem management", "workflow configuration", "servicenow studio"]
    },
    {
        "name": "Technical Documentation",
        "terms": ["technical documentation", "documentation", "technical writing", "wiki", "confluence", "kb", "knowledge base", "knowledge articles", "runbook", "playbook"]
    },
    {
        "name": "Troubleshooting & RCA",
        "terms": ["troubleshooting", "debugging", "root cause analysis", "rca", "incident investigation", "problem resolution", "issue resolution", "customer issue resolution", "incident management"]
    }
]


def _check_concept_match(skill_lower: str, description_lower: str) -> Optional[Dict[str, Any]]:
    """Check if a skill matches semantically via shared concept groups."""
    for group in _CONCEPT_GROUPS:
        belongs_to_group = False
        for term in group["terms"]:
            if term == skill_lower or (len(term) > 3 and term in skill_lower):
                belongs_to_group = True
                break
                
        if belongs_to_group:
            matched_terms = []
            for term in group["terms"]:
                if term == skill_lower or (len(term) > 3 and term in skill_lower):
                    continue
                is_match = False
                if term.isalnum() and len(term) <= 2:
                    if re.search(r'\b' + re.escape(term) + r'\b', description_lower):
                        is_match = True
                else:
                    if term in description_lower:
                        is_match = True
                if is_match:
                    matched_terms.append(term)
                    
            if len(matched_terms) >= 3:
                return {
                    "matched": True,
                    "reason": "concept_match",
                    "concept_group": group["name"],
                    "matched_context_terms": matched_terms
                }
    return None


def _analyze_skill_match(skill: str, description_lower: str) -> Dict[str, Any]:
    """Analyze a skill match against a job description, returning match details and partial status."""
    skill_lower = skill.lower().strip()
    if not skill_lower:
        return {"matched": False, "partial": False, "reason": "empty", "tokens": []}
        
    # 1. Whole skill phrase check (with synonyms)
    search_variants = [skill_lower]
    if skill_lower in _TECH_SYNONYMS:
        search_variants.extend(_TECH_SYNONYMS[skill_lower])
        
    for variant in search_variants:
        is_match = False
        if variant.isalnum() and len(variant) <= 2:
            if re.search(r'\b' + re.escape(variant) + r'\b', description_lower):
                is_match = True
        else:
            if variant in description_lower:
                is_match = True
        if is_match:
            return {
                "matched": True,
                "partial": False,
                "reason": "phrase_match",
                "matched_variant": variant,
                "tokens": [{"token": skill_lower, "matched": True, "variants": search_variants}]
            }
            
    # 2. Token-based fallback check (for multi-word skills where the description contains
    # constituent words but possibly in different positions/order)
    skill_clean = re.sub(r'[^\w\s\+\#\-\.\/]', ' ', skill_lower)
    tokens = [t.strip() for t in skill_clean.split() if t.strip()]
    if not tokens:
        return {"matched": False, "partial": False, "reason": "no_tokens", "tokens": []}
        
    stop_words = {'and', 'or', 'of', 'in', 'with', 'for', 'a', 'an', 'the', 'to', 'at', 'by', 'on', 'using', 'experience'}
    filtered_tokens = tokens
    if len(tokens) > 1:
        filtered_tokens = [t for t in tokens if t not in stop_words]
        if not filtered_tokens:
            filtered_tokens = tokens
            
    token_details = []
    matched_count = 0
    
    for token in filtered_tokens:
        token_variants = [token]
        if token in _TECH_SYNONYMS:
            token_variants.extend(_TECH_SYNONYMS[token])
            
        token_matched = False
        matched_variant = None
        for variant in token_variants:
            if variant.isalnum() and len(variant) <= 2:
                if re.search(r'\b' + re.escape(variant) + r'\b', description_lower):
                    token_matched = True
                    matched_variant = variant
                    break
            else:
                if variant in description_lower:
                    token_matched = True
                    matched_variant = variant
                    break
        
        token_details.append({
            "token": token,
            "matched": token_matched,
            "matched_variant": matched_variant,
            "variants": token_variants
        })
        if token_matched:
            matched_count += 1
            
    all_matched = (matched_count == len(filtered_tokens))
    match_ratio = matched_count / len(filtered_tokens) if filtered_tokens else 0.0
    
    if all_matched:
        return {
            "matched": True,
            "partial": False,
            "semantic": False,
            "reason": "token_match",
            "match_ratio": match_ratio,
            "tokens": token_details
        }
        
    # Check semantic concept match fallback
    concept_match = _check_concept_match(skill_lower, description_lower)
    if concept_match:
        return {
            "matched": True,
            "partial": False,
            "semantic": True,
            "reason": "concept_match",
            "concept_group": concept_match["concept_group"],
            "matched_context_terms": concept_match["matched_context_terms"],
            "match_ratio": 1.0,
            "tokens": token_details
        }
        
    is_partial = (match_ratio >= 0.5)
    return {
        "matched": False,
        "partial": is_partial,
        "semantic": False,
        "reason": "partial_match" if is_partial else "missing_tokens",
        "match_ratio": match_ratio,
        "tokens": token_details
    }


def _skill_matches_job(skill: str, description: str) -> bool:
    """Legacy helper returning simple True/False match flag."""
    return _analyze_skill_match(skill, description.lower())["matched"]


def _compute_match_scores(
    jobs: List[Dict[str, Any]],
    skills: List[str],
    full_resume_text: str,
    core_skills: List[str] = None
) -> List[Dict[str, Any]]:
    """Score and rank *jobs* against the applicant's profile.

    The final score is a weighted combination:

        ``final = 0.6 × keyword_score + 0.4 × tfidf_score``

    Both component scores are normalised to the 0–100 range.
    TF-IDF score is scaled to account for natural document-length disparity.

    Args:
        jobs: List of job dicts; each must contain a ``description`` field.
        skills: List of skill strings extracted from the resume.
        full_resume_text: The full plaintext of the resume.
        core_skills: List of starred core skill strings.

    Returns:
        A copy of *jobs* augmented with ``match_score``, ``keyword_score``,
        ``tfidf_score``, ``matched_skills``, ``partial_skills``, and
        ``skills_analysis`` fields, sorted descending by ``match_score``.
    """
    if not jobs:
        return []

    core_skills_lower = [c.lower() for c in core_skills] if core_skills else []

    # ---- Keyword matching ----
    keyword_results: List[Dict[str, Any]] = []
    for job in jobs:
        description: str = (job.get("descriptionText") or job.get("description") or "").lower()
        
        matched_skills = []
        partial_skills = []
        skills_analysis = {}
        kw_score_accumulator = 0.0
        total_weight = 0.0
        
        for skill in skills:
            analysis = _analyze_skill_match(skill, description)
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

    # ---- TF-IDF cosine similarity ----
    tfidf_scores: List[float] = []
    if full_resume_text.strip():
        descriptions: List[str] = [
            job.get("descriptionText") or job.get("description") or "" for job in jobs
        ]
        corpus: List[str] = [full_resume_text] + descriptions
        vectorizer = TfidfVectorizer(stop_words="english")
        tfidf_matrix = vectorizer.fit_transform(corpus)
        cos_sim = cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:])
        tfidf_scores = cos_sim[0].tolist()
    else:
        tfidf_scores = [0.0] * len(jobs)

    # ---- Combine scores ----
    scored_jobs: List[Dict[str, Any]] = []
    for idx, job in enumerate(jobs):
        kw_score_100 = round(keyword_results[idx]["keyword_score"] * 100)
        
        # Scale TF-IDF score to fit 0-100 better, since raw cosine similarity of different length texts is naturally low
        raw_tfidf = tfidf_scores[idx]
        scaled_tfidf = min(raw_tfidf * 2.5, 1.0)
        tfidf_score_100 = round(scaled_tfidf * 100)
        
        final_score = round(
            0.6 * keyword_results[idx]["keyword_score"] * 100
            + 0.4 * scaled_tfidf * 100
        )

        enriched = {**job}
        enriched["match_score"] = min(final_score, 100)
        enriched["keyword_score"] = kw_score_100
        enriched["tfidf_score"] = tfidf_score_100
        enriched["matched_skills"] = keyword_results[idx]["matched_skills"]
        enriched["partial_skills"] = keyword_results[idx]["partial_skills"]
        enriched["skills_analysis"] = keyword_results[idx]["skills_analysis"]
        scored_jobs.append(enriched)

    scored_jobs.sort(key=lambda j: j["match_score"], reverse=True)
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
        actor_id: str = config.get("actor_id", "apify/linkedin-jobs-scraper")

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
            "pastWeek": "r604800",
            "pastMonth": "r2592000",
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
        start_resp.raise_for_status()
        run_data: Dict[str, Any] = start_resp.json()["data"]
        run_id: str = run_data["id"]
        logger.info("Actor run started — run_id=%s", run_id)

        # 2. Poll for completion ------------------------------------------------
        poll_url = f"{APIFY_BASE_URL}/actor-runs/{run_id}?token={token}"
        for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
            time.sleep(POLL_INTERVAL_SECONDS)
            poll_resp = requests.get(poll_url, timeout=15)
            poll_resp.raise_for_status()
            status: str = poll_resp.json()["data"]["status"]
            logger.info("  Poll #%d — status=%s", attempt, status)

            if status == "SUCCEEDED":
                break
            if status in ("FAILED", "TIMED-OUT", "ABORTED"):
                return jsonify({"error": f"Apify actor run ended with status: {status}"}), 502
        else:
            return jsonify({"error": "Apify actor run polling timed out."}), 504

        # 3. Fetch dataset items ------------------------------------------------
        dataset_id: str = poll_resp.json()["data"]["defaultDatasetId"]
        items_url = f"{APIFY_BASE_URL}/datasets/{dataset_id}/items?token={token}"
        items_resp = requests.get(items_url, timeout=30)
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

        # Enrich jobs before saving
        jobs = _enrich_jobs_with_companies(jobs)

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
    except requests.RequestException as exc:
        logger.exception("HTTP error during Apify interaction")
        return jsonify({"error": f"HTTP error communicating with Apify: {exc}"}), 502
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
        cache_path = _latest_jobs_cache()
        if cache_path is None:
            logger.info("No cached job files found — returning empty list")
            return jsonify([]), 200

        jobs = _load_json(cache_path)
        jobs = _enrich_jobs_with_companies(jobs)
        logger.info("Returning %d cached jobs from %s", len(jobs), cache_path.name)
        return jsonify(jobs), 200

    except Exception as exc:
        logger.exception("Error reading cached jobs")
        return jsonify({"error": str(exc)}), 500


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

        if not file.filename.lower().endswith(".docx"):
            return jsonify({"error": "Only .docx files are supported."}), 400

        # Extract text from the DOCX using python-docx
        doc = Document(file)
        full_text = "\n".join(
            paragraph.text for paragraph in doc.paragraphs if paragraph.text.strip()
        )

        if not full_text.strip():
            return jsonify({"error": "The uploaded document appears to be empty."}), 400

        logger.info("Extracted %d characters from uploaded resume", len(full_text))

        # Parse sections
        resume_profile = _extract_resume_sections(full_text)

        # Persist to disk
        _ensure_data_dir()
        _save_json(resume_profile, RESUME_PROFILE_PATH)
        logger.info("Saved resume profile to %s", RESUME_PROFILE_PATH)

        return jsonify(resume_profile), 200

    except Exception as exc:
        logger.exception("Error processing resume upload")
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
            cache_path = _latest_jobs_cache()
            if cache_path:
                jobs = _load_json(cache_path)
                logger.info("Using %d cached jobs for matching", len(jobs))
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

        scored = _compute_match_scores(jobs, skills, full_text, core_skills)
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


if __name__ == "__main__":
    _startup()

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
