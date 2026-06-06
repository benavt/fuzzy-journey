import unittest

import orcid_publications as op


class OrcidPublicationsTests(unittest.TestCase):
    def test_normalize_orcid_accepts_url(self) -> None:
        self.assertEqual(
            op.normalize_orcid("https://orcid.org/0000-0002-1825-0097"),
            "0000-0002-1825-0097",
        )

    def test_normalize_orcid_rejects_bad_value(self) -> None:
        with self.assertRaises(op.WorkflowError):
            op.normalize_orcid("invalid")

    def test_extract_pdf_url_prefers_best_oa(self) -> None:
        work = {
            "best_oa_location": {"pdf_url": "https://example.org/best.pdf"},
            "primary_location": {"pdf_url": "https://example.org/primary.pdf"},
            "open_access": {"oa_url": "https://example.org/landing"},
            "locations": [],
        }
        self.assertEqual(op.extract_pdf_url(work), "https://example.org/best.pdf")

    def test_merge_categories_deduplicates_by_work_id(self) -> None:
        shared = {
            "id": "https://openalex.org/W123",
            "title": "Shared Work",
            "publication_year": 2024,
            "publication_date": "2024-06-01",
            "cited_by_count": 15,
            "doi": "https://doi.org/10.1/example",
            "best_oa_location": {"pdf_url": "https://example.org/work.pdf"},
            "primary_location": {},
            "open_access": {},
            "locations": [],
        }
        merged = op.merge_categories([shared], [shared])
        self.assertEqual(len(merged), 1)
        self.assertCountEqual(merged[0]["categories"], ["recent", "most_cited"])

    def test_build_filename_contains_year_and_work_id(self) -> None:
        work = {
            "id": "https://openalex.org/W999",
            "title": "A Study on Testing",
            "publication_year": 2023,
        }
        filename = op.build_filename(work)
        self.assertTrue(filename.startswith("2023-a-study-on-testing-"))
        self.assertTrue(filename.endswith("W999.pdf"))

    def test_summarize_orcid_profile_extracts_supported_fields(self) -> None:
        record = {
            "orcid-identifier": {"uri": "https://orcid.org/0000-0000-0000-0000"},
            "preferences": {"locale": "en"},
            "person": {
                "name": {
                    "given-names": {"value": "Ada"},
                    "family-name": {"value": "Lovelace"},
                    "credit-name": {"value": "Ada Lovelace"},
                },
                "biography": {"content": "Computing pioneer"},
                "keywords": {"keyword": [{"content": "algorithms"}]},
                "other-names": {"other-name": [{"content": "A. Lovelace"}]},
                "addresses": {"address": [{"country": {"value": "GB"}}]},
                "researcher-urls": {
                    "researcher-url": [
                        {"url-name": "Lab", "url": {"value": "https://example.org/lab"}}
                    ]
                },
                "external-identifiers": {
                    "external-identifier": [
                        {
                            "external-id-type": "Scopus Author ID",
                            "external-id-value": "12345",
                            "external-id-url": {"value": "https://example.org/scopus/12345"},
                        }
                    ]
                },
            },
            "activities-summary": {
                "employments": {
                    "affiliation-group": [
                        {
                            "summaries": [
                                {
                                    "employment-summary": {
                                        "department-name": "Computer Science",
                                        "role-title": "Professor",
                                        "organization": {"name": "Example University"},
                                        "start-date": {"year": {"value": "2020"}},
                                    }
                                }
                            ]
                        }
                    ]
                },
                "educations": {
                    "affiliation-group": [
                        {
                            "summaries": [
                                {
                                    "education-summary": {
                                        "department-name": "Mathematics",
                                        "role-title": "PhD",
                                        "organization": {"name": "Example College"},
                                    }
                                }
                            ]
                        }
                    ]
                },
                "qualifications": {
                    "affiliation-group": [
                        {
                            "summaries": [
                                {
                                    "qualification-summary": {
                                        "role-title": "MSc",
                                        "organization": {"name": "Example Institute"},
                                    }
                                }
                            ]
                        }
                    ]
                },
            },
        }

        profile = op.summarize_orcid_profile(record)
        self.assertEqual(profile["display_name"], "Ada Lovelace")
        self.assertEqual(profile["current_job_titles"], ["Professor"])
        self.assertEqual(profile["universities_attended"], ["Example College"])
        self.assertEqual(profile["degrees"], ["PhD", "MSc"])
        self.assertEqual(profile["domain_expertise"], ["algorithms"])
        self.assertEqual(profile["unsupported_or_usually_unavailable_fields"]["age"], None)

    def test_build_identity_check_warns_on_name_mismatch(self) -> None:
        author = op.AuthorRecord(
            openalex_id="https://openalex.org/A1",
            display_name="Different Person",
            orcid="0000-0000-0000-0000",
        )
        check = op.build_identity_check(author, {"display_name": "Ada Lovelace"})
        self.assertFalse(check["names_match"])
        self.assertIn("does not match", check["warning"])


if __name__ == "__main__":
    unittest.main()
