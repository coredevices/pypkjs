"""The phone app's rules for which requests a pbw's scripts may make (pypkjs.plugins.network)."""
import importlib.util
import os

import pytest

# Loaded by path: importing the pypkjs package monkey-patches the interpreter for gevent.
_spec = importlib.util.spec_from_file_location(
    'network_policy', os.path.join(os.path.dirname(__file__), '..', 'pypkjs', 'plugins', 'network.py'))
network = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(network)


@pytest.mark.parametrize('url, key', [
    ('https://API.Example.com./v1', 'Internet/api.example.com'),
    ('wss://example.com/socket', 'Internet/example.com'),
    ('http://8.8.8.8/', 'Internet/8.8.8.8'),
    ('http://localhost:8080/', 'LocalNetwork'),
    ('http://hue.local/', 'LocalNetwork'),
    ('http://192.168.1.20/', 'LocalNetwork'),
    ('http://172.20.0.1/', 'LocalNetwork'),
    ('http://100.64.0.1/', 'LocalNetwork'),
    ('http://[::1]/', 'LocalNetwork'),
    ('http://[fd00::1]/', 'LocalNetwork'),
    ('http://[::ffff:192.168.0.1]/', 'LocalNetwork'),
    ('http://[2001:db8::1]/', 'Internet/2001:db8::1'),
    ('http://127.1/', None),
    ('http://0x7f000001/', None),
    ('http://010.0.0.1/', None),
    ('file:///etc/passwd', None),
    ('data:text/plain,hi', None),
])
def test_permission_for(url, key):
    assert network.permission_for(url) == key


def appinfo(*permissions, manifest_version=2):
    return {'manifestVersion': manifest_version, 'usesPermissions': list(permissions)}


def internet(*domains):
    return {'name': 'Internet', 'parameters': {'domains': list(domains)}}


@pytest.mark.parametrize('url, info', [
    ('https://query1.finance.yahoo.com/q', appinfo(internet('query1.finance.yahoo.com'))),
    ('https://api.example.com/', appinfo(internet('.example.com.'))),
    ('https://anything.at.all/', appinfo('Internet')),
    ('http://192.168.1.2/', appinfo('LocalNetwork')),
    ('https://anything.at.all/', appinfo(manifest_version=1)),
    ('https://anything.at.all/', {}),
])
def test_allowed(url, info):
    assert network.refusal(url, info) is None


def test_undeclared_domain_is_refused():
    reason = network.refusal('https://query2.finance.yahoo.com/q', appinfo(internet('query1.finance.yahoo.com')))
    assert reason == ("the phone refuses requests to query2.finance.yahoo.com: add it to the Internet "
                      "permission's domains in usesPermissions")


def test_a_parent_domain_does_not_cover_its_parent():
    assert network.refusal('https://example.com/', appinfo(internet('api.example.com'))) is not None


def test_plugin_defined_permissions_grant_no_network():
    info = appinfo({'name': 'Internet', 'plugin': '11111111-2222-3333-4444-555555555555'})
    assert network.refusal('https://example.com/', info) is not None


def test_local_network_needs_its_own_permission():
    assert 'LocalNetwork' in network.refusal('http://10.0.0.5/', appinfo('Internet'))


def test_unrequestable_url_is_refused_even_for_legacy_apps():
    assert network.refusal('http://127.1/', {}) == 'the phone refuses to request http://127.1/'
