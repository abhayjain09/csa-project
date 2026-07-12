import importlib.util
import os
from pathlib import Path
import sys
import unittest


os.environ["REQUIRE_LLM_VALIDATION"] = "false"
os.environ["FARGATE_BROWSER_QUEUE_URL"] = "https://sqs.example/jobs"
os.environ["FARGATE_BROWSER_RESULT_QUEUE_URL"] = "https://sqs.example/results"

AGENT_PATH = Path(__file__).resolve().parents[2] / "agent" / "agent.py"
AGENT_SPEC = importlib.util.spec_from_file_location("agent", AGENT_PATH)
assert AGENT_SPEC and AGENT_SPEC.loader
agent = importlib.util.module_from_spec(AGENT_SPEC)
sys.modules["agent"] = agent
AGENT_SPEC.loader.exec_module(agent)

WORKER_PATH = Path(__file__).resolve().parents[1] / "worker.py"
WORKER_SPEC = importlib.util.spec_from_file_location("browser_worker", WORKER_PATH)
assert WORKER_SPEC and WORKER_SPEC.loader
worker = importlib.util.module_from_spec(WORKER_SPEC)
sys.modules[WORKER_SPEC.name] = worker
WORKER_SPEC.loader.exec_module(worker)


class FakeLocator:
    def __init__(self, count: int = 0, text: str = ""):
        self._count = count
        self._text = text

    def count(self):
        return self._count

    def inner_text(self, timeout=0):
        return self._text


class FakePage:
    def __init__(self, text: str, password_inputs: int = 0):
        self.text = text
        self.password_inputs = password_inputs

    def locator(self, selector):
        if selector == "input[type='password']":
            return FakeLocator(count=self.password_inputs)
        return FakeLocator(text=self.text)

    def title(self):
        return "Example page"


class AccessBlockTests(unittest.TestCase):
    def test_login_is_escalated_without_attempting_authentication(self):
        self.assertEqual(worker._access_block(FakePage("Sign in", password_inputs=1)), "login_required")

    def test_captcha_or_waf_is_escalated(self):
        self.assertEqual(worker._access_block(FakePage("Please verify you are human")), "blocked_waf_or_captcha")

    def test_normal_page_is_not_marked_as_blocked(self):
        self.assertIsNone(worker._access_block(FakePage("Annual reports and financial information")))


if __name__ == "__main__":
    unittest.main()
