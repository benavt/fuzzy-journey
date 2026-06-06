# OpenClaw ORCID Workflow

This repository contains a small Python workflow that takes a professor's ORCID,
finds recent and most-cited publications through OpenAlex, and downloads any
available PDFs into an output directory. It also pulls public ORCID profile
metadata when available. It can also extract text from downloaded PDFs and,
optionally, generate markdown summaries grounded in the paper text and OpenAlex
reference metadata.

## Usage

```bash
OPENALEX_API_KEY=your_key_here \
python3 orcid_publications.py 0000-0002-1825-0097 --output-dir output --mailto you@example.com
```

With optional summarization:

```bash
OPENALEX_API_KEY=your_openalex_key \
OPENAI_API_KEY=your_openai_key \
python3 orcid_publications.py 0000-0002-1825-0097 \
  --output-dir output \
  --mailto you@example.com \
  --summarize-pdfs
```

Optional flags:

- `--recent-limit 10` to inspect more recent works
- `--cited-limit 10` to inspect more highly cited works
- `--mailto you@example.com` to include a contact email in API requests
- `--openalex-mailto you@example.com` to explicitly set OpenAlex's `mailto` query parameter
- `--openalex-api-key your_key_here` instead of using the `OPENALEX_API_KEY` environment variable
- `--max-retries 5` to retry `429` and server errors with exponential backoff
- `--backoff-seconds 1.0` to control the initial retry wait
- `--pause-seconds 1.0` to slow down download requests
- `--summarize-pdfs` to generate literature-grounded markdown summaries for extracted PDFs
- `--summary-model gpt-5-mini` to choose the OpenAI model used for summaries
- `--summary-max-chars 120000` to cap how much extracted text is sent per paper
- `--reference-limit 5` to control how many OpenAlex references are used as literature context
- `--openai-api-key your_key_here` instead of using the `OPENAI_API_KEY` environment variable

The workflow writes:

- PDFs into the chosen output directory
- extracted text files into `texts/` for PDFs where readable text can be recovered
- summary markdown files into `summaries/` when `--summarize-pdfs` is enabled
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
- OpenAlex now requires an API key for normal usage as of February 13, 2026.
- The workflow identifies itself politely to OpenAlex via the `mailto` query parameter
  and retries `429`/5xx responses with exponential backoff.
- PDF text extraction is best-effort and works best on digitally generated PDFs.
  Scanned/image-only PDFs may fail extraction.
- Summary generation uses the OpenAI Responses API when `--summarize-pdfs` is enabled
  and an `OPENAI_API_KEY` is available.
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
