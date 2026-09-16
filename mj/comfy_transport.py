"""Bounded, origin-fixed transport for an administrator-approved self-hosted GPU.

Connect to an approved IP directly, preserving Host/TLS SNI. No redirects,
proxy inheritance, private-network wildcard, certificate bypass, or POST retry.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
from urllib.parse import urlsplit

import httpx

LIMIT = 32 * 1024 * 1024
PRIVATE = tuple(ipaddress.ip_network(x) for x in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7", "100.64.0.0/10"))


class ComfyFailure(Exception):
    """Known non-submission or terminal remote failure; safe to display."""
    safe_failure = True
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def validate_endpoint(cfg: dict):
    p = urlsplit(cfg.get("base_url", ""))
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError("Comfy base_url must be an administrator HTTP(S) origin without credentials/query")
    if p.path not in ("", "/"):
        raise ValueError("Use a dedicated Comfy origin, not a URL subpath")
    # Fixed host+port in an administrator-only config; API users cannot supply URLs.
    if p.hostname not in cfg.get("hosts", []) or "%" in p.hostname or not 1 <= (p.port or (443 if p.scheme == "https" else 80)) <= 65535:
        raise ValueError("Comfy origin is not on the exact host allowlist")
    ips = cfg.get("allowed_ips", [])
    if not isinstance(ips, list) or not 1 <= len(ips) <= 16:
        raise ValueError("Configure 1..16 exact allowed_ips, never a subnet wildcard")
    for raw in ips:
        ip = ipaddress.ip_address(raw)
        if str(ip) in ("168.63.129.16", "fd00:ec2::254") or ip.is_link_local or ip.is_multicast or ip.is_unspecified or getattr(ip, "ipv4_mapped", None):
            raise ValueError("Metadata, link-local, multicast and mapped addresses are forbidden")
        private = any(ip in n for n in PRIVATE if n.version == ip.version)
        if ip.is_loopback:
            if cfg.get("allow_loopback") is not True:
                raise ValueError("Loopback requires explicit administrator opt-in")
        elif private:
            if cfg.get("allow_private") is not True:
                raise ValueError("Private GPU addresses require explicit administrator opt-in")
        elif not ip.is_global:
            raise ValueError("Non-routable Comfy address is forbidden")
        if p.scheme == "http" and (cfg.get("allow_insecure_http") is not True or not (private or ip.is_loopback)):
            raise ValueError("HTTP is allowed only on explicitly approved private/tunnel addresses")
    auth = cfg.get("auth", "none")
    if auth not in ("none", "bearer"):
        raise ValueError("Supported Comfy proxy authentication: none or bearer")
    if auth == "bearer" and not re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", cfg.get("key_env", "")):
        raise ValueError("Use key_env for proxy bearer token; never a literal key")
    if p.scheme == "https" and any(ipaddress.ip_address(i).is_global for i in ips) and auth != "bearer":
        raise ValueError("Public Comfy access requires authenticated HTTPS reverse proxy")
    return p


class Client:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.origin = validate_endpoint(cfg)

    def request(self, method: str, path: str, *, params=None, json=None, files=None, data=None) -> bytes:
        allowed = {("GET", "/system_stats"), ("GET", "/object_info"), ("GET", "/queue"),
                   ("GET", "/history"), ("GET", "/view"), ("POST", "/upload/image"), ("POST", "/prompt")}
        if (method, path) not in allowed and not (method == "GET" and re.fullmatch(r"/history/[A-Za-z0-9_-]{1,150}", path)):
            raise ComfyFailure("Comfy route is not allowed")
        p = self.origin
        port = p.port or (443 if p.scheme == "https" else 80)
        try:
            literal = ipaddress.ip_address(p.hostname)
            resolved = {str(literal)}
        except ValueError:
            try:
                resolved = {x[4][0] for x in socket.getaddrinfo(p.hostname, port, type=socket.SOCK_STREAM)}
            except OSError as exc:
                raise ComfyFailure("Comfy DNS lookup failed", retryable=True) from exc
        if not resolved or not resolved <= set(self.cfg["allowed_ips"]):
            raise ComfyFailure("Comfy DNS differs from approved IPs; administrator must verify the server")
        host = f"[{p.hostname}]" if ":" in p.hostname else p.hostname
        authority = f"{host}:{port}"
        headers = {"Host": authority, "Accept-Encoding": "identity"}
        if self.cfg.get("auth", "none") == "bearer":
            key = os.getenv(self.cfg["key_env"], "")
            if not key or "\n" in key or "\r" in key:
                raise ComfyFailure("Comfy proxy token environment variable is missing or invalid")
            headers["Authorization"] = "Bearer " + key
        ip = sorted(resolved)[0]
        url = httpx.URL(self.cfg["base_url"].rstrip("/") + path).copy_with(host=ip)
        with httpx.Client(timeout=httpx.Timeout(60, connect=10), follow_redirects=False,
                          trust_env=False, transport=httpx.HTTPTransport(retries=0)) as client:
            req = client.build_request(method, url, headers=headers, params=params, json=json, files=files, data=data)
            # HTTPX/httpcore extension preserves certificate verification for DNS hosts.
            req.extensions["sni_hostname"] = p.hostname
            response = client.send(req, stream=True)
            try:
                if response.status_code >= 300:
                    # Never leak provider response bodies/headers/URLs into audit errors.
                    err = ComfyFailure(f"Comfy HTTP {response.status_code}; check GPU service", retryable=response.status_code >= 500 or response.status_code == 429)
                    err.status_code = response.status_code
                    raise err
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > LIMIT:
                        raise ComfyFailure("Comfy response exceeds 32 MiB limit")
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                response.close()
