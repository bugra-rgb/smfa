"""Bound request bodies before JSON/multipart parsing, including chunked bodies."""
import asyncio
import re

from starlette.responses import JSONResponse


def is_brand_upload(path: str, method: str) -> bool:
    return method == "POST" and re.fullmatch(r"/api/accounts/[^/]+/(logo|media|reels-cover-assets)", path) is not None


class RequestGuard:
    def __init__(self, app, json_limit=1024 * 1024, upload_limit=10 * 1024 * 1024, timeout=20):
        self.app, self.json_limit, self.upload_limit, self.timeout = app, json_limit, upload_limit, timeout

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])

        async def reject(status, detail):
            await JSONResponse({"detail": detail}, status_code=status, headers={
                "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            })(scope, receive, send)

        if sum(len(k) + len(v) for k, v in headers) > 16384:
            return await reject(431, "İstek başlıkları çok büyük.")
        lengths = [v for k, v in headers if k.lower() == b"content-length"]
        if len(lengths) > 1 or (lengths and (len(lengths[0]) > 12 or not lengths[0].isdigit())):
            return await reject(400, "İstek uzunluğu geçersiz.")
        limit = self.upload_limit if is_brand_upload(scope["path"], scope["method"]) else self.json_limit
        expected = int(lengths[0]) if lengths else None
        if expected is not None and expected > limit:
            return await reject(413, "İstek boyutu izin verilen sınırı aşıyor.")
        if scope["method"] in ("GET", "HEAD", "OPTIONS"):
            if expected not in (None, 0):
                return await reject(400, "Bu istek gövde içermemeli.")
            return await self.app(scope, receive, send)
        if any(k.lower() == b"content-encoding" and v.lower() != b"identity" for k, v in headers):
            return await reject(415, "Sıkıştırılmış istek gövdesi desteklenmiyor.")
        body = bytearray()
        try:
            async with asyncio.timeout(self.timeout):
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        return
                    chunk = event.get("body", b"")
                    if len(body) + len(chunk) > limit:
                        return await reject(413, "İstek boyutu izin verilen sınırı aşıyor.")
                    body.extend(chunk)
                    if not event.get("more_body", False):
                        break
        except TimeoutError:
            return await reject(408, "İstek aktarımı zaman aşımına uğradı.")
        if expected is not None and len(body) != expected:
            return await reject(400, "İstek uzunluğu eşleşmiyor.")
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
