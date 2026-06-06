#!/usr/bin/env python3
"""Fetch recent and highly cited papers for an ORCID and download PDFs."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sys
import time
import zlib
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
DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF_SECONDS = 1.0
OPENAI_RESPONSES_API = "https://api.openai.com/v1/responses"
DEFAULT_SUMMARY_MODEL = "gpt-5-mini"
DEFAULT_SUMMARY_MAX_CHARS = 120000
DEFAULT_REFERENCE_LIMIT = 5


class WorkflowError(RuntimeError):
    """Raised when the workflow cannot complete successfully."""


@dataclass
class AuthorRecord:
    openalex_id: str
    display_name: str
    orcid: str


@dataclass
class RequestOptions:
    headers: dict[str, str]
    openalex_mailto: str | None = None
    openalex_api_key: str | None = None
    openai_api_key: str | None = None
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS


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


def build_openalex_url(
    path: str, params: dict[str, Any], *, mailto: str | None, api_key: str | None
) -> str:
    enriched = {key: value for key, value in params.items() if value is not None}
    if mailto:
        enriched["mailto"] = mailto
    if api_key:
        enriched["api_key"] = api_key
    return f"{OPENALEX_API}{path}?{parse.urlencode(enriched)}"


def retry_delay(exc: error.HTTPError, attempt: int, base_delay: float) -> float:
    retry_after = exc.headers.get("Retry-After")
    if retry_after:
        try:
            return max(float(retry_after), 0.0)
        except ValueError:
            pass
    return base_delay * (2 ** (attempt - 1))


def fetch_json(url: str, *, options: RequestOptions) -> dict[str, Any]:
    req = request.Request(url, headers=options.headers)
    attempt = 0

    while True:
        attempt += 1
        try:
            with request.urlopen(req, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
                return json.load(response)
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            is_retryable = exc.code == 429 or 500 <= exc.code < 600
            if is_retryable and attempt <= options.max_retries:
                time.sleep(retry_delay(exc, attempt, options.backoff_seconds))
                continue
            raise WorkflowError(f"HTTP {exc.code} for {url}: {detail}") from exc
        except error.URLError as exc:
            if attempt <= options.max_retries:
                time.sleep(options.backoff_seconds * (2 ** (attempt - 1)))
                continue
            raise WorkflowError(f"Network error for {url}: {exc.reason}") from exc


def find_author(orcid: str, *, options: RequestOptions) -> AuthorRecord:
    url = build_openalex_url(
        "/authors",
        {
            "filter": f"orcid:https://orcid.org/{orcid}",
            "per-page": 1,
            "select": "id,display_name",
        },
        mailto=options.openalex_mailto,
        api_key=options.openalex_api_key,
    )
    payload = fetch_json(url, options=options)
    results = payload.get("results", [])
    if not results:
        raise WorkflowError(f"No OpenAlex author record found for ORCID {orcid}")

    author = results[0]
    return AuthorRecord(
        openalex_id=author["id"],
        display_name=author.get("display_name", "Unknown Author"),
        orcid=orcid,
    )


def fetch_orcid_record(orcid: str, *, options: RequestOptions) -> dict[str, Any]:
    return fetch_json(f"{ORCID_PUBLIC_API}/{orcid}/record", options=options)


def fetch_works(
    author: AuthorRecord,
    *,
    sort: str,
    limit: int,
    options: RequestOptions,
) -> list[dict[str, Any]]:
    url = build_openalex_url(
        "/works",
        {
            "filter": f"author.id:{author.openalex_id}",
            "sort": sort,
            "per-page": limit,
            "select": (
                "id,title,publication_year,publication_date,cited_by_count,doi,"
                "best_oa_location,primary_location,open_access,locations"
            ),
        },
        mailto=options.openalex_mailto,
        api_key=options.openalex_api_key,
    )
    payload = fetch_json(url, options=options)
    return list(payload.get("results", []))


def short_openalex_id(value: str | None) -> str | None:
    if not value:
        return None
    return str(value).rstrip("/").split("/")[-1]


def fetch_work_detail(work_id: str, *, options: RequestOptions) -> dict[str, Any]:
    short_id = short_openalex_id(work_id)
    if not short_id:
        raise WorkflowError(f"Invalid OpenAlex work id: {work_id}")
    url = build_openalex_url(
        f"/works/{short_id}",
        {
            "select": (
                "id,title,publication_year,publication_date,cited_by_count,doi,"
                "abstract_inverted_index,concepts,primary_location,referenced_works"
            )
        },
        mailto=options.openalex_mailto,
        api_key=options.openalex_api_key,
    )
    return fetch_json(url, options=options)


def fetch_reference_details(
    reference_ids: list[str], *, options: RequestOptions, limit: int
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for ref_id in reference_ids[:limit]:
        try:
            reference = fetch_work_detail(ref_id, options=options)
            details.append(
                {
                    "id": reference.get("id"),
                    "title": reference.get("title"),
                    "publication_year": reference.get("publication_year"),
                    "cited_by_count": reference.get("cited_by_count"),
                    "doi": reference.get("doi"),
                }
            )
        except WorkflowError:
            continue
    return details


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


def reconstruct_abstract(inverted_index: dict[str, list[int]] | None) -> str | None:
    if not inverted_index:
        return None
    positions: dict[int, str] = {}
    for word, indexes in inverted_index.items():
        for index in indexes:
            positions[index] = word
    if not positions:
        return None
    return " ".join(positions[index] for index in sorted(positions))


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
    biography = person.get("biography") or {}

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
        "biography": extract_value(biography.get("content")),
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


def decode_pdf_literal(value: bytes) -> str:
    result = bytearray()
    i = 0
    while i < len(value):
        char = value[i]
        if char != 0x5C:  # backslash
            result.append(char)
            i += 1
            continue

        i += 1
        if i >= len(value):
            break
        escaped = value[i]
        mapping = {
            ord("n"): b"\n",
            ord("r"): b"\r",
            ord("t"): b"\t",
            ord("b"): b"\b",
            ord("f"): b"\f",
            ord("("): b"(",
            ord(")"): b")",
            ord("\\"): b"\\",
        }
        if escaped in mapping:
            result.extend(mapping[escaped])
            i += 1
            continue
        if escaped in b"01234567":
            octal = bytes([escaped])
            i += 1
            for _ in range(2):
                if i < len(value) and value[i] in b"01234567":
                    octal += bytes([value[i]])
                    i += 1
                else:
                    break
            result.append(int(octal, 8) % 256)
            continue
        result.append(escaped)
        i += 1

    return result.decode("utf-8", errors="ignore")


def decode_pdf_hex(value: bytes) -> str:
    cleaned = re.sub(rb"\s+", b"", value)
    if len(cleaned) % 2 == 1:
        cleaned += b"0"
    try:
        return bytes.fromhex(cleaned.decode("ascii")).decode("utf-8", errors="ignore")
    except ValueError:
        return ""


def extract_pdf_strings(content: bytes) -> list[str]:
    strings: list[str] = []
    i = 0
    while i < len(content):
        char = content[i]
        if char == 0x28:  # (
            depth = 1
            i += 1
            start = i
            literal = bytearray()
            while i < len(content) and depth > 0:
                current = content[i]
                if current == 0x5C and i + 1 < len(content):
                    literal.extend(content[i : i + 2])
                    i += 2
                    continue
                if current == 0x28:
                    depth += 1
                elif current == 0x29:
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                if depth > 0:
                    literal.append(current)
                i += 1
            text = decode_pdf_literal(bytes(literal)).strip()
            if text:
                strings.append(text)
            continue
        if char == 0x3C and i + 1 < len(content) and content[i + 1] != 0x3C:  # <hex>
            i += 1
            start = i
            while i < len(content) and content[i] != 0x3E:
                i += 1
            text = decode_pdf_hex(content[start:i]).strip()
            if text:
                strings.append(text)
            i += 1
            continue
        i += 1
    return strings


def decode_pdf_stream(stream_bytes: bytes, filters: list[str]) -> bytes | None:
    decoded = stream_bytes
    try:
        for filter_name in filters:
            if filter_name == "ASCII85Decode":
                decoded = base64.a85decode(decoded, adobe=True)
            elif filter_name == "FlateDecode":
                decoded = zlib.decompress(decoded)
            else:
                return None
        return decoded
    except (ValueError, zlib.error, binascii.Error):
        return None


def extract_stream_filters(object_header: bytes) -> list[str]:
    match = re.search(rb"/Filter\s*(\[[^\]]+\]|/\w+)", object_header)
    if not match:
        return []
    raw = match.group(1)
    return [name.decode("ascii") for name in re.findall(rb"/([A-Za-z0-9]+)", raw)]


def normalize_extracted_text(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_text_from_pdf(pdf_path: Path) -> str:
    raw = pdf_path.read_bytes()
    text_chunks: list[str] = []
    pattern = re.compile(rb"<<.*?>>\s*stream\r?\n(.*?)\r?\nendstream", re.S)
    for match in pattern.finditer(raw):
        header = match.group(0).split(b"stream", 1)[0]
        stream_bytes = match.group(1)
        filters = extract_stream_filters(header)
        decoded = decode_pdf_stream(stream_bytes, filters) if filters else stream_bytes
        if not decoded:
            continue
        for block in re.findall(rb"BT(.*?)ET", decoded, re.S):
            strings = extract_pdf_strings(block)
            if strings:
                text_chunks.append(" ".join(strings))

    text = normalize_extracted_text("\n\n".join(text_chunks))
    if len(text) < 500:
        raise WorkflowError(
            f"Could not extract enough readable text from {pdf_path.name}; the PDF may be scanned or encoded."
        )
    return text


def write_text_artifacts(works: list[dict[str, Any]], output_dir: Path) -> list[dict[str, Any]]:
    text_dir = output_dir / "texts"
    text_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    for work in works:
        item = dict(work)
        output_file = item.get("output_file")
        if item.get("download_status") != "downloaded" or not output_file:
            item["full_text_status"] = "skipped_no_pdf"
            results.append(item)
            continue

        pdf_path = Path(output_file)
        text_path = text_dir / f"{pdf_path.stem}.txt"
        try:
            text = extract_text_from_pdf(pdf_path)
            text_path.write_text(text, encoding="utf-8")
            item["full_text_status"] = "extracted"
            item["full_text_path"] = str(text_path)
            item["full_text_chars"] = len(text)
        except WorkflowError as exc:
            item["full_text_status"] = "failed"
            item["full_text_error"] = str(exc)
        results.append(item)

    return results


def post_json(url: str, payload: dict[str, Any], *, headers: dict[str, str]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=DEFAULT_TIMEOUT_SECONDS * 4) as response:
            return json.load(response)
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise WorkflowError(f"HTTP {exc.code} for {url}: {detail}") from exc
    except error.URLError as exc:
        raise WorkflowError(f"Network error for {url}: {exc.reason}") from exc


def build_summary_prompt(
    work: dict[str, Any],
    work_detail: dict[str, Any],
    references: list[dict[str, Any]],
    full_text: str,
) -> str:
    abstract = reconstruct_abstract(work_detail.get("abstract_inverted_index"))
    concepts = [
        concept.get("display_name")
        for concept in work_detail.get("concepts", [])
        if concept.get("display_name")
    ][:10]
    reference_lines = [
        f"- {ref.get('title')} ({ref.get('publication_year')}) cited_by={ref.get('cited_by_count')}"
        for ref in references
        if ref.get("title")
    ]
    metadata = {
        "title": work.get("title"),
        "publication_year": work.get("publication_year"),
        "publication_date": work.get("publication_date"),
        "doi": work.get("doi"),
        "cited_by_count": work.get("cited_by_count"),
        "categories": work.get("categories", []),
    }
    return (
        "You are summarizing a scholarly paper for a researcher.\n"
        "Use the paper text as the primary source of truth. Use the OpenAlex metadata and reference list only as supporting context.\n"
        "Write markdown with these exact sections:\n"
        "## Overview\n"
        "## Research Question And Contribution\n"
        "## Methods And Evidence\n"
        "## Main Findings\n"
        "## Implications For The Field\n"
        "## Grounding In Other Literature\n"
        "## Limitations And Open Questions\n"
        "## Practical Takeaways\n"
        "In the 'Grounding In Other Literature' section, explicitly compare the work to the provided references when available.\n\n"
        f"Paper metadata:\n{json.dumps(metadata, indent=2)}\n\n"
        f"OpenAlex abstract:\n{abstract or 'Not available'}\n\n"
        f"OpenAlex concepts:\n{json.dumps(concepts, indent=2)}\n\n"
        f"Reference context:\n{chr(10).join(reference_lines) or 'No reference metadata available'}\n\n"
        "Extracted paper text follows:\n"
        f"{full_text}"
    )


def openai_summary_markdown(
    prompt: str,
    *,
    options: RequestOptions,
    model: str,
) -> str:
    if not options.openai_api_key:
        raise WorkflowError(
            "OPENAI_API_KEY required for summarization. Set the environment variable or pass --openai-api-key."
        )
    payload = {
        "model": model,
        "input": prompt,
    }
    headers = {
        "Authorization": f"Bearer {options.openai_api_key}",
        "Content-Type": "application/json",
    }
    response = post_json(OPENAI_RESPONSES_API, payload, headers=headers)
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    raise WorkflowError("OpenAI response did not include output_text.")


def summarize_downloaded_pdfs(
    works: list[dict[str, Any]],
    output_dir: Path,
    *,
    options: RequestOptions,
    model: str,
    summary_max_chars: int,
    reference_limit: int,
) -> list[dict[str, Any]]:
    summary_dir = output_dir / "summaries"
    summary_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    for work in works:
        item = dict(work)
        text_path = item.get("full_text_path")
        if item.get("full_text_status") != "extracted" or not text_path:
            item["summary_status"] = "skipped_no_text"
            results.append(item)
            continue

        try:
            full_text = Path(text_path).read_text(encoding="utf-8")[:summary_max_chars]
            work_detail = fetch_work_detail(str(item.get("id")), options=options)
            references = fetch_reference_details(
                work_detail.get("referenced_works", []),
                options=options,
                limit=reference_limit,
            )
            prompt = build_summary_prompt(item, work_detail, references, full_text)
            summary = openai_summary_markdown(prompt, options=options, model=model)
            summary_path = summary_dir / f"{Path(text_path).stem}.md"
            summary_path.write_text(summary, encoding="utf-8")
            item["summary_status"] = "generated"
            item["summary_path"] = str(summary_path)
            item["summary_model"] = model
        except WorkflowError as exc:
            item["summary_status"] = "failed"
            item["summary_error"] = str(exc)

        results.append(item)

    return results


def download_pdf(url: str, destination: Path, *, options: RequestOptions) -> None:
    req = request.Request(url, headers=options.headers)
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
    options: RequestOptions,
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
            download_pdf(pdf_url, destination, options=options)
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
    parser.add_argument(
        "--openalex-mailto",
        help=(
            "Email to include as the OpenAlex mailto query parameter. "
            "Defaults to --mailto if not set."
        ),
    )
    parser.add_argument(
        "--openalex-api-key",
        help=(
            "OpenAlex API key. Defaults to the OPENALEX_API_KEY environment variable."
        ),
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=f"Maximum retries for retryable API errors like 429 (default: {DEFAULT_MAX_RETRIES})",
    )
    parser.add_argument(
        "--backoff-seconds",
        type=float,
        default=DEFAULT_BACKOFF_SECONDS,
        help=f"Initial backoff for retries in seconds (default: {DEFAULT_BACKOFF_SECONDS})",
    )
    parser.add_argument(
        "--summarize-pdfs",
        action="store_true",
        help="Extract text from downloaded PDFs and generate markdown summaries.",
    )
    parser.add_argument(
        "--summary-model",
        default=DEFAULT_SUMMARY_MODEL,
        help=f"OpenAI model to use for PDF summaries (default: {DEFAULT_SUMMARY_MODEL})",
    )
    parser.add_argument(
        "--summary-max-chars",
        type=int,
        default=DEFAULT_SUMMARY_MAX_CHARS,
        help=(
            f"Maximum extracted text characters to send for each summary "
            f"(default: {DEFAULT_SUMMARY_MAX_CHARS})"
        ),
    )
    parser.add_argument(
        "--reference-limit",
        type=int,
        default=DEFAULT_REFERENCE_LIMIT,
        help=f"How many OpenAlex references to include in literature grounding (default: {DEFAULT_REFERENCE_LIMIT})",
    )
    parser.add_argument(
        "--openai-api-key",
        help="OpenAI API key. Defaults to the OPENAI_API_KEY environment variable.",
    )
    return parser.parse_args(argv)


def validate_positive(value: int, name: str) -> None:
    if value <= 0:
        raise WorkflowError(f"{name} must be greater than zero")


def validate_non_negative(value: float, name: str) -> None:
    if value < 0:
        raise WorkflowError(f"{name} must not be negative")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    try:
        orcid = normalize_orcid(args.orcid)
        validate_positive(args.recent_limit, "recent-limit")
        validate_positive(args.cited_limit, "cited-limit")
        validate_positive(args.max_retries, "max-retries")
        validate_positive(args.summary_max_chars, "summary-max-chars")
        validate_positive(args.reference_limit, "reference-limit")
        validate_non_negative(args.backoff_seconds, "backoff-seconds")

        headers = build_headers(args.mailto)
        openalex_mailto = args.openalex_mailto or args.mailto
        openalex_api_key = args.openalex_api_key or os.environ.get("OPENALEX_API_KEY")
        if not openalex_api_key:
            raise WorkflowError(
                "OpenAlex API key required. Set OPENALEX_API_KEY or pass --openalex-api-key."
            )

        options = RequestOptions(
            headers=headers,
            openalex_mailto=openalex_mailto,
            openalex_api_key=openalex_api_key,
            openai_api_key=args.openai_api_key or os.environ.get("OPENAI_API_KEY"),
            max_retries=args.max_retries,
            backoff_seconds=args.backoff_seconds,
        )

        orcid_record = fetch_orcid_record(orcid, options=options)
        orcid_profile = summarize_orcid_profile(orcid_record)
        author = find_author(orcid, options=options)
        identity_check = build_identity_check(author, orcid_profile)
        recent_raw = fetch_works(
            author,
            sort="publication_date:desc",
            limit=args.recent_limit,
            options=options,
        )
        cited_raw = fetch_works(
            author,
            sort="cited_by_count:desc",
            limit=args.cited_limit,
            options=options,
        )

        recent = [summarize_work(work) for work in recent_raw]
        cited = [summarize_work(work) for work in cited_raw]
        combined = merge_categories(recent_raw, cited_raw)
        output_dir = Path(args.output_dir).expanduser().resolve()
        combined = process_downloads(
            combined,
            output_dir,
            options=options,
            pause_seconds=args.pause_seconds,
        )
        combined = write_text_artifacts(combined, output_dir)
        if args.summarize_pdfs:
            combined = summarize_downloaded_pdfs(
                combined,
                output_dir,
                options=options,
                model=args.summary_model,
                summary_max_chars=args.summary_max_chars,
                reference_limit=args.reference_limit,
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
        extracted = sum(1 for item in combined if item.get("full_text_status") == "extracted")
        summaries = sum(1 for item in combined if item.get("summary_status") == "generated")

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
        print(f"Extracted full texts: {extracted}")
        if args.summarize_pdfs:
            print(f"Generated summaries: {summaries}")
        return 0
    except WorkflowError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
