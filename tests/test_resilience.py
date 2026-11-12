"""Regression tests for transport failures, local config and loop recovery."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from agent import run_agent
from env_config import load_env
from providers import Provider, post_json
from status_tools import StatusTools
from test_lab import FakeProvider, final, tool_call


class RecoveryTests(unittest.TestCase):
    def test_provider_failure_redacts_key(self):
        class BrokenProvider:
            key = 'private-test-key'

            def complete(self, messages, schemas):
                raise RuntimeError('Authorization failed: ' + self.key)

        result = run_agent('status', BrokenProvider(), StatusTools())
        self.assertEqual(result['status'], 'error')
        self.assertNotIn(BrokenProvider.key, json.dumps(result))
        self.assertEqual(result['trace'][-1]['event'], 'error')

    def test_non_object_arguments_can_be_corrected(self):
        fake = FakeProvider([
            tool_call('bad', 'get_status', '[]'),
            tool_call('good', 'get_status', '{"provider":"github"}'), final(),
        ])
        result = run_agent('status', fake, StatusTools())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(json.loads(fake.histories[1][-1]['content'])['error'], 'INVALID_ARGUMENTS')
        self.assertEqual(json.loads(fake.histories[2][-1]['content'])['provider'], 'github')

    def test_empty_or_missing_arguments_return_invalid_arguments(self):
        for arguments in ('', None):
            with self.subTest(arguments=arguments):
                fake = FakeProvider([tool_call('a', 'get_status', arguments), final()])
                result = run_agent('status', fake, StatusTools())
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(json.loads(fake.histories[1][-1]['content'])['error'], 'INVALID_ARGUMENTS')

    def test_unhandled_tool_timeout_returns_to_model(self):
        class BrokenTools:
            def execute(self, name, args):
                raise TimeoutError('tool timed out')

        fake = FakeProvider([tool_call('a', 'get_status', '{}'), final('Không đủ dữ liệu')])
        result = run_agent('status', fake, BrokenTools())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(json.loads(fake.histories[1][-1]['content'])['error'], 'TimeoutError')

    def test_unexpected_finish_cannot_execute_tools(self):
        response = tool_call('a', 'get_status', '{"provider":"github"}')
        response['finish'] = 'length'
        result = run_agent('status', FakeProvider([response]), StatusTools())
        self.assertEqual(result['status'], 'error')
        self.assertFalse(any(t['event'] == 'tool' for t in result['trace']))

    def test_usage_includes_tool_and_final_turns(self):
        first = tool_call('a', 'get_status', '{"provider":"github"}')
        last = final()
        first['usage'], last['usage'] = {'total_tokens': 12}, {'total_tokens': 8}
        result = run_agent('status', FakeProvider([first, last]), StatusTools())
        self.assertEqual(sum(u['total_tokens'] for u in result['usage']), 20)

    def test_empty_final_is_not_completed(self):
        for content in (None, '', ' \n '):
            with self.subTest(content=content):
                response = final(content)
                response['usage'] = {'total_tokens': 15}
                result = run_agent('status', FakeProvider([response]), StatusTools())
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['trace'][-1]['event'], 'error')
                self.assertEqual(result['usage'], [{'total_tokens': 15}])


@patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test'}, clear=True)
class ProviderResponseTests(unittest.TestCase):
    @patch.dict(os.environ, {'OPENROUTER_PROVIDER_SORT': 'price'})
    @patch('providers.post_json')
    def test_price_routing_without_host_pin_allows_fallbacks(self, post):
        post.return_value = {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}]}
        Provider('openrouter', 'z-ai/glm-5.3-flash').complete([], [])
        routing = post.call_args.args[2]['provider']
        self.assertEqual(routing['sort'], 'price')
        self.assertNotIn('only', routing)
        self.assertTrue(routing['require_parameters'])

    @patch('providers.post_json')
    def test_error_envelope_does_not_raise_choices_key_error(self, post):
        post.return_value = {'error': {'code': 503, 'message': 'upstream unavailable'}}
        with self.assertRaisesRegex(RuntimeError, 'code=503'):
            Provider('openrouter', 'model').complete([], [])

    @patch('providers.post_json')
    def test_null_content_with_tool_calls_preserves_original_message(self, post):
        response = tool_call('a', 'get_status', '{"provider":"github"}')
        message = response['message']
        message['reasoning_details'] = [{'type': 'reasoning.encrypted', 'data': 'opaque'}]
        post.return_value = {'choices': [{'message': message, 'finish_reason': 'tool_calls'}]}
        result = Provider('openrouter', 'nvidia/nemotron-3-ultra-550b-a55b:free').complete([], [])
        self.assertIs(result['message'], message)
        self.assertEqual(result['calls'], response['calls'])

    @patch('providers.post_json')
    def test_other_models_keep_existing_request_options(self, post):
        post.return_value = {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}]}
        Provider('openrouter', 'other-model').complete([], [])
        payload = post.call_args.args[2]
        self.assertNotIn('reasoning', payload)
        self.assertNotIn('max_tokens', payload)
        self.assertNotIn('provider', payload)

    @patch.dict(os.environ, {'OPENROUTER_PROVIDER_ONLY': ' relace/fp4, morph/fp8, '})
    @patch('providers.post_json')
    def test_allowed_openrouter_hosts_use_price_and_require_parameters(self, post):
        post.return_value = {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}]}
        from status_tools import SCHEMAS
        Provider('openrouter', 'z-ai/glm-5.3-flash').complete([], SCHEMAS)
        payload = post.call_args.args[2]
        self.assertEqual(payload['provider'], {'only': ['relace/fp4', 'morph/fp8'],
                                               'sort': 'price', 'require_parameters': True})
        self.assertEqual(payload['tools'], SCHEMAS)

    @patch.dict(os.environ, {'OPENROUTER_PROVIDER_ONLY': 'relace/fp4', 'GEMINI_API_KEY': 'test'})
    @patch('providers.post_json')
    def test_openrouter_host_config_does_not_affect_gemini(self, post):
        post.return_value = {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}]}
        Provider('gemini', 'model').complete([], [])
        self.assertNotIn('provider', post.call_args.args[2])

    @patch('providers.post_json')
    def test_null_tool_calls_with_valid_content_is_accepted(self, post):
        post.return_value = {'choices': [{'message': {'role': 'assistant', 'content': 'ok', 'tool_calls': None}, 'finish_reason': 'stop'}]}
        result = Provider('openrouter', 'model').complete([], [])
        self.assertEqual(result['calls'], [])
        self.assertEqual(result['message']['content'], 'ok')

    @patch('providers.post_json')
    def test_empty_stop_is_rejected_without_exposing_reasoning(self, post):
        for content in (None, '', ' \n '):
            with self.subTest(content=content):
                post.return_value = {'choices': [{'message': {'role': 'assistant', 'content': content,
                    'reasoning': 'private reasoning text'}, 'finish_reason': 'stop'}]}
                with self.assertRaisesRegex(RuntimeError, 'content rỗng') as caught:
                    Provider('openrouter', 'model').complete([], [])
                self.assertNotIn('private reasoning text', str(caught.exception))

    @patch('providers.post_json')
    def test_reasoning_only_judge_remains_unscored(self, post):
        from evals.grading import grade_case
        post.return_value = {'choices': [{'message': {'role': 'assistant', 'content': None,
            'reasoning': '{"factuality":40,"completeness":25,"evidence":20,"boundaries":15,"reason":"ok","improvement":"none"}'},
            'finish_reason': 'stop'}]}
        grade = grade_case({'id': 'T', 'question': 'q', 'expected': 'e'},
                           {'status': 'completed', 'answer': 'answer', 'trace': []},
                           Provider('openrouter', 'model'))
        self.assertEqual(grade['status'], 'unscored')
        self.assertIsNone(grade['score'])
        self.assertIn('content rỗng', grade['reason'])
        self.assertNotIn('strip', grade['reason'])


class ConfigTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_local_env_preserves_shell_values_and_does_not_execute(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / '.env'
            path.write_text('export MODEL="file-model"\nAPI_KEY="literal$(command)"\n', encoding='utf-8')
            os.environ['MODEL'] = 'shell-model'
            load_env(path)
            self.assertEqual(os.environ['MODEL'], 'shell-model')
            self.assertEqual(os.environ['API_KEY'], 'literal$(command)')

    @patch.dict(os.environ, {}, clear=True)
    def test_bad_local_config_does_not_expose_values(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / '.env'
            path.write_text('API_KEY="private-test-key', encoding='utf-8')
            with self.assertRaises(ValueError) as caught:
                load_env(path)
            self.assertNotIn('private-test-key', str(caught.exception))

    @patch.dict(os.environ, {'API_KEY': 'test', 'API_ENDPOINT': 'https://example.org/v1/'}, clear=True)
    def test_custom_gateway_uses_generic_key(self):
        provider = Provider('openai', 'model')
        self.assertEqual(provider.key, 'test')
        self.assertEqual(provider.url, 'https://example.org/v1/chat/completions')

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'gemini-test', 'API_ENDPOINT': 'https://example.org/v1'}, clear=True)
    def test_old_gateway_endpoint_does_not_override_gemini(self):
        self.assertEqual(Provider('gemini', 'model').url,
                         'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions')


class TransportTests(unittest.TestCase):
    @patch('providers._pace_requests')
    @patch('providers.time.sleep')
    @patch('providers.urllib.request.urlopen')
    def test_429_respects_retry_after_then_succeeds(self, urlopen, sleep, pace):
        error = urllib.error.HTTPError('https://example.org', 429, 'rate limited', {'Retry-After': '2'}, io.BytesIO(b'{}'))
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{"ok":true}')
        urlopen.side_effect = [error, response]
        self.assertEqual(post_json('https://example.org', {}, {}), {'ok': True})
        sleep.assert_called_once_with(2.0)
        self.assertEqual(urlopen.call_count, 2)

    @patch('providers._pace_requests')
    @patch('providers.time.sleep')
    @patch('providers.urllib.request.urlopen')
    def test_persistent_429_has_finite_retries(self, urlopen, sleep, pace):
        urlopen.side_effect = [urllib.error.HTTPError('https://example.org', 429, 'limited', {'Retry-After': '1'}, io.BytesIO(b'{}')) for _ in range(3)]
        with self.assertRaisesRegex(RuntimeError, '429'):
            post_json('https://example.org', {}, {})
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @patch('providers._pace_requests')
    @patch('providers.time.sleep')
    @patch('providers.urllib.request.urlopen')
    def test_long_cooldown_and_auth_errors_fail_without_retry(self, urlopen, sleep, pace):
        for code, headers in [(429, {'Retry-After': '120'}), (403, {})]:
            with self.subTest(code=code):
                urlopen.reset_mock()
                urlopen.side_effect = urllib.error.HTTPError('https://example.org', code, 'error', headers, io.BytesIO(b'{}'))
                with self.assertRaisesRegex(RuntimeError, str(code)):
                    post_json('https://example.org', {}, {})
                self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()
