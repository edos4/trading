"""Normalize supported video links without accepting arbitrary server fetches."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


VIDEO_HOSTS = {"facebook.com", "fb.watch", "youtube.com", "youtu.be", "vimeo.com",
               "instagram.com", "tiktok.com", "x.com", "twitter.com"}
TRACKING = {"fbclid", "mibextid", "si", "feature", "share", "share_url", "rdid"}


def canonical_video_url(value: str) -> str:
    """Keep content-identifying query fields; remove fragments and tracking."""
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise ValueError("Enter one valid public video URL")
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower()
        port = url.port
    except ValueError:
        raise ValueError("Enter one valid public video URL") from None
    if url.scheme not in {"http", "https"} or url.username or url.password or port not in {None, 80, 443}:
        raise ValueError("Use a public HTTP(S) video link without credentials or a custom port")
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    if host not in VIDEO_HOSTS:
        raise ValueError("Supported video sites: Facebook, YouTube, Vimeo, Instagram, TikTok and X")
    query = [(k, v) for k, v in parse_qsl(url.query, keep_blank_values=True)
             if k.lower() not in TRACKING and not k.lower().startswith("utm_")]
    path = url.path.rstrip("/") or "/"
    if host == "youtube.com":
        if path == "/watch":
            query = [(k, v) for k, v in query if k == "v"]
            if not query:
                raise ValueError("Use a video link, not a channel or playlist")
        elif not path.startswith(("/shorts/", "/live/", "/embed/")):
            raise ValueError("Use a video link, not a channel or playlist")
    if path == "/" and not query:
        raise ValueError("Use a specific video URL")
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def check_public_fetch(value: str) -> None:
    """Apply to extractor requests/redirects too, not just the submitted URL."""
    url = urlsplit(value)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
        raise ValueError("Media request must use public HTTP(S)")
    if url.port not in {None, 80, 443}:
        raise ValueError("Media request uses an unsupported port")
    addresses = socket.getaddrinfo(url.hostname, url.port or (443 if url.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("Media request did not resolve to public addresses")
