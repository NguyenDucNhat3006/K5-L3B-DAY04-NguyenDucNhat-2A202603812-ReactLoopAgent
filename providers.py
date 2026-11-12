"""Bốn provider cùng một interface. Không chứa agent loop."""
import json
import os
import threading
import time
import urllib.error
import urllib.request

CONFIG = {
    'openai': ('OPENAI_API_KEY', 'https://api.openai.com/v1/chat/completions'),
    'gemini': ('GEMINI_API_KEY', 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'),
    'openrouter': ('OPENROUTER_API_KEY', 'https://openrouter.ai/api/v1/chat/completions'),
    'anthropic': ('ANTHROPIC_API_KEY', 'https://api.anthropic.com/v1/messages'),
}

# Shared by agent and judge instances; optional pacing for small API quotas.
_request_lock = threading.Lock()
_last_request_at = 0.0


def _pace_requests():
    global _last_request_at
    interval = float(os.getenv('API_MIN_INTERVAL_SECONDS', '0'))
    if not 0 <= interval <= 60:
        raise ValueError('API_MIN_INTERVAL_SECONDS phải nằm trong 0..60.')
    with _request_lock:
        wait = interval - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _retry_delay(exc):
    """Read safe retry metadata; never print an HTTP response body."""
    delay = exc.headers.get('Retry-After') if exc.headers else None
    if delay is None:
        try:
            data = json.load(exc)
            if isinstance(data, list):
                data = data[0] if data else {}
            for detail in data.get('error', {}).get('details', []):
                if 'retryDelay' in detail:
                    delay = str(detail['retryDelay']).removesuffix('s')
                    break
        except (ValueError, AttributeError, TypeError, OSError):
            pass
    try:
        delay = float(delay)
    except (TypeError, ValueError):
        delay = 30.0
    # Do not retry earlier than a long provider cooldown; let the run fail safely.
    return delay if 0 <= delay <= 60 else None


def post_json(url, headers, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', **headers})
    for attempt in range(3):
        _pace_requests()
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            # Retry only rate limits, at most twice. Auth/model errors fail fast.
            delay = _retry_delay(exc) if exc.code == 429 and attempt < 2 else None
            if delay is not None:
                time.sleep(max(1.0, delay))
                continue
            raise RuntimeError(f'Provider trả HTTP {exc.code}; kiểm tra model, key và quota.') from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f'Không kết nối được provider: {exc.reason}') from exc


class Provider:
    """complete(messages, schemas) -> {message, calls, finish, usage}.

    messages: system/user/assistant/tool theo format Chat Completions. message trả về
    cần được append nguyên vẹn; tool result có role, tool_call_id, content (JSON string).
    """
    def __init__(self, name, model):
        if name not in CONFIG:
            raise ValueError(f'Provider không hợp lệ: {name}')
        if not model:
            raise ValueError('Cần đặt MODEL là model có hỗ trợ tool calling.')
        self.name, self.model = name, model
        key_name, self.url = CONFIG[name]
        self.key = os.getenv(key_name)
        # Custom OpenAI-compatible gateways use the same request/response format.
        if name == 'openai':
            self.key = self.key or os.getenv('API_KEY')
            endpoint = os.getenv('API_ENDPOINT', '').strip().rstrip('/')
            if endpoint:
                self.url = endpoint if endpoint.endswith('/chat/completions') else endpoint + '/chat/completions'
        if not self.key:
            raise ValueError(f'Thiếu {key_name}; xem README.md.')

    def complete(self, messages, schemas):
        if self.name == 'anthropic':
            return self._anthropic(messages, schemas)
        payload = {'model': self.model, 'messages': messages}
        if self.name == 'openrouter':
            allowed = [p.strip() for p in os.getenv('OPENROUTER_PROVIDER_ONLY', '').split(',') if p.strip()]
            sort = os.getenv('OPENROUTER_PROVIDER_SORT', '').strip()
            if sort and sort not in ('price', 'throughput', 'latency'):
                raise ValueError('OPENROUTER_PROVIDER_SORT cần là price, throughput hoặc latency.')
            if allowed or sort:
                payload['provider'] = {'sort': sort or 'price', 'require_parameters': True}
                if allowed:
                    payload['provider']['only'] = allowed
        if schemas:
            payload['tools'] = schemas
        data = post_json(self.url, {'Authorization': f'Bearer {self.key}'}, payload)
        if not isinstance(data, dict):
            raise RuntimeError('Provider trả JSON không đúng định dạng object.')
        if data.get('error'):
            error = data['error']
            code = error.get('code', 'unknown') if isinstance(error, dict) else 'unknown'
            raise RuntimeError(f'Provider trả lỗi trong JSON, code={code}; thử lại hoặc kiểm tra trạng thái provider.')
        choices = data.get('choices')
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise RuntimeError('Provider trả phản hồi thiếu choices hợp lệ.')
        choice = choices[0]
        message = choice['message']
        calls = [{'id': c['id'], 'name': c['function']['name'], 'arguments': c['function']['arguments']} for c in (message.get('tool_calls') or [])]
        if choice.get('finish_reason') == 'stop' and not calls:
            content = message.get('content')
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError('Provider trả finish=stop nhưng content rỗng; chưa có câu trả lời cuối. Hãy thử lại hoặc kiểm tra model.')
        return {'message': message, 'calls': calls, 'finish': choice.get('finish_reason'), 'usage': data.get('usage', {})}

    def _anthropic(self, messages, schemas):
        system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
        native = []
        for m in messages:
            if m['role'] == 'system':
                continue
            if m['role'] == 'assistant':
                blocks = []
                if m.get('content'):
                    blocks.append({'type': 'text', 'text': m['content']})
                for call in m.get('tool_calls', []):
                    blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'], 'input': json.loads(call['function']['arguments'])})
                native.append({'role': 'assistant', 'content': blocks})
            elif m['role'] == 'tool':
                block = {'type': 'tool_result', 'tool_use_id': m['tool_call_id'], 'content': m['content']}
                if native and native[-1]['role'] == 'user' and isinstance(native[-1]['content'], list):
                    native[-1]['content'].append(block)
                else:
                    native.append({'role': 'user', 'content': [block]})
            else:
                native.append({'role': 'user', 'content': m['content']})
        tools = [{'name': x['function']['name'], 'description': x['function']['description'], 'input_schema': x['function']['parameters']} for x in schemas]
        payload = {'model': self.model, 'max_tokens': 1024, 'system': system, 'messages': native}
        if tools:
            payload['tools'] = tools
        data = post_json(self.url, {'x-api-key': self.key, 'anthropic-version': '2023-06-01'}, payload)
        blocks = data['content']
        calls = [{'id': b['id'], 'name': b['name'], 'arguments': json.dumps(b['input'], ensure_ascii=False)} for b in blocks if b['type'] == 'tool_use']
        text = '\n'.join(b['text'] for b in blocks if b['type'] == 'text')
        message = {'role': 'assistant', 'content': text or None}
        if calls:
            message['tool_calls'] = [{'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': c['arguments']}} for c in calls]
        reason = {'end_turn': 'stop', 'tool_use': 'tool_calls', 'max_tokens': 'length'}.get(data.get('stop_reason'), data.get('stop_reason'))
        return {'message': message, 'calls': calls, 'finish': reason, 'usage': data.get('usage', {})}
