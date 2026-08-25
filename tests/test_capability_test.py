import json
import os
import tempfile
import unittest
from argparse import Namespace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import capability_test


class CapabilityTestTests(unittest.TestCase):
    def test_missing_optional_deployment_is_skipped(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(capability_test.CapabilitySkipped, "AZURE_OPENAI_EMBEDDING_DEPLOYMENT"):
                capability_test.embeddings(Mock(), "test")

    def test_mutating_batch_requires_explicit_confirmation(self) -> None:
        client = Mock()

        with self.assertRaisesRegex(capability_test.CapabilitySkipped, "--execute-mutating"):
            capability_test.batch(client, "", False)

        client.files.create.assert_not_called()
        client.batches.create.assert_not_called()

    def test_run_capability_dispatches_mutating_operation_with_confirmation(self) -> None:
        client = Mock()
        with patch("capability_test.batch", return_value={"batch_id": "batch-1"}) as batch:
            result = capability_test.run_capability(client, "batch", "prompt", True)

        batch.assert_called_once_with(client, "prompt", True)
        self.assertEqual(result["batch_id"], "batch-1")
        self.assertIn("latency_ms", result)

    @patch.dict(os.environ, {"AZURE_OPENAI_DEPLOYMENT": "chat-deployment"}, clear=True)
    def test_structured_output_validates_expected_shape(self) -> None:
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ready": true, "summary": "ready"}'))]
        )

        result = capability_test.structured(client, "test")

        self.assertTrue(result["schema_valid"])
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["response_format"]["type"], "json_schema")
        self.assertTrue(request["response_format"]["json_schema"]["strict"])

    @patch.dict(os.environ, {"AZURE_OPENAI_DEPLOYMENT": "chat-deployment"}, clear=True)
    def test_tool_call_arguments_are_validated(self) -> None:
        tool_call = SimpleNamespace(function=SimpleNamespace(name="get_migration_status", arguments='{"application":"demo"}'))
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[tool_call]))]
        )

        result = capability_test.tools(client, "test")

        self.assertEqual(result["tool_name"], "get_migration_status")
        self.assertTrue(result["arguments_valid_json"])

    @patch.dict(os.environ, {"AZURE_OPENAI_DEPLOYMENT": "chat-deployment"}, clear=True)
    def test_streaming_counts_chunks_and_nonempty_content(self) -> None:
        client = Mock()
        client.chat.completions.create.return_value = [
            SimpleNamespace(choices=[]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="ready"))]),
        ]

        result = capability_test.streaming(client, "test")

        self.assertEqual(result, {"chunk_count": 3, "output_nonempty": True})
        self.assertTrue(client.chat.completions.create.call_args.kwargs["stream"])

    @patch.dict(os.environ, {"AZURE_OPENAI_DEPLOYMENT": "chat-deployment"}, clear=True)
    def test_responses_normalizes_model_text_and_status(self) -> None:
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(model="gpt-test", output_text="ready", status="completed")

        result = capability_test.responses(client, "test")

        self.assertEqual(result, {"model": "gpt-test", "output_nonempty": True, "status": "completed"})
        client.responses.create.assert_called_once_with(model="chat-deployment", input="test", max_output_tokens=80)

    @patch.dict(os.environ, {"AZURE_OPENAI_DEPLOYMENT": "chat-deployment"}, clear=True)
    def test_tools_handles_empty_tool_calls(self) -> None:
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=None))]
        )

        result = capability_test.tools(client, "test")

        self.assertEqual(result, {"tool_call_count": 0, "tool_name": None, "arguments_valid_json": False})

    @patch.dict(os.environ, {"AZURE_OPENAI_EMBEDDING_DEPLOYMENT": "embedding-deployment"}, clear=True)
    def test_embeddings_reports_vector_shape(self) -> None:
        client = Mock()
        client.embeddings.create.return_value = SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.1, 0.2]), SimpleNamespace(embedding=[0.3, 0.4])]
        )

        result = capability_test.embeddings(client, "test")

        self.assertEqual(result, {"dimensions": 2, "vector_count": 2})
        client.embeddings.create.assert_called_once_with(model="embedding-deployment", input="test")

    @patch.dict(os.environ, {"AZURE_OPENAI_IMAGE_DEPLOYMENT": "image-deployment"}, clear=True)
    def test_images_handles_empty_and_base64_results(self) -> None:
        client = Mock()
        client.images.generate.side_effect = [
            SimpleNamespace(data=[]),
            SimpleNamespace(data=[SimpleNamespace(url=None, b64_json="encoded")]),
        ]

        self.assertEqual(capability_test.images(client, "test"), {"image_returned": False})
        self.assertEqual(capability_test.images(client, "test"), {"image_returned": True})

    def test_audio_transcription_uploads_configured_file(self) -> None:
        client = Mock()
        client.audio.transcriptions.create.return_value = SimpleNamespace(text="transcript")
        with tempfile.TemporaryDirectory() as directory:
            audio_path = Path(directory) / "sample.wav"
            audio_path.write_bytes(b"audio")
            with patch.dict(
                os.environ,
                {"OPENAI_AUDIO_FILE": str(audio_path), "AZURE_OPENAI_AUDIO_DEPLOYMENT": "audio-deployment"},
                clear=True,
            ):
                result = capability_test.audio_transcription(client, "ignored")

        self.assertEqual(result, {"transcript_nonempty": True})
        self.assertEqual(client.audio.transcriptions.create.call_args.kwargs["model"], "audio-deployment")

    @patch.dict(
        os.environ,
        {"AZURE_OPENAI_DEPLOYMENT": "chat-deployment", "OPENAI_SAFETY_PROMPT": "safe prompt"},
        clear=True,
    )
    def test_safety_reports_filter_metadata_presence(self) -> None:
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="content_filter", content_filter_results={"hate": {}})]
        )

        result = capability_test.safety(client, "ignored")

        self.assertEqual(result, {"finish_reason": "content_filter", "content_filter_metadata_present": True})

    def test_fine_tuning_uploads_training_file_when_confirmed(self) -> None:
        client = Mock()
        client.files.create.return_value = SimpleNamespace(id="file-1")
        client.fine_tuning.jobs.create.return_value = SimpleNamespace(id="job-1", status="validating_files")
        with tempfile.TemporaryDirectory() as directory:
            training_path = Path(directory) / "training.jsonl"
            training_path.write_text("{}\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"OPENAI_FINE_TUNING_FILE": str(training_path), "AZURE_OPENAI_FINE_TUNING_MODEL": "gpt-test"},
                clear=True,
            ):
                result = capability_test.fine_tuning(client, "ignored", True)

        self.assertEqual(result, {"fine_tuning_job_id": "job-1", "status": "validating_files"})
        client.fine_tuning.jobs.create.assert_called_once_with(training_file="file-1", model="gpt-test")

    @patch("capability_test.parse_args")
    def test_legacy_mode_fails_before_building_client(self, parse_args) -> None:
        parse_args.return_value = Namespace(
            target="direct",
            api_mode="legacy",
            capability="chat",
            prompt="test",
            execute_mutating=False,
        )

        with patch("capability_test.build_client") as build_client, patch("sys.stdout", new_callable=StringIO) as output:
            exit_code = capability_test.main()

        self.assertEqual(exit_code, 1)
        build_client.assert_not_called()
        self.assertEqual(
            json.loads(output.getvalue()),
            {"status": "failed", "error": "Advanced capabilities require --api-mode v1."},
        )

    @patch("capability_test.parse_args")
    @patch("capability_test.build_client", return_value=Mock())
    def test_skipped_capability_does_not_fail_process(self, _, parse_args) -> None:
        parse_args.return_value = Namespace(
            target="direct",
            api_mode="v1",
            capability="embeddings",
            prompt="test",
            execute_mutating=False,
        )

        with patch.dict(os.environ, {}, clear=True), patch("sys.stdout", new_callable=StringIO) as output:
            exit_code = capability_test.main()

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", result["reason"])

    @patch("capability_test.parse_args")
    @patch("capability_test.build_client", return_value=Mock())
    def test_failure_output_does_not_expose_exception_message(self, _, parse_args) -> None:
        parse_args.return_value = Namespace(
            target="direct",
            api_mode="v1",
            capability="chat",
            prompt="test",
            execute_mutating=False,
        )

        with patch("capability_test.run_capability", side_effect=ValueError("secret endpoint detail")), patch(
            "sys.stdout", new_callable=StringIO
        ) as output:
            exit_code = capability_test.main()

        rendered = output.getvalue()
        result = json.loads(rendered)
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["error_type"], "ValueError")
        self.assertNotIn("secret endpoint detail", rendered)

    @patch("capability_test.parse_args")
    @patch("capability_test.build_client", return_value=Mock())
    def test_all_capabilities_continue_after_skips_and_failures(self, _, parse_args) -> None:
        parse_args.return_value = Namespace(
            target="direct",
            api_mode="v1",
            capability="all",
            prompt="test",
            execute_mutating=False,
        )

        def run_capability(_client, name, _prompt, _execute_mutating):
            if name == "embeddings":
                raise capability_test.CapabilitySkipped("not configured")
            if name == "images":
                raise ValueError("private detail")
            return {"latency_ms": 1}

        with patch("capability_test.run_capability", side_effect=run_capability), patch(
            "sys.stdout", new_callable=StringIO
        ) as output:
            exit_code = capability_test.main()

        results = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(exit_code, 1)
        self.assertEqual(len(results), len(capability_test.CAPABILITIES) + 2)
        self.assertEqual(next(item for item in results if item["capability"] == "embeddings")["status"], "skipped")
        self.assertEqual(next(item for item in results if item["capability"] == "images")["error_type"], "ValueError")


if __name__ == "__main__":
    unittest.main()