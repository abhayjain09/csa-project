"""Focused prompt-contract tests for targeted grounded-search recovery."""

import ast
import unittest
from pathlib import Path


LAMBDA_PATH = Path(__file__).resolve().parent / "lambda.py"


def _load_prompt_builder():
    tree = ast.parse(LAMBDA_PATH.read_text(encoding="utf-8"))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef)
        and item.name == "_document_search_prompt"
    )
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {}
    exec(compile(module, str(LAMBDA_PATH), "exec"), namespace)
    return namespace["_document_search_prompt"]


class RecoveryPromptTests(unittest.TestCase):
    def test_targeted_phase_demands_exact_company_and_direct_document(self):
        prompt = _load_prompt_builder()(
            '"Cisco Systems, Inc." "sustainability report" filetype:pdf',
            "sustainability report",
            2025,
            "Cisco Systems, Inc.",
            "CSCO",
            "us",
            synonyms=["purpose report"],
            search_phase="targeted_recovery",
        )
        self.assertIn("Recovery search", prompt)
        self.assertIn("exact legal company identity", prompt)
        self.assertIn("direct official PDF", prompt)
        self.assertIn("Do not return a similarly named company", prompt)


if __name__ == "__main__":
    unittest.main()
