"""Whether the phone app would let a pbw's scripts reach a URL (libpebble3's PluginNetworkPolicy
and NetworkGate). The phone refuses a request its declared permissions don't cover; the emulator
makes it anyway and warns."""

from urllib.parse import urlsplit

INTERNET = 'Internet'
LOCAL_NETWORK = 'LocalNetwork'

_REQUESTABLE = ('http://', 'https://', 'ws://', 'wss://')


def refusal(url, appinfo):
    """Why the phone would refuse this request from the pbw with this appinfo; None if it wouldn't."""
    key = permission_for(url)
    if key is None:
        return "the phone refuses to request %s" % url
    declared = declared_keys(appinfo)
    if declared is None or any(k in declared for k in covering_keys(key)):
        return None
    if key == LOCAL_NETWORK:
        return ("%s is on the local network: the phone refuses requests to it without the LocalNetwork "
                "permission in usesPermissions" % urlsplit(url).hostname)
    return ("the phone refuses requests to %s: add it to the Internet permission's domains in "
            "usesPermissions" % key.split('/', 1)[1])


def permission_for(url):
    """'LocalNetwork' or 'Internet/<host>'; None for a URL the phone won't request at all."""
    if not str(url).lower().startswith(_REQUESTABLE):
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    host = (host or '').rstrip('.')
    if not host:
        return None
    reach = _reach(host)
    if reach is None:
        return None
    return LOCAL_NETWORK if reach == LOCAL_NETWORK else '%s/%s' % (INTERNET, host)


def declared_keys(appinfo):
    """The network permissions a pbw declares; None for a legacy pbw, which may reach anything."""
    if not isinstance(appinfo.get('manifestVersion'), int) or appinfo['manifestVersion'] < 2:
        return None
    keys = set()
    for permission in appinfo.get('usesPermissions') or []:
        if not isinstance(permission, dict):
            keys.add(str(permission))
            continue
        if permission.get('plugin'):
            continue
        name = permission.get('name')
        domains = (permission.get('parameters') or {}).get('domains') if name == INTERNET else None
        if not domains:
            keys.add(name)
            continue
        for domain in domains:
            domain = str(domain).lower()
            if domain.startswith('.'):
                domain = domain[1:]
            keys.add('%s/%s' % (INTERNET, domain.rstrip('.')))
    return keys


def covering_keys(key):
    """Most specific first: Internet/a.b.c is covered by Internet/b.c, Internet/c and Internet."""
    if not key.startswith(INTERNET + '/'):
        return [key]
    labels = key.split('/', 1)[1].split('.')
    return ['%s/%s' % (INTERNET, '.'.join(labels[i:])) for i in range(len(labels))] + [INTERNET]


def _reach(host):
    if ':' in host:
        return _reach_ipv6(host)
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.home.arpa', '.internal')):
        return LOCAL_NETWORK
    labels = host.split('.')
    # A DNS name's last label is never all digits.
    looks_numeric = labels[-1].isdigit() or host.startswith('0x')
    if not looks_numeric:
        return INTERNET
    quad = _quad(labels)
    return _reach_ipv4(quad) if quad is not None else None


def _quad(labels):
    """A plain dotted quad only: a resolver reads `127.1`, `0x7f.1` or a leading zero differently."""
    if len(labels) != 4:
        return None
    quad = []
    for label in labels:
        if not label.isdigit() or (len(label) > 1 and label[0] == '0') or int(label) > 255:
            return None
        quad.append(int(label))
    return quad


def _reach_ipv4(b):
    if b[0] in (10, 127, 0) or (b[0] == 192 and b[1] == 168) or (b[0] == 172 and 16 <= b[1] <= 31):
        return LOCAL_NETWORK
    if (b[0] == 169 and b[1] == 254) or (b[0] == 100 and 64 <= b[1] <= 127):
        return LOCAL_NETWORK
    return INTERNET


def _reach_ipv6(host):
    mapped = host.rsplit('::ffff:', 1)[1] if '::ffff:' in host else ''
    if mapped:
        if '.' in mapped:
            quad = _quad(mapped.split('.'))
            return _reach_ipv4(quad) if quad is not None else None
        words = mapped.split(':')
        if len(words) != 2:
            return None
        try:
            high, low = int(words[0], 16), int(words[1], 16)
        except ValueError:
            return None
        if not (0 <= high <= 0xFFFF and 0 <= low <= 0xFFFF):
            return None
        return _reach_ipv4([high >> 8, high & 0xFF, low >> 8, low & 0xFF])
    if host in ('::1', '::'):
        return LOCAL_NETWORK
    prefix = host.split(':', 1)[0]
    if len(prefix) == 4 and prefix.startswith(('fc', 'fd')):
        return LOCAL_NETWORK
    if prefix.startswith(('fe8', 'fe9', 'fea', 'feb')):
        return LOCAL_NETWORK
    return INTERNET
