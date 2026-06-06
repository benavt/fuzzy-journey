#!/usr/bin/env python3
"""Fetch recent and highly cited papers for an ORCID and download PDFs."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, parse, request


OPENALEX_API = "https://api.openalex.org"
ORCID_PUBLIC_API = "https://pub.orcid.org/v3.0"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_RECENT_LIMIT = 5
DEFAULT_CITED_LIMIT = 5
USER_AGENT = "OpenClaw ORCID workflow/1.0"


class WorkflowError(RuntimeError):
    """Raised when the workflow cannot complete successfully."""


@dataclass
class AuthorRecord:
    openalex_id: str
    display_name: str
    orcid: str


def normalize_orcid(orcid: str) -> str:
    """Return a bare ORCID identifier from mixed input formats."""
    cleaned = orcid.strip()
    cleaned = re.sub(r"^https?://orcid\.org/", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace(" ", "")

    if not re.fullmatch(r"\d{4}-\d{4}-\d{4}-[\dX]{4}", cleaned, flags=re.IGNORECASE):
        raise WorkflowError(
            "ORCID must look like 0000-0000-0000-0000 or https://orcid.org/0000-0000-0000-0000"
        )

    return cleaned.upper()


def build_headers(mailto: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if mailto:
        headers["From"] = mailto
    return headers


def fetch_json(url: str, *, headers: dict[str, str]) -> dict[str, Any]:
    req = request.Request(url, headers=headers)
    try:
        with request.urlopen(req, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            return json.load(response)
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise WorkflowError(f"HTTP {exc.code} for {url}: {detail}") from exc
    except error.URLError as exc:
        raise WorkflowError(f"Network error for {url}: {exc.reason}") from exc


def find_author(orcid: str, *, headers: dict[str, str]) -> AuthorRecord:
    query = parse.urlencode(
        {
            "filter": f"orcid:https://orcid.org/{orcid}",
            "per-page": 1,
        }
    )
    payload = fetch_json(f"{OPENALEX_API}/authors?{query}", headers=headers)
    results = payload.get("results", [])
    if not results:
        raise WorkflowError(f"No OpenAlex author record found for ORCID {orcid}")

    author = results[0]
    return AuthorRecord(
        openalex_id=author["id"],
        display_name=author.get("display_name", "Unknown Author"),
        orcid=orcid,
    )


def fetch_orcid_record(orcid: str, *, headers: dict[str, str]) -> dict[str, Any]:
    return fetch_json(f"{ORCID_PUBLIC_API}/{orcid}/record", headers=headers)


def fetch_works(
    author: AuthorRecord,
    *,
    sort: str,
    limit: int,
    headers: dict[str, str],
) -> list[dict[str, Any]]:
    query = parse.urlencode(
        {
            "filter": f"author.id:{author.openalex_id}",
            "sort": sort,
            "per-page": limit,
        }
    )
    payload = fetch_json(f"{OPENALEX_API}/works?{query}", headers=headers)
    return list(payload.get("results", []))


def extract_pdf_url(work: dict[str, Any]) -> str | None:
    candidates: list[str | None] = []

    best_oa = work.get("best_oa_location") or {}
    primary = work.get("primary_location") or {}
    open_access = work.get("open_access") or {}

    candidates.append(best_oa.get("pdf_url"))
    candidates.append(primary.get("pdf_url"))
    candidates.append(open_access.get("oa_url"))

    for location in work.get("locations", []):
        candidates.append(location.get("pdf_url"))
        landing_page_url = location.get("landing_page_url")
        if isinstance(landing_page_url, str) and landing_page_url.lower().endswith(".pdf"):
            candidates.append(landing_page_url)

    for candidate in candidates:
        if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
            return candidate

    return None


def extract_value(node: Any) -> Any:
    if isinstance(node, dict) and "value" in node:
        return node.get("value")
    return node


def compact(items: list[Any]) -> list[Any]:
    return [item for item in items if item not in (None, "", [], {})]


def format_partial_date(date_node: dict[str, Any] | None) -> str | None:
    if not isinstance(date_node, dict):
        return None

    year = extract_value(date_node.get("year"))
    month = extract_value(date_node.get("month"))
    day = extract_value(date_node.get("day"))

    if not year:
        return None

    parts = [f"{int(year):04d}"]
    if month:
        parts.append(f"{int(month):02d}")
    if day:
        parts.append(f"{int(day):02d}")
    return "-".join(parts)


def extract_organization(org_node: dict[str, Any] | None) -> dict[str, Any]:
    org_node = org_node or {}
    address = org_node.get("address") or {}
    disambiguated = org_node.get("disambiguated-organization") or {}
    return {
        "name": extract_value(org_node.get("name")),
        "city": extract_value(address.get("city")),
        "region": extract_value(address.get("region")),
        "country": extract_value(address.get("country")),
        "disambiguated_organization_identifier": extract_value(
            disambiguated.get("disambiguated-organization-identifier")
        ),
        "disambiguation_source": extract_value(disambiguated.get("disambiguation-source")),
    }


def extract_affiliation_summaries(
    section: dict[str, Any] | None, summary_key: str
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    section = section or {}
    for group in section.get("affiliation-group", []):
        for summary in group.get("summaries", []):
            payload = summary.get(summary_key)
            if payload:
                summaries.append(payload)
    return summaries


def normalize_affiliation(item: dict[str, Any], kind: str) -> dict[str, Any]:
    return {
        "type": kind,
        "department_name": extract_value(item.get("department-name")),
        "role_title": extract_value(item.get("role-title")),
        "start_date": format_partial_date(item.get("start-date")),
        "end_date": format_partial_date(item.get("end-date")),
        "organization": extract_organization(item.get("organization")),
        "visibility": item.get("visibility"),
        "put_code": item.get("put-code"),
    }


def extract_researcher_urls(person: dict[str, Any]) -> list[dict[str, str]]:
    entries = (person.get("researcher-urls") or {}).get("researcher-url", [])
    urls: list[dict[str, str]] = []
    for entry in entries:
        url_value = extract_value(entry.get("url"))
        if url_value:
            urls.append(
                {
                    "name": extract_value(entry.get("url-name")) or "profile",
                    "url": url_value,
                }
            )
    return urls


def summarize_orcid_profile(record: dict[str, Any]) -> dict[str, Any]:
    person = record.get("person") or {}
    activities = record.get("activities-summary") or {}
    name = person.get("name") or {}

    employments = [
        normalize_affiliation(item, "employment")
        for item in extract_affiliation_summaries(activities.get("employments"), "employment-summary")
    ]
    educations = [
        normalize_affiliation(item, "education")
        for item in extract_affiliation_summaries(activities.get("educations"), "education-summary")
    ]
    qualifications = [
        normalize_affiliation(item, "qualification")
        for item in extract_affiliation_summaries(
            activities.get("qualifications"), "qualification-summary"
        )
    ]

    current_job_titles = compact(
        [
            item.get("role_title")
            for item in employments
            if item.get("role_title") and not item.get("end_date")
        ]
    )
    universities_attended = compact(
        [
            item.get("organization", {}).get("name")
            for item in educations
            if item.get("organization", {}).get("name")
        ]
    )
    degrees = compact(
        [
            item.get("role_title")
            for item in educations + qualifications
            if item.get("role_title")
        ]
    )
    domain_expertise = compact(
        [
            extract_value(keyword.get("content"))
            for keyword in (person.get("keywords") or {}).get("keyword", [])
        ]
    )
    countries = compact(
        [
            extract_value(address.get("country"))
            for address in (person.get("addresses") or {}).get("address", [])
        ]
    )

    return {
        "orcid_record_uri": (record.get("orcid-identifier") or {}).get("uri"),
        "display_name": " ".join(
            compact(
                [
                    extract_value(name.get("given-names")),
                    extract_value(name.get("family-name")),
                ]
            )
        )
        or None,
        "credit_name": extract_value(name.get("credit-name")),
        "biography": extract_value(person.get("biography", {}).get("content")),
        "locale": extract_value((record.get("preferences") or {}).get("locale")),
        "other_names": compact(
            [
                extract_value(item.get("content"))
                for item in (person.get("other-names") or {}).get("other-name", [])
            ]
        ),
        "researcher_urls": extract_researcher_urls(person),
        "external_identifiers": [
            {
                "type": extract_value(item.get("external-id-type")),
                "value": extract_value(item.get("external-id-value")),
                "url": extract_value(item.get("external-id-url", {}).get("value")),
            }
            for item in (person.get("external-identifiers") or {}).get("external-identifier", [])
        ],
        "countries": countries,
        "domain_expertise": domain_expertise,
        "current_job_titles": current_job_titles,
        "employment_history": employments,
        "educations": educations,
        "qualifications": qualifications,
        "degrees": degrees,
        "universities_attended": universities_attended,
        "unsupported_or_usually_unavailable_fields": {
            "age": None,
            "date_of_birth": None,
            "native_language": None,
            "gender": None,
            "race_ethnicity": None,
        },
    }


def build_identity_check(author: AuthorRecord, orcid_profile: dict[str, Any]) -> dict[str, Any]:
    orcid_name = orcid_profile.get("display_name")
    names_match = bool(orcid_name and author.display_name and orcid_name == author.display_name)
    warning = None
    if orcid_name and author.display_name and not names_match:
        warning = (
            "OpenAlex author name does not match the public ORCID profile name. "
            "Review the record manually before trusting the publication list."
        )

    return {
        "openalex_display_name": author.display_name,
        "orcid_display_name": orcid_name,
        "names_match": names_match,
        "warning": warning,
    }


def publication_year(work: dict[str, Any]) -> str:
    year = work.get("publication_year")
    return str(year) if year else "unknown"


def slugify(value: str) -> str:
    ascii_only = value.encode("ascii", errors="ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_only).strip("-").lower()
    return slug or "untitled"


def build_filename(work: dict[str, Any]) -> str:
    title = work.get("title") or "untitled"
    work_id = str(work.get("id", "work")).rstrip("/").split("/")[-1]
    return f"{publication_year(work)}-{slugify(title)[:80]}-{work_id}.pdf"


def download_pdf(url: str, destination: Path, *, headers: dict[str, str]) -> None:
    req = request.Request(url, headers=headers)
    try:
        with request.urlopen(req, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get("Content-Type", "")
            # Some providers send octet-stream or omit the type, so accept both.
            if "pdf" not in content_type.lower() and not url.lower().endswith(".pdf"):
                raise WorkflowError(f"URL did not return a PDF content type: {url}")
            destination.write_bytes(response.read())
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise WorkflowError(f"HTTP {exc.code} while downloading {url}: {detail}") from exc
    except error.URLError as exc:
        raise WorkflowError(f"Network error while downloading {url}: {exc.reason}") from exc


def summarize_work(work: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": work.get("id"),
        "title": work.get("title"),
        "publication_year": work.get("publication_year"),
        "publication_date": work.get("publication_date"),
        "cited_by_count": work.get("cited_by_count"),
        "doi": work.get("doi"),
        "pdf_url": extract_pdf_url(work),
    }


def merge_categories(
    recent: list[dict[str, Any]], cited: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}

    def upsert(work: dict[str, Any], category: str) -> None:
        work_id = str(work.get("id"))
        if work_id not in by_id:
            by_id[work_id] = summarize_work(work)
            by_id[work_id]["categories"] = [category]
        elif category not in by_id[work_id]["categories"]:
            by_id[work_id]["categories"].append(category)

    for work in recent:
        upsert(work, "recent")
    for work in cited:
        upsert(work, "most_cited")

    return list(by_id.values())


def process_downloads(
    works: list[dict[str, Any]],
    output_dir: Path,
    *,
    headers: dict[str, str],
    pause_seconds: float,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    output_dir.mkdir(parents=True, exist_ok=True)

    for work in works:
        item = dict(work)
        pdf_url = work.get("pdf_url")
        if not pdf_url:
            item["download_status"] = "skipped_no_pdf_url"
            results.append(item)
            continue

        destination = output_dir / build_filename(work)
        item["output_file"] = str(destination)

        try:
            download_pdf(pdf_url, destination, headers=headers)
            item["download_status"] = "downloaded"
        except WorkflowError as exc:
            item["download_status"] = "failed"
            item["download_error"] = str(exc)

        results.append(item)
        if pause_seconds > 0:
            time.sleep(pause_seconds)

    return results


def write_manifest(
    *,
    author: AuthorRecord,
    orcid_profile: dict[str, Any],
    identity_check: dict[str, Any],
    recent: list[dict[str, Any]],
    cited: list[dict[str, Any]],
    combined: list[dict[str, Any]],
    output_dir: Path,
) -> Path:
    manifest = {
        "author": {
            "display_name": author.display_name,
            "openalex_id": author.openalex_id,
            "orcid": author.orcid,
        },
        "orcid_profile": orcid_profile,
        "identity_check": identity_check,
        "recent_publications": recent,
        "most_cited_publications": cited,
        "downloads": combined,
    }

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find recent and most-cited publications for a professor ORCID and "
            "download available PDFs."
        )
    )
    parser.add_argument("orcid", help="Professor ORCID identifier or ORCID URL")
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory where PDFs and manifest.json will be written",
    )
    parser.add_argument(
        "--recent-limit",
        type=int,
        default=DEFAULT_RECENT_LIMIT,
        help=f"How many recent publications to inspect (default: {DEFAULT_RECENT_LIMIT})",
    )
    parser.add_argument(
        "--cited-limit",
        type=int,
        default=DEFAULT_CITED_LIMIT,
        help=f"How many most-cited publications to inspect (default: {DEFAULT_CITED_LIMIT})",
    )
    parser.add_argument(
        "--mailto",
        help="Optional contact email to include in API requests",
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=0.5,
        help="Delay between PDF downloads in seconds (default: 0.5)",
    )
    return parser.parse_args(argv)


def validate_positive(value: int, name: str) -> None:
    if value <= 0:
        raise WorkflowError(f"{name} must be greater than zero")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    try:
        orcid = normalize_orcid(args.orcid)
        validate_positive(args.recent_limit, "recent-limit")
        validate_positive(args.cited_limit, "cited-limit")

        headers = build_headers(args.mailto)
        orcid_record = fetch_orcid_record(orcid, headers=headers)
        orcid_profile = summarize_orcid_profile(orcid_record)
        author = find_author(orcid, headers=headers)
        identity_check = build_identity_check(author, orcid_profile)
        recent_raw = fetch_works(
            author,
            sort="publication_date:desc",
            limit=args.recent_limit,
            headers=headers,
        )
        cited_raw = fetch_works(
            author,
            sort="cited_by_count:desc",
            limit=args.cited_limit,
            headers=headers,
        )

        recent = [summarize_work(work) for work in recent_raw]
        cited = [summarize_work(work) for work in cited_raw]
        combined = merge_categories(recent_raw, cited_raw)
        output_dir = Path(args.output_dir).expanduser().resolve()
        combined = process_downloads(
            combined,
            output_dir,
            headers=headers,
            pause_seconds=args.pause_seconds,
        )
        manifest_path = write_manifest(
            author=author,
            orcid_profile=orcid_profile,
            identity_check=identity_check,
            recent=recent,
            cited=cited,
            combined=combined,
            output_dir=output_dir,
        )

        downloaded = sum(1 for item in combined if item["download_status"] == "downloaded")
        skipped = sum(1 for item in combined if item["download_status"] == "skipped_no_pdf_url")
        failed = sum(1 for item in combined if item["download_status"] == "failed")

        print(f"Author: {author.display_name} ({author.orcid})")
        if identity_check.get("warning"):
            print(f"Warning: {identity_check['warning']}")
        if orcid_profile.get("current_job_titles"):
            print(f"Current job titles: {', '.join(orcid_profile['current_job_titles'])}")
        if orcid_profile.get("universities_attended"):
            print(f"Universities attended: {', '.join(orcid_profile['universities_attended'])}")
        if orcid_profile.get("domain_expertise"):
            print(f"Domain expertise keywords: {', '.join(orcid_profile['domain_expertise'])}")
        print(f"Output directory: {output_dir}")
        print(f"Manifest: {manifest_path}")
        print(f"Downloaded PDFs: {downloaded}")
        print(f"Skipped (no PDF URL): {skipped}")
        print(f"Failed downloads: {failed}")
        return 0
    except WorkflowError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
