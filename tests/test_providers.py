import json
import hashlib
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from factory.providers import get_publisher
from factory.providers.ai import OpenAIImageProvider, OpenAILLMProvider, RunwayVideoProvider
from factory.providers.base import ProviderError
from factory.providers.drive import GoogleDriveProvider
from factory.providers.oauth import SCOPES, authorization_url, exchange_code, refresh
from factory.providers.social import InstagramPublisher, TikTokPublisher, YouTubePublisher


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def file(tmp_path, content=b"1234567890"):
    path = tmp_path / "video.mp4"
    path.write_bytes(content)
    return str(path)


def test_youtube_session_is_durable_before_video_bytes_and_timeout_does_not_reinsert(tmp_path):
    saved, calls = {}, []
    attempt = {"failed": False}

    def save(cp):
        saved.clear()
        saved.update(cp)

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            assert saved["initiation_started"]
            return httpx.Response(200, headers={"Location": "https://www.googleapis.com/upload/session-1"})
        if request.method == "GET":
            return httpx.Response(200, json={"items": [{"status": {"uploadStatus": "processed", "privacyStatus": "private"}}]})
        assert saved["session_url"].endswith("session-1")
        if request.headers.get("content-range") == "bytes */10":
            return httpx.Response(201, json={"id": "youtube-existing"})
        attempt["failed"] = True
        raise httpx.ReadTimeout("lost upload response", request=request)

    provider = YouTubePublisher(client(handler))
    first = provider.publish(file(tmp_path), {"title": "test"}, {"access_token": "TOKEN"}, save_checkpoint=save)
    assert first["status"] == "PROCESSING" and attempt["failed"]
    second = provider.publish(file(tmp_path), {"title": "test"}, {"access_token": "TOKEN"}, checkpoint=saved, save_checkpoint=save)
    assert second["status"] == "PUBLISHED"
    assert second["remote_id"] == "youtube-existing"
    assert sum(method == "POST" for method, _ in calls) == 1
    assert sum(method == "PUT" for method, _ in calls) == 2


def test_youtube_expired_session_is_unknown_never_new_upload(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(404)
    result = YouTubePublisher(client(handler)).publish(file(tmp_path), {}, {"access_token": "TOKEN"},
        {"session_url": "https://www.googleapis.com/upload/expired"}, lambda _: None)
    assert result["status"] == "UNKNOWN"
    assert len(requests) == 1 and requests[0].method == "PUT"


def test_youtube_resumes_at_server_confirmed_offset(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"items": [{"status": {"uploadStatus": "uploaded", "privacyStatus": "private"}}]})
        if request.headers.get("content-range") == "bytes */10":
            return httpx.Response(308, headers={"Range": "bytes=0-3"})
        assert request.headers["content-range"] == "bytes 4-9/10"
        assert request.content == b"567890"
        return httpx.Response(201, json={"id": "same-upload"})
    result = YouTubePublisher(client(handler)).publish(file(tmp_path), {}, {"access_token": "TOKEN"},
        {"session_url": "https://www.googleapis.com/upload/session", "upload_size": 10,
         "fingerprint": hashlib.sha256(b"1234567890").hexdigest()}, lambda _: None)
    assert result["remote_id"] == "same-upload"
    assert all(r.method != "POST" for r in requests)


def test_instagram_commits_intent_before_publish_and_unknown_never_reposts(tmp_path):
    saved, calls = {}, []
    def save(cp):
        saved.clear()
        saved.update(cp)
    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "container-1"})
        if request.method == "GET":
            return httpx.Response(200, json={"status_code": "FINISHED"})
        assert saved["publish_started"] is True
        raise httpx.ReadTimeout("lost publish response", request=request)
    provider = InstagramPublisher("v24.0", client(handler))
    credentials = {"access_token": "TOKEN", "external_id": "ig-account"}
    result = provider.publish(file(tmp_path), {"public_video_url": "https://media.example/video.mp4", "caption": "test"}, credentials, save_checkpoint=save)
    assert result["status"] == "UNKNOWN"
    count = len(calls)
    again = provider.publish(file(tmp_path), {}, credentials, checkpoint=saved, save_checkpoint=save)
    assert again["status"] == "UNKNOWN" and len(calls) == count
    assert sum(r.url.path.endswith("/media_publish") for r in calls) == 1


def test_instagram_unfinished_container_is_never_published(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"status_code": "IN_PROGRESS"})
    result = InstagramPublisher("v24.0", client(handler)).publish(file(tmp_path), {},
        {"access_token": "TOKEN", "external_id": "account"}, {"container_id": "pending"}, lambda _: None)
    assert result["status"] == "PROCESSING"
    assert len(requests) == 1 and requests[0].method == "GET"


def test_tiktok_private_autonomous_factory_blocked_even_with_fake_approval(tmp_path):
    def handler(request):
        pytest.fail("TikTok publishing must never make a network call")
    result = TikTokPublisher(client(handler)).publish(file(tmp_path), {"approved": True}, {"access_token": "TOKEN", "audit_approved": True})
    assert result["status"] == "POLICY_BLOCKED"
    assert result["handoff"]["policy_url"].startswith("https://developers.tiktok.com/")


def test_production_never_falls_back_to_mock(monkeypatch):
    transport_client = client(lambda request: pytest.fail("Registry must not contact providers"))
    monkeypatch.setattr("factory.providers.social.httpx.Client", lambda **kwargs: transport_client)
    assert isinstance(get_publisher("tiktok", SimpleNamespace(mode="production")), TikTokPublisher)
    with pytest.raises(ProviderError, match="Bilinmeyen"):
        get_publisher("browser-hack", SimpleNamespace(mode="production"))


def test_drive_list_paginates():
    def handler(request):
        assert "trashed = false" in request.url.params["q"]
        if request.url.params.get("pageToken") == "page-2":
            return httpx.Response(200, json={"files": [{"id": "b"}]})
        return httpx.Response(200, json={"files": [{"id": "a"}], "nextPageToken": "page-2"})
    files = GoogleDriveProvider({"access_token": "TOKEN"}, client(handler)).list_files("input")
    assert [item["id"] for item in files] == ["a", "b"]


def test_drive_upload_reserved_id_persisted_and_existing_file_not_duplicated(tmp_path):
    saved, requests = {}, []
    def save(cp):
        saved.clear()
        saved.update(cp)
    def handler(request):
        requests.append(request)
        if request.url.path.endswith("generateIds"):
            return httpx.Response(200, json={"ids": ["reserved-id"]})
        if request.method == "POST":
            assert saved["file_id"] == "reserved-id"
            assert json.loads(request.content)["id"] == "reserved-id"
            return httpx.Response(200, headers={"Location": "https://www.googleapis.com/upload/drive-session"})
        if request.method == "PUT":
            assert saved["session_url"]
            return httpx.Response(200, json={"id": "reserved-id", "size": "10"})
        return httpx.Response(200, json={"id": "reserved-id", "size": "10"})
    provider = GoogleDriveProvider({"access_token": "TOKEN"}, client(handler))
    first = provider.upload(file(tmp_path), "folder", "stable-job", save_checkpoint=save)
    second = provider.upload(file(tmp_path), "folder", "stable-job", checkpoint=saved, save_checkpoint=save)
    assert first["file_id"] == second["file_id"] == "reserved-id"
    assert sum(r.method == "POST" for r in requests) == 1
    assert sum(r.method == "PUT" for r in requests) == 1


def test_google_session_host_not_trusted_from_checkpoint(tmp_path):
    with pytest.raises(ProviderError) as exc:
        YouTubePublisher(client(lambda r: pytest.fail("Unsafe URL requested"))).publish(file(tmp_path), {},
            {"access_token": "TOKEN"}, {"session_url": "https://attacker.invalid/steal"}, lambda _: None)
    assert exc.value.code == "unsafe_remote_url"


def test_production_mutation_requires_checkpoint_callback(tmp_path):
    with pytest.raises(ProviderError) as exc:
        YouTubePublisher(client(lambda r: pytest.fail("Missing checkpoint callback"))).publish(file(tmp_path), {"title": "x"}, {"access_token": "TOKEN"})
    assert exc.value.code == "checkpoint_required"


def test_oauth_google_pkce_scope_and_refresh_rotation():
    config = SimpleNamespace(google_client_id="id", google_client_secret="secret")
    url = authorization_url("drive", "https://example.com/callback", "state", "verifier", config=config)
    params = parse_qs(urlparse(url).query)
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"] == ["state"]
    assert params["access_type"] == ["offline"]
    assert params["scope"] == [" ".join(SCOPES["drive"])]
    def handler(request):
        payload = parse_qs(request.content.decode())
        assert payload["refresh_token"] == ["old-refresh"]
        return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600})
    result = refresh("drive", {"access_token": "old-access", "refresh_token": "old-refresh"}, config=config, client=client(handler))
    assert result["access_token"] == "new-access" and result["refresh_token"] == "new-refresh"
    assert result["expires_at"] > result["refreshed_at"]


def test_invalid_grant_is_nonretryable_reconnect_without_token_leak():
    config = SimpleNamespace(google_client_id="id", google_client_secret="SUPER_SECRET")
    with pytest.raises(ProviderError) as exc:
        exchange_code("drive", "code", "https://example.com/callback", "verifier", config=config,
            client=client(lambda r: httpx.Response(400, json={"error": "invalid_grant", "error_description": "secret SUPER_SECRET"})))
    assert exc.value.code == "oauth_expired" and not exc.value.retryable
    assert "SUPER_SECRET" not in str(exc.value)


def test_ai_research_uses_real_tool_sources_not_model_invented_sources():
    def handler(request):
        payload = json.loads(request.content)
        assert payload["tools"] == [{"type": "web_search"}]
        assert "web_search_call.action.sources" in payload["include"]
        ideas = {"ideas": [{"title": "checked", "source_urls": ["https://official.example/fact"]},
                           {"title": "invented", "source_urls": ["https://fake.example/fact"]}], "sources": [{"url": "https://fake.example/fact"}]}
        return httpx.Response(200, json={"output": [
            {"type": "web_search_call", "action": {"sources": [{"url": "https://official.example/fact", "title": "Official"}]}},
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(ideas)}]}], "usage": {"input_tokens": 123, "output_tokens": 45}})
    result = OpenAILLMProvider("KEY", "model", client(handler)).research({"niche": "news"}, [])
    assert [idea["title"] for idea in result["ideas"]] == ["checked"]
    assert result["sources"][0]["url"] == "https://official.example/fact"
    assert result["usage"]["actual_cost"] is None


def test_image_unknown_result_does_not_regenerate(tmp_path):
    saved, calls = {}, []
    def save(cp):
        saved.clear()
        saved.update(cp)
    def handler(request):
        calls.append(request)
        assert saved["image_started"]
        raise httpx.ReadTimeout("timeout", request=request)
    provider = OpenAIImageProvider("KEY", "image-model", client(handler))
    result = provider.generate("x", str(tmp_path / "img.png"), {"save_checkpoint": save})
    again = provider.generate("x", str(tmp_path / "img.png"), {"checkpoint": saved, "save_checkpoint": save})
    assert result["status"] == again["status"] == "UNKNOWN" and len(calls) == 1


def test_runway_resumes_task_and_does_not_resubmit(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "RUNNING"})
    result = RunwayVideoProvider("KEY", "gen4.5", client(handler)).generate("prompt", file(tmp_path),
        {"checkpoint": {"started": True, "task_id": "task-123"}, "save_checkpoint": lambda _: None})
    assert result["status"] == "PROCESSING"
    assert len(requests) == 1 and requests[0].method == "GET"


def test_image_motion_produces_real_decodable_video_from_official_image_response(tmp_path):
    import base64
    import struct
    import subprocess
    import zlib
    from factory.providers.ai import ImageMotionVideoProvider

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 96, 8, 2, 0, 0, 0))
    pixels = b"".join(b"\x00" + bytes([40 + y % 100, 100, 180]) * 64 for y in range(96))
    png += chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")
    def handler(request):
        assert json.loads(request.content)["output_format"] == "png"
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png).decode()}]})
    provider = ImageMotionVideoProvider(OpenAIImageProvider("KEY", "configured-image-model", client(handler)))
    path = tmp_path / "motion.mp4"
    result = provider.generate("gradient", str(path), {"width": 64, "height": 96, "duration": 1, "save_checkpoint": lambda _: None})
    assert result["status"] == "COMPLETED" and path.stat().st_size > 500
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)], capture_output=True, text=True, check=True, timeout=20)
    video = json.loads(probe.stdout)["streams"][0]
    assert (video["width"], video["height"], video["codec_name"]) == (64, 96, "h264")


def test_runway_three_resumable_scenes_stitch_thirty_seconds(tmp_path):
    import subprocess
    sample = tmp_path / "sample.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=64x96:rate=10", "-t", "10", "-c:v", "libx264", "-threads", "1", str(sample)], check=True, timeout=30)
    data = sample.read_bytes()
    saved, requests = {}, []
    created = []
    scene_plan = [{"description": label, "duration": 10} for label in ("Harbor sunrise", "Mountain river", "City sunset")]
    def save(cp):
        saved.clear()
        saved.update(json.loads(json.dumps(cp)))
    def handler(request):
        requests.append(request)
        if request.method == "POST":
            payload = json.loads(request.content)
            assert request.url.path == "/v1/image_to_video"
            assert payload["model"] == "gen4.5" and "promptImage" not in payload
            slot = len(created)
            assert scene_plan[slot]["description"] in payload["promptText"]
            assert saved["scenes"][slot]["started"] is True
            task_id = f"task-{slot}"
            created.append(task_id)
            return httpx.Response(200, json={"id": task_id})
        if request.url.host == "cdn.runway.example":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=data)
        task_id = request.url.path.rsplit("/", 1)[-1]
        assert any(s.get("task_id") == task_id for s in saved["scenes"])
        return httpx.Response(200, json={"status": "SUCCEEDED", "output": [f"https://cdn.runway.example/{task_id}.mp4"]})
    provider = RunwayVideoProvider("KEY", "gen4.5", client(handler))
    output = tmp_path / "thirty.mp4"
    for _ in range(4):
        result = provider.generate("scene", str(output), {"duration": 30, "width": 64, "height": 96,
            "checkpoint": saved, "save_checkpoint": save, "scenes": scene_plan})
    assert result["status"] == "COMPLETED" and len(created) == 3
    assert len(saved["scenes"]) == 3 and all(s["complete"] for s in saved["scenes"])
    assert result["usage"]["requests"] == 3 and result["usage"]["seconds"] == 30
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-of", "json", str(output)], capture_output=True, text=True, check=True, timeout=20)
    assert 29.8 <= float(json.loads(probe.stdout)["format"]["duration"]) <= 30.2
    count = len(requests)
    assert provider.generate("scene", str(output), {"duration": 30, "checkpoint": saved, "save_checkpoint": save, "scenes": scene_plan})["status"] == "COMPLETED"
    assert len(requests) == count


def test_transcription_extracts_bounded_audio_from_video(tmp_path):
    from factory.media.engine import generate_mock_video
    from factory.providers.ai import OpenAIVoiceProvider
    video = tmp_path / "source.mp4"
    generate_mock_video(video, duration=3, width=64, height=96)
    def handler(request):
        assert request.url.path == "/v1/audio/transcriptions"
        assert b'filename="speech.m4a"' in request.content
        assert len(request.content) < 25 * 1024 * 1024
        return httpx.Response(200, json={"text": "Deneme", "segments": [{"start": 0, "end": 1, "text": "Deneme"}]})
    result = OpenAIVoiceProvider("KEY", "voice-model", client=client(handler)).transcribe(str(video), {"duration": 2, "trim_start": 0.2, "speed": 1})
    assert result["text"] == "Deneme" and result["segments"][0]["end"] == 1


def test_youtube_analytics_normalizes_advanced_watch_and_retention_metrics():
    def handler(request):
        if request.url.host == "www.googleapis.com":
            return httpx.Response(200, json={"items": [{"statistics": {"viewCount": "123", "likeCount": "5"}}]})
        assert request.url.params["startDate"] == "2026-09-01"
        return httpx.Response(200, json={"columnHeaders": [{"name": name} for name in ["estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage", "shares"]], "rows": [[7.5, 18, 60, 4]]})
    result = YouTubePublisher(client(handler)).analytics("vid", {"access_token": "TOKEN"}, "2026-09-01", "2026-09-14")
    assert result["watch_time_seconds"] == 450
    assert result["average_view_duration"] == 18
    assert result["average_view_percentage"] == 60 and result["shares"] == 4
    assert result["views"] == 123


def _png_color(color):
    import struct
    import zlib
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 96, 8, 2, 0, 0, 0))
    return png + chunk(b"IDAT", zlib.compress((b"\x00" + bytes(color) * 64) * 96)) + chunk(b"IEND", b"")


def test_image_motion_two_descriptions_create_distinct_ordered_scenes_and_resume(tmp_path):
    import base64
    import subprocess
    from factory.providers.ai import ImageMotionVideoProvider
    saved, requests = {}, []
    def save(cp):
        saved.clear()
        saved.update(json.loads(json.dumps(cp)))
    def handler(request):
        index = len(requests)
        requests.append(request)
        assert saved["scenes"][index]["image_started"] is True
        prompt = json.loads(request.content)["prompt"]
        assert ("Red desert" if index == 0 else "Blue ocean") in prompt
        color = (200, 25, 20) if index == 0 else (20, 30, 200)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(_png_color(color)).decode()}],
                                        "usage": {"input_tokens": 12, "output_tokens": 15}})
    provider = ImageMotionVideoProvider(OpenAIImageProvider("KEY", "image-model", client(handler)))
    path = tmp_path / "story.mp4"
    options = {"width": 64, "height": 96, "duration": 4, "save_checkpoint": save,
               "scenes": [{"description": "Red desert", "duration": 2}, {"description": "Blue ocean", "duration": 2}]}
    result = provider.generate("Travel story", str(path), options)
    assert result["status"] == "COMPLETED" and len(requests) == 2
    assert result["usage"]["input_tokens"] == 24 and result["usage"]["output_tokens"] == 30
    assert result["usage"]["requests"] == result["usage"]["images"] == 2
    assert all(scene["complete"] for scene in saved["scenes"])
    def sample(seconds):
        frame = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(seconds), "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-threads", "1", "pipe:1"], capture_output=True, check=True, timeout=20)
        return frame.stdout[:3]
    first, second = sample(0.5), sample(3.5)
    assert first[0] > first[2] + 100 and second[2] > second[0] + 100
    assert provider.generate("Travel story", str(path), {**options, "checkpoint": saved})["status"] == "COMPLETED"
    assert len(requests) == 2


def test_multiscene_second_image_unknown_never_repeats_first_or_second(tmp_path):
    import base64
    from factory.providers.ai import ImageMotionVideoProvider
    saved, requests = {}, []
    def save(cp):
        saved.clear()
        saved.update(json.loads(json.dumps(cp)))
    def handler(request):
        requests.append(request)
        if len(requests) == 2:
            raise httpx.ReadTimeout("lost second image", request=request)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(_png_color((20, 30, 180))).decode()}]})
    provider = ImageMotionVideoProvider(OpenAIImageProvider("KEY", "image-model", client(handler)))
    options = {"width": 64, "height": 96, "duration": 2, "save_checkpoint": save,
               "scenes": [{"description": "First", "duration": 1}, {"description": "Second", "duration": 1}]}
    output = str(tmp_path / "unknown.mp4")
    assert provider.generate("story", output, options)["status"] == "UNKNOWN"
    assert saved["scenes"][0]["complete"] and saved["scenes"][1]["image_started"]
    assert provider.generate("story", output, {**options, "checkpoint": saved})["status"] == "UNKNOWN"
    assert len(requests) == 2


def test_multiscene_image_count_cap_before_paid_request(tmp_path):
    from factory.providers.ai import ImageMotionVideoProvider
    provider = ImageMotionVideoProvider(OpenAIImageProvider("KEY", "model", client(lambda r: pytest.fail("No paid call expected"))))
    with pytest.raises(ProviderError) as exc:
        provider.generate("story", str(tmp_path / "many.mp4"), {"duration": 30,
            "scenes": [{"description": str(i), "duration": 1} for i in range(13)], "save_checkpoint": lambda _: None})
    assert exc.value.code == "invalid_scene_plan"
