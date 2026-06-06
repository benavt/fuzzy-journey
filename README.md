# OpenClaw ORCID Workflow

This repository contains a small Python workflow that takes a professor's ORCID,
finds recent and most-cited publications through OpenAlex, and downloads any
available PDFs into an output directory. It also pulls public ORCID profile
metadata when available.

## Usage

```bash
python3 orcid_publications.py 0000-0002-1825-0097 --output-dir output
```

Optional flags:

- `--recent-limit 10` to inspect more recent works
- `--cited-limit 10` to inspect more highly cited works
- `--mailto you@example.com` to include a contact email in API requests
- `--pause-seconds 1.0` to slow down download requests

The workflow writes:

- PDFs into the chosen output directory
- `manifest.json` summarizing recent works, most cited works, ORCID profile
  fields, download status, and any failures

## ORCID Profile Fields

When the ORCID record exposes them publicly, the workflow stores:

- current job titles and employment history
- education history
- qualifications and degree-like titles
- universities or institutions listed in education items
- keywords that can serve as domain expertise hints
- biography, profile URLs, countries, and external identifiers

The workflow does not infer or fabricate sensitive demographics. ORCID public
records generally do not provide age, date of birth, native language, gender,
or race/ethnicity, so those are left unavailable in the manifest.

## Notes

- Metadata comes from the OpenAlex API and the ORCID public API.
- PDF downloads only succeed when OpenAlex exposes a direct PDF URL or an open
  access PDF link that resolves to a PDF.
- Job title, degrees, universities attended, and domain expertise are
  best-effort summaries of what the researcher has made public on their ORCID
  record. Many ORCID records are sparse or incomplete.
- Some publications may be listed in the manifest without a downloaded file if
  no PDF URL is available or the remote host blocks automated download requests.

## Tests

```bash
python3 -m unittest test_orcid_publications.py
```
