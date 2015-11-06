import os
import urlparse
import urllib
import base64
import hashlib
import json
import requests
import jws
import ecdsa
from exceptions import *

def _parse_www_auth(s):
    props = [x.split('=') for x in s.split(' ')[1].split(',')]
    return dict([(y[0], y[1].strip('"')) for y in props])

def _num_to_base64(n):
    b = bytearray()
    while n:
        b.insert(0, n & 0xFF)
        n >>= 8
    if len(b) == 0:
        b.insert(0, 0)
    return base64.urlsafe_b64encode(b).rstrip('=')

def _base64_to_num(s):
    s = s.encode('utf-8')
    s = base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))
    b = bytearray(s)
    m = len(b) - 1
    return sum((1 << ((m - bi)*8)) * bb for (bi, bb) in enumerate(b))

def _jwk_to_key(jwk):
    if jwk['kty'] != 'EC':
        raise DXFUnexpectedKeyTypeError(jwk['kty'], 'EC')
    if jwk['crv'] != 'P-256':
        raise DXFUnexpectedKeyTypeError(jwk['crv'], 'P-256')
    return ecdsa.VerifyingKey.from_public_point(
            ecdsa.ellipticcurve.Point(ecdsa.NIST256p.curve,
                                      _base64_to_num(jwk['x']),
                                      _base64_to_num(jwk['y'])),
            ecdsa.NIST256p)

def _pad64(s):
    return s + '=' * (-len(s) % 4)

def sha256_file(fname):
    sha256 = hashlib.sha256()
    with open(fname, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            sha256.update(chunk)
    return sha256.hexdigest()

def _verify_manifest(content, content_digest=None):
    # Adapted from https://github.com/joyent/node-docker-registry-client
    manifest = json.loads(content)
    signatures = []
    for sig in manifest['signatures']:
        protected64 = sig['protected'].encode('utf-8')
        protected = base64.urlsafe_b64decode(_pad64(protected64))
        protected_header = json.loads(protected)

        format_length = protected_header['formatLength']
        format_tail64 = protected_header['formatTail'].encode('utf-8')
        format_tail = base64.urlsafe_b64decode(_pad64(format_tail64))

        alg = sig['header']['alg']
        if alg.lower() == 'none':
            raise DXFDisallowedSignatureAlgorithmError('none')
        if sig['header'].get('chain'):
            raise DXFChainNotImplementedError()

        signatures.append({
            'alg': alg,
            'signature': sig['signature'],
            'protected64': protected64,
            'key': _jwk_to_key(sig['header']['jwk']),
            'format_length': format_length,
            'format_tail': format_tail
        })

    payload = content[:signatures[0]['format_length']] + \
              signatures[0]['format_tail']
    payload64 = base64.urlsafe_b64encode(payload).rstrip('=')

    if content_digest:
        method, expected_dgst = content_digest.split(':')
        if method != 'sha256':
            raise DXFUnexpectedDigestMethodError(method, 'sha256')
        hasher = hashlib.new(method)
        hasher.update(payload)
        dgst = hasher.hexdigest()
        if dgst != expected_dgst:
            raise DXFDigestMismatchError(dgst, expected_dgst)

    for sig in signatures:
        data = {
            'key': sig['key'],
            'header': {
                'alg': sig['alg']
            }
        }
        jws.header.process(data, 'verify')
        sig64 = sig['signature'].encode('utf-8')
        data['verifier']("%s.%s" % (sig['protected64'], payload64),
                         base64.urlsafe_b64decode(_pad64(sig64)),
                         sig['key'])

    dgsts = []
    for layer in manifest['fsLayers']:
        method, dgst = layer['blobSum'].split(':')
        if method != 'sha256':
            raise DXFUnexpectedDigestMethodError(method, 'sha256')
        dgsts.append(dgst)
    return dgsts

class DXF(object):
    def __init__(self, host, repo):
        self.host = host
        self._repo_base_url = 'https://' + host + '/v2/'
        self._repo = repo
        self._repo_url = self._repo_base_url + repo + '/'
        self._token = None
        self._headers = {}

    @property
    def token(self):
        return self._token

    @token.setter
    def token(self, value):
        self._token = value
        self._headers = {
            'Authorization': 'Bearer ' + value
        }

    def auth_by_password(self, username, password, *actions):
        r = requests.get(self._repo_base_url)
        if r.status_code != requests.codes.unauthorized:
            raise DXFUnexpectedStatusCodeError(r.status_code,
                                               requests.codes.unauthorized)
        info = _parse_www_auth(r.headers['www-authenticate'])
        url_parts = list(urlparse.urlparse(info['realm']))
        query = urlparse.parse_qs(url_parts[4])
        query.update(
        {
            'service': info['service'],
            'scope': 'repository:' + self._repo + ':' + ','.join(actions)
        })
        url_parts[4] = urllib.urlencode(query, True)
        url_parts[0] = 'https'
        auth_url = urlparse.urlunparse(url_parts)
        headers = {
            'Authorization': 'Basic ' + base64.b64encode(
                os.environ['DXF_USERNAME'] + ':' + os.environ['DXF_PASSWORD'])
        }
        r = requests.get(auth_url, headers=headers)
        r.raise_for_status()
        return r.json()['token']

    def push_blob(self, filename):
        dgst = sha256_file(filename)
        start_url = self._repo_url + 'blobs/uploads/'
        r = requests.post(start_url, headers=self._headers)
        r.raise_for_status()
        upload_url = r.headers['Location']
        url_parts = list(urlparse.urlparse(upload_url))
        query = urlparse.parse_qs(url_parts[4])
        query.update({ 'digest': 'sha256:' + dgst })
        url_parts[4] = urllib.urlencode(query, True)
        url_parts[0] = 'https'
        upload_url = urlparse.urlunparse(url_parts)
        with open(filename, 'rb') as f:
            r = requests.put(upload_url, data=f, headers=self._headers)
        r.raise_for_status()
        return dgst

    def pull_blob(self, digest):
        download_url = self._repo_url + 'blobs/sha256:' + digest
        r = requests.get(download_url, headers=self._headers)
        r.raise_for_status()
        sha256 = hashlib.sha256()
        for chunk in r.iter_content(8192):
            sha256.update(chunk)
            yield chunk
        dgst = sha256.hexdigest()
        if dgst != digest:
            raise DXFDigestMismatchError(dgst, digest)

    def del_blob(self, digest):
        delete_url = self._repo_url + 'blobs/sha256:' + digest
        r = requests.delete(delete_url, headers=self._headers)
        r.raise_for_status()

    def set_alias(self, alias, *digests):
        manifest = {
            'name': self._repo,
            'tag': alias,
            'fsLayers': [{ 'blobSum': 'sha256:' + dgst } for dgst in digests]
        }
        manifest_json = json.dumps(manifest)
        manifest64 = base64.urlsafe_b64encode(manifest_json).rstrip('=')
        format_length = manifest_json.rfind('}')
        format_tail = manifest_json[format_length:]
        protected_json = json.dumps({
            'formatLength': format_length,
            'formatTail': base64.urlsafe_b64encode(format_tail).rstrip('=')
        })
        protected64 = base64.urlsafe_b64encode(protected_json).rstrip('=')
        key = ecdsa.SigningKey.generate(curve=ecdsa.NIST256p)
        point = key.privkey.public_key.point
        data = {
            'key': key,
            'header': {
                'alg': 'ES256'
            }
        }
        jws.header.process(data, 'sign')
        sig = data['signer']("%s.%s" % (protected64, manifest64), key)
        signatures = [{
            'header': {
                'jwk': {
                    'kty': 'EC',
                    'crv': 'P-256',
                    'x': _num_to_base64(point.x()),
                    'y': _num_to_base64(point.y())
                },
                'alg': 'ES256'
            },
            'signature': base64.urlsafe_b64encode(sig).rstrip('='),
            'protected': protected64
        }]
        manifest_json = manifest_json[:format_length] + \
                        ', "signatures": ' + json.dumps(signatures) + \
                        format_tail
        upload_url = self._repo_url + 'manifests/' + alias
        #print _verify_manifest(manifest_json)
        r = requests.put(upload_url, headers=self._headers, data=manifest_json)
        r.raise_for_status()
        return manifest_json

    def get_alias(self, alias):
        download_url = self._repo_url + 'manifests/' + alias
        r = requests.get(download_url, headers=self._headers)
        r.raise_for_status()
        return _verify_manifest(r.content, r.headers['docker-content-digest'])

    def del_alias(self, alias):
        dgsts = self.get_alias(alias)
        delete_url = self._repo_url + 'manifests/' + alias
        r = requests.delete(delete_url, headers=self._headers)
        r.raise_for_status()
        return dgsts