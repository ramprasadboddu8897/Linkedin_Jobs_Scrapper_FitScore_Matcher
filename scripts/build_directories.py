"""scripts/build_directories.py.

Indexes company career portal URLs and corporate recruiter telemetry.
Outputs:
  - data/career_sites.json
  - data/recruiters_index.json
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import docx

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("build_directories")

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
RECRUITERS_DIR = DATA_DIR / "recruiters"
CAREER_URLS_TXT = DATA_DIR / "Career_page_urls.txt"
CAREER_DOCX = DATA_DIR / "Company_Career_Sites_by_Industry.docx"

CAREER_SITES_OUTPUT = DATA_DIR / "career_sites.json"
RECRUITERS_OUTPUT = DATA_DIR / "recruiters_index.json"

CORPORATE_SUFFIXES = {
    "inc",
    "llc",
    "ltd",
    "co",
    "corp",
    "corporation",
    "pvt",
    "private",
    "limited",
    "solutions",
    "services",
    "technologies",
    "technology",
    "group",
    "careers",
    "career",
}


def clean_company_name(name: str) -> str:
    """Normalizes company names for deterministic dictionary lookup."""
    if not name:
        return ""
    name = name.lower().strip()
    # Replace underscores and hyphens with spaces so filenames like AAA_Careers split cleanly
    name = re.sub(r"[_\-]+", " ", name)
    # Remove file extensions or parenthetical notes
    name = re.sub(r"\.(js|json)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\(.*?\)", "", name)
    name = re.sub(r"[^\w\s]", " ", name)
    words = [w for w in name.split() if w not in CORPORATE_SUFFIXES]
    return " ".join(words)


def parse_numeric(val: Any) -> int:
    """Converts string numbers with commas ('2,892') or raw numbers to integers safely."""
    if isinstance(val, (int, float)):
        return int(val)
    if not val:
        return 0
    cleaned = re.sub(r"[^\d]", "", str(val))
    return int(cleaned) if cleaned else 0


def is_recruiter_active(recruiter: dict) -> tuple[bool, str]:
    """Checks recruiter activity status with multi-tiered fallbacks.

    Returns:
        tuple: (is_active: bool, status_label: str)
    """
    raw_active = (
        recruiter.get("activeNow")
        or recruiter.get("Active")
        or recruiter.get("active")
        or recruiter.get("isActive")
    )

    profiles_viewed = parse_numeric(recruiter.get("profilesViewed", 0))
    overall_actions = parse_numeric(recruiter.get("overallActions", 0))
    logins = parse_numeric(recruiter.get("logins", 0))

    if raw_active is not None:
        val_str = str(raw_active).strip().upper()
        if val_str in ("TRUE", "1", "YES"):
            return True, "Active Now"
        if val_str in ("FALSE", "0", "NO"):
            if profiles_viewed > 0 or overall_actions > 0:
                return True, "Active Recent"
            return False, "Inactive"

    # Tier 2: Field is completely missing — evaluate telemetry heuristics
    if profiles_viewed > 0 or overall_actions > 0 or logins > 0:
        return True, "Active (Telemetry)"

    # Tier 3: Zero recorded activity
    return False, "Hiring Team Contact"


def load_recruiter_data(file_path: Path) -> List[Dict[str, Any]]:
    """Loads array data from .json or .js files, handling non-breaking spaces and JS quirks."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore").strip()
        if not content:
            return []

        # Replace non-breaking spaces (\xa0) with regular ASCII space
        content = content.replace("\xa0", " ")

        # Strip standard JS variable assignments: e.g. const data = [...]; or var x = [...]
        start_bracket = content.find("[")
        end_bracket = content.rfind("]")
        if start_bracket != -1 and end_bracket != -1:
            json_str = content[start_bracket : end_bracket + 1]
        else:
            json_str = content

        # Remove single line comments // ...
        json_str = re.sub(r"//.*$", "", json_str, flags=re.MULTILINE)
        # Remove multi-line comments /* ... */
        json_str = re.sub(r"/\*.*?\*/", "", json_str, flags=re.DOTALL)
        # Remove trailing commas before closing braces/brackets
        json_str = re.sub(r",\s*([\]\}])", r"\1", json_str)

        return json.loads(json_str)
    except Exception as exc:
        logger.warning("Failed to parse %s: %s", file_path.name, exc)
        return []


def parse_career_docx(docx_path: Path) -> Dict[str, str]:
    """Parses company career portals from Word document tables using python-docx."""
    career_map: Dict[str, str] = {}
    if not docx_path.exists():
        logger.warning("Docx not found at %s", docx_path)
        return career_map

    try:
        doc = docx.Document(docx_path)
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                if len(cells) >= 2:
                    company_name = cells[0]
                    url_text = cells[1]

                    # Skip header row or empty cells
                    if not company_name or not url_text:
                        continue
                    if "company name" in company_name.lower() or "career site" in url_text.lower():
                        continue

                    # Ensure proper URL scheme
                    if not re.match(r"^https?://", url_text, re.IGNORECASE):
                        # Verify it looks like a domain before prefixing
                        if "." in url_text and not " " in url_text:
                            url_text = f"https://{url_text}"
                        else:
                            continue

                    cleaned_name = clean_company_name(company_name)
                    if cleaned_name and cleaned_name not in career_map:
                        career_map[cleaned_name] = url_text

        logger.info("Indexed %d career site URLs from DOCX tables", len(career_map))
    except Exception as exc:
        logger.warning("Failed to parse docx tables from %s: %s", docx_path.name, exc)

    return career_map


# Multi-tenant ATS platforms where the company is in the subdomain or path
ATS_PLATFORMS = {
    "phenompeople.com",
    "phenompeople.net",
    "phenompro.com",
    "myworkdayjobs.com",
    "greenhouse.io",
    "lever.co",
    "icims.com",
    "smartrecruiters.com",
}

DISALLOWED_KEYS = {
    "mycareer",
    "career",
    "careers",
    "job",
    "jobs",
    "work",
    "talent",
    "portal",
    "internal",
    "corp",
    "www",
    "apply",
    "boards",
}


def extract_company_from_url(url: str) -> str:
    """Extracts the real corporate name from a career portal URL."""
    parsed = urlparse(url)
    netloc = parsed.netloc.lower().split(":")[0]
    path = parsed.path.strip("/")

    # 1. Multi-tenant ATS: company might be in path or subdomain
    if any(ats in netloc for ats in ("lever.co", "greenhouse.io", "smartrecruiters.com")):
        path_parts = [p for p in path.split("/") if p]
        if path_parts:
            return path_parts[0]

    for platform in ATS_PLATFORMS:
        if netloc.endswith(platform):
            sub = netloc[: -len(platform)].rstrip(".")
            parts = sub.split(".")
            if parts and parts[-1] not in ("jobs", "careers", "boards", "apply"):
                return parts[-1]

    # 2. Standard corporate domains (e.g. mycareer.airasia.com -> airasia, careers.microsoft.com -> microsoft)
    parts = netloc.split(".")

    # Handle two-part TLDs (e.g., .co.uk, .com.au, .com.my)
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "gov", "edu"}:
        return parts[-3]
    elif len(parts) >= 2:
        return parts[-2]

    return parts[0]


def build_career_sites_index() -> Dict[str, str]:
    """Parses career portal text and documents into a normalized company->URL map."""
    career_map: Dict[str, str] = {}

    # 1. Parse Word document tables FIRST (explicit Company Name column takes priority)
    docx_map = parse_career_docx(CAREER_DOCX)
    for k, v in docx_map.items():
        if k and k not in DISALLOWED_KEYS:
            career_map[k] = v

    logger.info("Indexed %d career site URLs from DOCX tables", len(career_map))

    # 2. Parse plain text file as supplementary
    if CAREER_URLS_TXT.exists():
        txt_count = 0
        for line in CAREER_URLS_TXT.read_text(
            encoding="utf-8", errors="ignore"
        ).splitlines():
            url = line.strip()
            if not url or not url.startswith("http"):
                continue

            company_slug = extract_company_from_url(url)
            clean_key = clean_company_name(company_slug)

            if clean_key and clean_key not in DISALLOWED_KEYS and clean_key not in career_map:
                career_map[clean_key] = url
                txt_count += 1

        logger.info("Indexed %d additional career site URLs from TXT", txt_count)

    logger.info("Total combined career sites indexed: %d", len(career_map))
    return career_map


def build_recruiters_index() -> Dict[str, List[Dict[str, Any]]]:
    """Scans data/recruiters/ for .json and .js files and structures recruiters by company."""
    recruiters_by_company: Dict[str, List[Dict[str, Any]]] = {}

    if not RECRUITERS_DIR.exists():
        logger.warning(
            "Recruiters directory not found at %s. Creating it.", RECRUITERS_DIR
        )
        RECRUITERS_DIR.mkdir(parents=True, exist_ok=True)
        return recruiters_by_company

    # Match both *.json and *.js files
    files = list(RECRUITERS_DIR.glob("*.json")) + list(
        RECRUITERS_DIR.glob("*.js")
    )

    for file_path in files:
        # Deduce company name from filename (e.g., 'AAA_Careers.js' -> 'aaa')
        base_name = file_path.stem
        cleaned_company = clean_company_name(base_name)
        if not cleaned_company:
            cleaned_company = base_name.lower()

        raw_recruiters = load_recruiter_data(file_path)
        if not raw_recruiters:
            continue

        active_list: List[Dict[str, Any]] = []
        fallback_list: List[Dict[str, Any]] = []

        for r in raw_recruiters:
            name = r.get("name", "").strip()
            email = r.get("email", "").strip()
            if not name or not email:
                continue

            profiles = parse_numeric(r.get("profilesViewed", 0))
            actions = parse_numeric(r.get("overallActions", 0))
            logins = parse_numeric(r.get("logins", 0))
            is_active, status_label = is_recruiter_active(r)

            record = {
                "name": name,
                "email": email,
                "status": status_label,
                "profilesViewed": profiles,
                "overallActions": actions,
                "logins": logins,
            }

            if is_active:
                active_list.append(record)
            else:
                fallback_list.append(record)

        # Sort candidates by activity (profilesViewed, overallActions, logins)
        active_list.sort(
            key=lambda x: (x["profilesViewed"], x["overallActions"], x["logins"]), reverse=True
        )
        fallback_list.sort(
            key=lambda x: (x["profilesViewed"], x["overallActions"], x["logins"]), reverse=True
        )

        # If no recruiters passed the active check, fall back to top contacts
        selected_recruiters = (
            active_list if active_list else fallback_list[:3]
        )

        if selected_recruiters:
            recruiters_by_company[cleaned_company] = selected_recruiters

    logger.info("Indexed recruiters for %d companies", len(recruiters_by_company))
    return recruiters_by_company


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    career_sites = build_career_sites_index()
    with open(CAREER_SITES_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(career_sites, f, indent=2)
    logger.info("Saved %s (%d entries)", CAREER_SITES_OUTPUT, len(career_sites))

    recruiters_index = build_recruiters_index()
    with open(RECRUITERS_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(recruiters_index, f, indent=2)
    logger.info("Saved %s (%d companies)", RECRUITERS_OUTPUT, len(recruiters_index))


if __name__ == "__main__":
    main()
