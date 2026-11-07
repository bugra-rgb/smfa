"""Official OAuth flows. State/PKCE lifecycle and encrypted persistence live in API."""
from __future__ import annotations

import base64
import hashlib
import time
from urllib.parse import urlencode

import httpx

from .base import ProviderError, bearer, checked

SCOPES = {
    "x": ["tweet.read", "tweet.write", "users.read", "media.write", "offline.access"],
    "drive": ["https://www.googleapis.com/auth/drive"],
    "youtube": ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly", "https://www.googleapis.com/auth/yt-analytics.readonly"],
    "instagram": ["instagram_business_basic", "instagram_business_content_publish", "instagram_business_manage_insights"],
    "facebook": ["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
    # This private factory does not request TikTok posting permission.
    "tiktok": ["user.info.basic", "video.list"],
}


def _config(config=None):
    if config is None:
        from factory.config import settings
        return settings
    return config


def _keys(provider, config):
    c = _config(config)
    prefix = {"drive": "google", "youtube": "youtube", "instagram": "instagram", "facebook": "facebook", "tiktok": "tiktok", "x": "x"}.get(provider)
    if not prefix:
        raise ProviderError("Desteklenmeyen OAuth sağlayıcısı.", "unknown_provider")
    key = getattr(c, f"{prefix}_client_key" if provider == "tiktok" else f"{prefix}_client_id", "")
    secret = getattr(c, f"{prefix}_client_secret", "")
    if provider == "youtube":
        key = key or getattr(c, "google_client_id", "")
        secret = secret or getattr(c, "google_client_secret", "")
    if not key or not secret:
        raise ProviderError(f"{provider} OAuth uygulama ayarları eksik.", "configuration_missing")
    return key, secret


def authorization_url(provider, redirect_uri, state, code_verifier, *, config=None):
    key, _ = _keys(provider, config)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).decode().rstrip("=")
    params = {"client_id": key, "redirect_uri": redirect_uri, "response_type": "code", "state": state}
    if provider in {"drive", "youtube"}:
        params.update(scope=" ".join(SCOPES[provider]), access_type="offline", prompt="consent",
                      include_granted_scopes="true", code_challenge=challenge, code_challenge_method="S256")
        base = "https://accounts.google.com/o/oauth2/v2/auth"
    elif provider == "x":
        base = "https://x.com/i/oauth2/authorize"
        params.update(scope=" ".join(SCOPES[provider]), code_challenge=challenge, code_challenge_method="S256")
    elif provider == "instagram":
        base = "https://www.instagram.com/oauth/authorize"
        params.update(scope=",".join(SCOPES[provider]), enable_fb_login="0", force_authentication="1")
    elif provider == "facebook":
        from .facebook import graph
        version = _config(config).graph_api_version
        graph(version)
        base = f"https://www.facebook.com/{version}/dialog/oauth"
        params.update(scope=",".join(SCOPES[provider]), auth_type="rerequest")
    else:
        base = "https://www.tiktok.com/v2/auth/authorize/"
        params.pop("client_id")
        params.update(client_key=key, scope=",".join(SCOPES[provider]))
    return base + "?" + urlencode(params)


def _expiry(payload, previous=None):
    result = {**(previous or {}), **payload}
    if not result.get("access_token"):
        raise ProviderError("OAuth yanıtında erişim anahtarı alınamadı.", "oauth_invalid_response")
    result["expires_at"] = int(time.time()) + int(payload.get("expires_in", 3600))
    result["refreshed_at"] = int(time.time())
    return result


def exchange_code(provider, code, redirect_uri, code_verifier, *, config=None, client=None):
    key, secret = _keys(provider, config)
    with _client(client) as c:
        data = {"client_id": key, "client_secret": secret, "code": code,
                "redirect_uri": redirect_uri, "grant_type": "authorization_code"}
        if provider in {"drive", "youtube"}:
            data["code_verifier"] = code_verifier
            payload = checked(c.post("https://oauth2.googleapis.com/token", data=data)).json()
        elif provider == "x":
            data.pop("client_secret")
            data["code_verifier"] = code_verifier
            payload = checked(c.post("https://api.x.com/2/oauth2/token", data=data, auth=(key, secret))).json()
        elif provider == "instagram":
            initial = checked(c.post("https://api.instagram.com/oauth/access_token", data=data)).json()
            payload = checked(c.get("https://graph.instagram.com/access_token", params={
                "grant_type": "ig_exchange_token", "client_secret": secret, "access_token": initial["access_token"]})).json()
            payload["user_id"] = initial.get("user_id")
            if isinstance(initial.get("permissions"), list):
                payload["scope"] = initial["permissions"]
        elif provider == "facebook":
            from .facebook import graph, checked as fb_checked
            base = graph(_config(config).graph_api_version)
            payload = fb_checked(c.get(base + "/oauth/access_token", params={
                "client_id": key, "client_secret": secret, "code": code, "redirect_uri": redirect_uri})).json()
            payload = fb_checked(c.get(base + "/oauth/access_token", params={
                "client_id": key, "client_secret": secret, "grant_type": "fb_exchange_token",
                "fb_exchange_token": payload["access_token"]})).json()
            granted = fb_checked(c.get(base + "/me/permissions", headers=bearer(payload))).json().get("data", [])
            payload["scope"] = [p["permission"] for p in granted if p.get("status") == "granted"]
            payload["credential_kind"] = "facebook_user"
            payload["expires_in"] = payload.get("expires_in", payload.get("expires", 3600))
        else:
            data["client_key"] = data.pop("client_id")
            payload = checked(c.post("https://open.tiktokapis.com/v2/oauth/token/", data=data)).json()
        return _expiry(payload)


def refresh(provider, credentials, *, config=None, client=None):
    key, secret = _keys(provider, config)
    if provider == "facebook":
        raise ProviderError("Facebook bağlantısının süresi doldu; Sayfayı yeniden bağlayın.", "oauth_expired")
    with _client(client) as c:
        if provider == "instagram":
            # Instagram tokens must be >=24h old and still valid when refreshed.
            payload = checked(c.get("https://graph.instagram.com/refresh_access_token", params={
                "grant_type": "ig_refresh_token", "access_token": credentials["access_token"]})).json()
        else:
            if not credentials.get("refresh_token"):
                raise ProviderError("Yenileme anahtarı yok; hesabı tekrar bağlayın.", "oauth_expired")
            data = {"client_id": key, "client_secret": secret, "refresh_token": credentials["refresh_token"], "grant_type": "refresh_token"}
            url = "https://oauth2.googleapis.com/token"
            if provider == "tiktok":
                data["client_key"] = data.pop("client_id")
                url = "https://open.tiktokapis.com/v2/oauth/token/"
            if provider == "x":
                data.pop("client_secret")
                payload = checked(c.post("https://api.x.com/2/oauth2/token", data=data, auth=(key, secret))).json()
            else:
                payload = checked(c.post(url, data=data)).json()
        return _expiry(payload, credentials)


def resolve_identity(provider, credentials, *, config=None, client=None):
    with _client(client) as c:
        headers = bearer(credentials)
        if provider == "drive":
            user = checked(c.get("https://www.googleapis.com/drive/v3/about", headers=headers, params={"fields": "user"})).json()["user"]
            return {"external_id": user["permissionId"], "username": user.get("emailAddress", ""), "name": user.get("displayName", "")}
        if provider == "youtube":
            items = checked(c.get("https://www.googleapis.com/youtube/v3/channels", headers=headers, params={"part": "snippet", "mine": "true"})).json().get("items", [])
            if not items:
                raise ProviderError("Bu Google hesabında YouTube kanalı bulunamadı.", "identity_missing")
            item = items[0]
            return {"external_id": item["id"], "username": item["snippet"].get("customUrl", item["snippet"]["title"])}
        if provider == "instagram":
            version = getattr(_config(config), "graph_api_version", "v24.0")
            item = checked(c.get(f"https://graph.instagram.com/{version}/me", headers=headers, params={"fields": "user_id,username"})).json()
            return {"external_id": str(item.get("user_id") or item["id"]), "username": item["username"]}
        if provider == "facebook":
            from .facebook import graph, checked as fb_checked
            item = fb_checked(c.get(graph(_config(config).graph_api_version) + "/me", headers=headers, params={"fields": "id,name"})).json()
            return {"external_id": str(item["id"]), "username": str(item.get("name") or item["id"])}
        if provider == "x":
            item = checked(c.get("https://api.x.com/2/users/me", headers=headers)).json()["data"]
            return {"external_id": item["id"], "username": item["username"], "name": item.get("name", "")}
        if provider == "tiktok":
            item = checked(c.get("https://open.tiktokapis.com/v2/user/info/", headers=headers, params={"fields": "open_id,display_name"})).json()
            if item.get("error", {}).get("code") not in (None, "ok"):
                raise ProviderError("TikTok hesap bilgisi alınamadı.", "identity_missing")
            return {"external_id": item["data"]["user"]["open_id"], "username": item["data"]["user"]["display_name"]}
        raise ProviderError("Desteklenmeyen OAuth sağlayıcısı.", "unknown_provider")


class _client:
    def __init__(self, client):
        self.client, self.owned = client or httpx.Client(timeout=45, follow_redirects=False), client is None
    def __enter__(self):
        return self.client
    def __exit__(self, *_):
        if self.owned:
            self.client.close()


def instagram_permissions(credentials, *, config=None, client=None, force_refresh=False):
    """Read granted permissions; requested OAuth scopes are never treated as grants."""
    if not force_refresh and isinstance(credentials.get("scope"), list) and credentials["scope"]:
        return [s for s in credentials["scope"] if isinstance(s, str)]
    version=_config(config).graph_api_version
    with _client(client) as c:
        payload=checked(c.get(f'https://graph.instagram.com/{version}/me/permissions',headers=bearer(credentials))).json()
        rows=payload.get('data')
        if not isinstance(rows,list):
            raise ProviderError('Instagram izinleri doğrulanamadı.','permissions_unverified')
        # Some responses group permission names inside one permissions list.
        granted=[]
        for item in rows:
            if item.get('status')=='granted' and isinstance(item.get('permission'),str):granted.append(item['permission'])
        return granted
