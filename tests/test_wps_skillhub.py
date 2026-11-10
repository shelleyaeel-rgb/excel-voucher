import json
import threading
import urllib.error
import uuid

import pytest

from xiaoyu import wps_skillhub as hub
from xiaoyu.wps_service import WpsError


TOKEN = 'synthetic-token-only'


class Response:
    def __init__(self, value, *, status=200, headers=None):
        self.raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        return self.raw[:limit]


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def account_store(tmp_path, monkeypatch):
    monkeypatch.setattr(hub, '_encrypt_key', lambda token: 'protected:' + token[::-1])
    monkeypatch.setattr(hub, '_decrypt_key', lambda token: token.removeprefix('protected:')[::-1])
    return tmp_path / 'account.json'


def test_account_round_trip_and_wrong_scope(account_store):
    account = hub.WpsAccount(str(uuid.uuid4()), TOKEN)
    hub.save_account(account, account_store)
    assert TOKEN not in account_store.read_text(encoding='utf8')
    assert TOKEN not in repr(account)
    assert hub.require_account(account.account_id, account_store) == account
    with pytest.raises(WpsError, match='账号连接已变化'):
        hub.require_account(str(uuid.uuid4()), account_store)
    hub.save_account(None, account_store)
    assert hub.load_account(account_store) is None


@pytest.mark.parametrize('field,value', [('expires_at', float('nan')), ('expires_at', float('inf')),
    ('expires_at', True), ('display_name', ''), ('token', 'line\nbreak'), ('account_id', 'bad')])
def test_invalid_account_cannot_overwrite_saved_authorization(account_store, field, value):
    account = hub.WpsAccount(str(uuid.uuid4()), TOKEN)
    hub.save_account(account, account_store)
    before = account_store.read_bytes()
    setattr(account, field, value)
    with pytest.raises(WpsError):
        hub.save_account(account, account_store)
    assert account_store.read_bytes() == before


def test_expired_or_corrupt_account_fails_without_exposing_token(account_store):
    hub.save_account(hub.WpsAccount(str(uuid.uuid4()), TOKEN, expires_at=1), account_store)
    assert hub.WpsAccountService(account_store).status()['status'] == 'expired'
    with pytest.raises(WpsError) as caught:
        hub.require_account(path=account_store)
    assert caught.value.code == 'AUTH_REQUIRED'
    account_store.write_text('{"token":"' + TOKEN + '"}')
    with pytest.raises(WpsError) as caught:
        hub.load_account(account_store)
    assert TOKEN not in str(caught.value)


def test_expired_request_does_not_invalidate_new_login(account_store):
    account = hub.WpsAccount(str(uuid.uuid4()), TOKEN)
    hub.save_account(account, account_store)
    hub.mark_account_expired(str(uuid.uuid4()), account_store)
    assert hub.WpsAccountService(account_store).status()['connected']
    hub.mark_account_expired(account.account_id, account_store)
    assert hub.WpsAccountService(account_store).status()['status'] == 'expired'


def test_browser_authorization_exchange_stores_only_encrypted_token(account_store):
    opener = Opener(Response({'code': 202}), Response({'code': 200, 'data': {'token': TOKEN, 'expires_in': 3600}}))
    initialized = []
    class Client:
        def __init__(self, token):
            assert token == TOKEN
        def initialize(self):
            initialized.append(True)
    class Cancel:
        def is_set(self):
            return False
        def wait(self, duration):
            pass
    urls, statuses = [], []
    service = hub.WpsAccountService(account_store, opener=opener, client_factory=Client)
    service.connect(Cancel(), statuses.append, urls.append)
    assert initialized == [True] and service.status()['connected']
    code = urls[0].split('auth_code=')[1]
    uuid.UUID(code)
    assert all(json.loads(req.data) == {'code': code} for req in opener.requests)
    assert all(req.full_url == hub.EXCHANGE_URL for req in opener.requests)
    assert TOKEN not in str(urls + statuses) and TOKEN not in account_store.read_text(encoding='utf8')
    assert hub.load_account(account_store).expires_at > hub.time.time()


@pytest.mark.parametrize('code,expected', [(403, 'ACCESS_DENIED'), (409, 'CANCELLED'), (500, 'AUTH_FAILED')])
def test_authorization_denied_preserves_existing_account(account_store, code, expected):
    account = hub.WpsAccount(str(uuid.uuid4()), TOKEN)
    hub.save_account(account, account_store)
    opener = Opener(Response({'code': code, 'message': TOKEN}))
    service = hub.WpsAccountService(account_store, opener=opener)
    with pytest.raises(WpsError) as caught:
        service.connect(threading.Event(), lambda _: None, lambda _: True)
    assert caught.value.code == expected and TOKEN not in str(caught.value)
    assert hub.load_account(account_store) == account
    assert len(opener.requests) == 1


def test_cancel_during_exchange_never_saves_token(account_store):
    cancel = threading.Event()
    class CancellingOpener:
        def open(self, *args, **kwargs):
            cancel.set()
            return Response({'code': 200, 'data': {'token': TOKEN}})
    service = hub.WpsAccountService(account_store, opener=CancellingOpener())
    with pytest.raises(WpsError) as caught:
        service.connect(cancel, lambda _: None, lambda _: True)
    assert caught.value.code == 'CANCELLED' and not account_store.exists()


def test_browser_open_failure_does_not_start_polling(account_store):
    opener = Opener()
    with pytest.raises(WpsError) as caught:
        hub.WpsAccountService(account_store, opener=opener).connect(threading.Event(), lambda _: None, lambda _: False)
    assert caught.value.code == 'BROWSER_FAILED' and not opener.requests


def session_responses(tool_result):
    return [Response({'jsonrpc': '2.0', 'id': 1, 'result': {'protocolVersion': '2025-03-26'}},
                     headers={'Mcp-Session-Id': 'synthetic-session'}),
            Response(b'', status=202), Response({'jsonrpc': '2.0', 'id': 3, 'result': tool_result})]


def test_rpc_handshake_and_json_text_result():
    opener = Opener(*session_responses({'content': [{'type': 'text', 'text': '{"code":0,"data":{"ok":true}}'}]}))
    result = hub.SkillHubClient(TOKEN, opener).call_tool('sheet.get_sheets_info', {'url': 'https://www.kdocs.cn/l/test'})
    assert result['data']['ok'] is True
    requests = [json.loads(req.data) for req in opener.requests]
    assert [r['method'] for r in requests] == ['initialize', 'notifications/initialized', 'tools/call']
    assert 'id' not in requests[1]
    assert opener.requests[2].get_header('Mcp-session-id') == 'synthetic-session'


def test_sse_uses_matching_rpc_id():
    opener = Opener(Response(b'event: message\ndata: {"jsonrpc":"2.0","id":9,"result":{}}\n\nevent: message\ndata: {"jsonrpc":"2.0","id":3,"result":{"ok":true}}\n\n'))
    value, _ = hub._post(opener, hub.MCP_URL, {'id': 3})
    assert value['result'] == {'ok': True}


@pytest.mark.parametrize('code,expected', [(400006, 'AUTH_REQUIRED'), (403001, 'ACCESS_DENIED'), (429001, 'RATE_LIMITED'), (999, 'TOOL_ERROR')])
def test_tool_errors_are_sanitized_and_never_retried(code, expected):
    opener = Opener(*session_responses({'structuredContent': {'code': code, 'message': TOKEN}}))
    with pytest.raises(WpsError) as caught:
        hub.SkillHubClient(TOKEN, opener).call_tool('sheet.range_data_batch_update', {}, mutation=True)
    assert caught.value.code == expected and caught.value.mutation_attempted
    assert TOKEN not in str(caught.value) and len(opener.requests) == 3


@pytest.mark.parametrize('failure', [TimeoutError(TOKEN), urllib.error.HTTPError(hub.MCP_URL, 401, TOKEN, {}, None)])
def test_transport_failure_never_repeats_a_write(failure):
    opener = Opener(failure)
    with pytest.raises(WpsError) as caught:
        hub._post(opener, hub.MCP_URL, {}, mutation=True)
    assert caught.value.mutation_attempted and TOKEN not in str(caught.value)
    assert len(opener.requests) == 1


def test_invalid_response_is_not_treated_as_success():
    opener = Opener(Response(b'not JSON ' + TOKEN.encode()))
    with pytest.raises(WpsError) as caught:
        hub._post(opener, hub.MCP_URL, {}, mutation=True)
    assert caught.value.code == 'INVALID_RESPONSE' and caught.value.mutation_attempted
    assert TOKEN not in str(caught.value)
