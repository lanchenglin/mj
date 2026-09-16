"""Bounded HTTPS for native media APIs. No redirects, proxy inheritance or POST retry.

DNS is checked once and the request connects to that public IP while preserving
Host and TLS SNI. API secrets never accompany result-CDN requests.
"""
from __future__ import annotations

import contextlib
import ipaddress
import socket
from urllib.parse import urlsplit
import httpx
from .providers import UnknownSubmission


class ApiFailure(Exception):
    safe_failure = True
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def validate_url(url, hosts):
    p = urlsplit(url)
    if (p.scheme != "https" or not p.hostname or p.hostname not in hosts
            or p.username or p.password or p.port not in (None, 443) or p.fragment):
        raise ApiFailure("API/result URL is not on the administrator HTTPS host allowlist")
    return p


def request(method, url, hosts, *, headers=None, body=None, limit=64*1024*1024, destination=None):
    p = validate_url(url, hosts)
    try:
        ips = {x[4][0] for x in socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise ApiFailure("API DNS lookup failed; no request sent", retryable=True) from exc
    if not ips or any(not ipaddress.ip_address(i).is_global or getattr(ipaddress.ip_address(i), "ipv4_mapped", None) or i == "168.63.129.16" for i in ips):
        raise ApiFailure("API DNS resolves to a private, reserved or metadata address")
    target = httpx.URL(url).copy_with(host=sorted(ips)[0])
    h = {"Host": p.hostname, "Accept-Encoding": "identity", **(headers or {})}
    temp = destination.with_suffix(destination.suffix + ".part") if destination else None
    try:
        with httpx.Client(timeout=httpx.Timeout(240, connect=20), trust_env=False,
                          follow_redirects=False, transport=httpx.HTTPTransport(retries=0)) as client:
            req = client.build_request(method, target, headers=h, json=body)
            req.extensions["sni_hostname"] = p.hostname
            response = client.send(req, stream=True)
            try:
                status = response.status_code
                if not 200 <= status < 300:
                    if method == "POST" and (status >= 500 or status in (408, 409)):
                        raise UnknownSubmission("API submission outcome unknown; do not repeat generation")
                    raise ApiFailure(f"API HTTP {status}; inspect provider console", status >= 500 or status == 429)
                if int(response.headers.get("content-length", "0")) > limit:
                    raise ApiFailure("API response exceeds size limit")
                size, chunks = 0, []
                if temp:
                    temp.parent.mkdir(parents=True, exist_ok=True)
                with (temp.open("wb") if temp else contextlib.nullcontext()) as f:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise ApiFailure("API response exceeds size limit")
                        if f: f.write(chunk)
                        else: chunks.append(chunk)
                if temp:
                    temp.replace(destination)
                    return b""
                return b"".join(chunks)
            finally:
                response.close()
    except httpx.TransportError as exc:
        if method == "POST":
            raise UnknownSubmission("API submission response lost; inspect billing, no automatic resubmit") from exc
        raise ApiFailure("API result query/download temporarily unavailable", retryable=True) from exc
    finally:
        if temp: temp.unlink(missing_ok=True)
