"""Tests for scripts.lib.minimax_common."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.lib.minimax_common import (
    MinimaxError,
    _extract_json,
    load_openai_api_key,
    openai_search_chat_completion,
)


class ExtractJsonTests(unittest.TestCase):
    def test_plain_json(self):
        data = _extract_json('{"x": 1}')
        self.assertEqual(data, {"x": 1})

    def test_fenced_json(self):
        data = _extract_json('```\n{"x": 1}\n```')
        self.assertEqual(data, {"x": 1})

    def test_fenced_json_with_lang(self):
        data = _extract_json('```json\n{"x": 1}\n```')
        self.assertEqual(data, {"x": 1})

    def test_embedded_in_prose(self):
        data = _extract_json('Here is the answer: {"x": 1} done.')
        self.assertEqual(data, {"x": 1})

    def test_no_json_raises(self):
        with self.assertRaises(ValueError):
            _extract_json("No json here.")


class LoadOpenaiApiKeyTests(unittest.TestCase):
    def test_env_var(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            self.assertEqual(load_openai_api_key(), "sk-test")

    def test_missing_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "home", return_value=Path("/nonexistent")):
                with self.assertRaises(MinimaxError) as ctx:
                    load_openai_api_key()
                self.assertIn("OPENAI_API_KEY not set", str(ctx.exception))


class OpenaiSearchChatCompletionTests(unittest.TestCase):
    @patch("scripts.lib.minimax_common.openai.OpenAI")
    @patch("scripts.lib.minimax_common.load_openai_api_key")
    def test_temperature_retry_on_unsupported_value(self, mock_load_key, mock_openai_cls):
        mock_load_key.return_value = "sk-test"
        client = MagicMock()
        mock_openai_cls.return_value = client

        # First call raises unsupported_value; second succeeds
        response1 = MagicMock()
        response1.choices = [MagicMock(message=MagicMock(content='{"x":1}'), finish_reason="stop")]
        response1.usage = None

        side_effects = [
            Exception("unsupported_value: temperature"),
            response1,
        ]
        client.chat.completions.create.side_effect = side_effects

        result = openai_search_chat_completion(
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            temperature=0.3,
        )
        self.assertEqual(result["choices"][0]["message"]["content"], '{"x":1}')
        # Second call should not include temperature
        second_call = client.chat.completions.create.call_args_list[1]
        self.assertNotIn("temperature", second_call.kwargs)

    @patch("scripts.lib.minimax_common.openai.OpenAI")
    @patch("scripts.lib.minimax_common.load_openai_api_key")
    def test_success_without_retry(self, mock_load_key, mock_openai_cls):
        mock_load_key.return_value = "sk-test"
        client = MagicMock()
        mock_openai_cls.return_value = client

        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content='{"y":2}'), finish_reason="stop")]
        response.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        client.chat.completions.create.return_value = response

        result = openai_search_chat_completion(
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            temperature=0.3,
        )
        self.assertEqual(result["choices"][0]["message"]["content"], '{"y":2}')
        self.assertEqual(result["usage"]["total_tokens"], 15)


if __name__ == "__main__":
    unittest.main()
