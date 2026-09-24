"""Клиент OAuth Я ID.

Авторизация: https://yandex.ru/dev/id/doc/ru/codes/code-url
Профиль:    https://yandex.ru/dev/id/doc/ru/user-information
"""

from __future__ import annotations

import hashlib
import secrets
from base64 import urlsafe_b64encode
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from django.conf import settings

import requests


class YandexOAuthError(RuntimeError):
    """Ошибка обмена кода или запроса профиля Я ID."""


def is_configured() -> bool:
    return bool(settings.YANDEX_CLIENT_ID and settings.YANDEX_CLIENT_SECRET)


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorization_url() -> tuple[str, str, str]:
    """Возвращает (url, state, code_verifier)."""

    if not is_configured():
        raise YandexOAuthError("Я ID не настроен: задайте YANDEX_CLIENT_ID и YANDEX_CLIENT_SECRET")

    state = secrets.token_urlsafe(24)
    verifier, challenge = _pkce_pair()
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.YANDEX_CLIENT_ID,
            "redirect_uri": settings.YANDEX_REDIRECT_URI,
            "scope": settings.YANDEX_OAUTH_SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{settings.YANDEX_OAUTH_BASE}/authorize?{query}", state, verifier


def exchange_code(code: str, code_verifier: str) -> dict[str, Any]:
    """Меняет authorization code на access/refresh token."""

    response = requests.post(
        f"{settings.YANDEX_OAUTH_BASE}/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": settings.YANDEX_CLIENT_ID,
            "client_secret": settings.YANDEX_CLIENT_SECRET,
            "redirect_uri": settings.YANDEX_REDIRECT_URI,
            "code_verifier": code_verifier,
        },
        timeout=15,
    )
    payload = _json(response)
    if response.status_code >= 400:
        raise YandexOAuthError(payload.get("error_description") or payload.get("error") or "ошибка токена")
    return payload


def fetch_user_info(access_token: str) -> dict[str, Any]:
    response = requests.get(
        settings.YANDEX_USERINFO_URL,
        params={"format": "json"},
        headers={"Authorization": f"OAuth {access_token}"},
        timeout=15,
    )
    payload = _json(response)
    if response.status_code >= 400:
        raise YandexOAuthError(payload.get("error_description") or "не удалось получить профиль Я ID")
    return payload


def token_expires_at(expires_in: object) -> datetime | None:
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _json(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {"error_description": response.text[:300]}
    return data if isinstance(data, dict) else {"error_description": str(data)}
