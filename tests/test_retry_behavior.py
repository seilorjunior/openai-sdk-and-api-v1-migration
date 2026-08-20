import unittest
from pathlib import Path

import httpx
from openai import OpenAI


class RetryBehaviorTests(unittest.TestCase):
    def test_apim_policies_leave_429_retries_to_the_sdk(self) -> None:
        samples = Path(__file__).resolve().parents[1] / "samples"

        for policy_name in ("apim-policy.xml", "apim-unified-chat-policy.xml"):
            with self.subTest(policy=policy_name):
                policy = (samples / policy_name).read_text(encoding="utf-8")
                retry_condition = policy.split('<retry condition="', 1)[1].split('"', 1)[0]

                self.assertNotIn("429", retry_condition)
                self.assertIn("StatusCode &gt;= 500", retry_condition)

    def test_sdk_retries_429_and_succeeds(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return httpx.Response(
                    429,
                    headers={"retry-after-ms": "1"},
                    json={"error": {"message": "rate limited", "type": "rate_limit_error"}},
                )
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "test-model",
                    "choices": [{
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ready"},
                    }],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        http_client = httpx.Client(transport=httpx.MockTransport(handler))
        client = OpenAI(
            api_key="test",
            base_url="https://example.test/openai/v1/",
            max_retries=2,
            http_client=http_client,
        )

        response = client.chat.completions.create(
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
        )

        self.assertEqual(response.choices[0].message.content, "ready")
        self.assertEqual(attempts, 3)


if __name__ == "__main__":
    unittest.main()