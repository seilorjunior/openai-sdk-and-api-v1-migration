import os
import unittest
from argparse import Namespace
from unittest.mock import Mock, patch

import deepseek_user_security_context_test


class DeepSeekUserSecurityContextTestTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    @patch("deepseek_user_security_context_test.user_security_context_test.main", return_value=2)
    @patch(
        "deepseek_user_security_context_test.parse_args",
        return_value=Namespace(
            prompt="test",
            print_full_exchange=False,
            acknowledge_sensitive_output=False,
            save_full_exchange=None,
        ),
    )
    def test_main_requires_deepseek_base_url(self, _: Mock, base_main: Mock) -> None:
        with self.assertRaisesRegex(ValueError, "AZURE_OPENAI_DEEPSEEK_BASE_URL is required"):
            deepseek_user_security_context_test.main()

        base_main.assert_not_called()

    @patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_DEEPSEEK_BASE_URL": "https://customer.example/openai/v1",
            "AZURE_OPENAI_DEEPSEEK_DEPLOYMENT": "customer-deepseek-deployment",
            "AZURE_OPENAI_DEEPSEEK_TENANT_ID": "33333333-3333-3333-3333-333333333333",
        },
        clear=True,
    )
    @patch("deepseek_user_security_context_test.user_security_context_test.main", return_value=0)
    def test_main_allows_deepseek_deployment_override(self, base_main: Mock) -> None:
        args = Namespace(
            prompt="test",
            print_full_exchange=False,
            acknowledge_sensitive_output=False,
            save_full_exchange=None,
        )

        with patch("deepseek_user_security_context_test.parse_args", return_value=args):
            exit_code = deepseek_user_security_context_test.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(os.environ["AZURE_OPENAI_BASE_URL"], "https://customer.example/openai/v1")
        self.assertEqual(os.environ["AZURE_OPENAI_TENANT_ID"], "33333333-3333-3333-3333-333333333333")
        self.assertEqual(os.environ["AZURE_OPENAI_DEPLOYMENT"], "customer-deepseek-deployment")
        base_main.assert_called_once_with(args)


if __name__ == "__main__":
    unittest.main()