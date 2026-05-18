import unittest
from unittest.mock import patch

from app.services import llm


class TestLlmService(unittest.TestCase):
    def test_normalize_text_response_preserves_paragraph_breaks(self):
        response = "first paragraph\r\n\r\nsecond paragraph\n"

        normalized = llm._normalize_text_response(response, "test")

        self.assertEqual(normalized, "first paragraph\n\nsecond paragraph")

    def test_generate_terms_returns_empty_list_on_provider_error(self):
        with patch.object(llm, "_generate_response", return_value="Error: missing key"):
            terms = llm.generate_terms("subject", "script")

        self.assertEqual(terms, [])

    def test_generate_terms_parses_pretty_json(self):
        pretty_json = '[\n  "office workers",\n  "city street"\n]'
        with patch.object(llm, "_generate_response", return_value=pretty_json):
            terms = llm.generate_terms("subject", "script")

        self.assertEqual(terms, ["office workers", "city street"])


if __name__ == "__main__":
    unittest.main()
