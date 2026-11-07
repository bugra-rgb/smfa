"""Real FFmpeg integration tests; never substitute text files for media."""

import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave
import zlib

from factory.media.engine import MAX_MEDIA_BYTES, MediaError, _input_args, extract_audio, generate_mock_video, inspect_media, quality_check, render_video, write_subtitles


def write_png(path: Path, transparent: bool = False) -> None:
    def chunk(kind: bytes, content: bytes) -> bytes:
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content) & 0xFFFFFFFF)
    pixels = b"".join(b"\x00" + bytes((255, 255, 255, 0 if transparent else 255)) * 32 for _ in range(24))
    header = struct.pack(">IIBBBBB", 32, 24, 8, 6, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg kurulumu gerekiyor")
class MediaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="factory-media-test-")
        cls.directory = Path(cls.temporary.name)
        cls.source = cls.directory / "source.mp4"
        generate_mock_video(cls.source, duration=2, width=160, height=240)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def settings(self, **extra):
        return {"width": 160, "height": 240, "duration": 2, **extra}

    def test_real_render_with_visible_logo_and_timed_subtitles(self):
        logo = self.directory / "logo.png"
        write_png(logo)
        subtitle = self.directory / "timed.srt"
        timing = write_subtitles("", 2, subtitle, [{"start": .1, "end": 1.9, "text": "Türkçe altyazı denemesi"}])
        self.assertEqual(timing["timing"], "provider_timed")
        options = self.settings(logo_path=str(logo), branding={"position": "top-right", "width": 32, "margin": 8},
                                subtitle={"enabled": True, "font_size": 14})
        output = self.directory / "branded.mp4"
        result = render_video(self.source, output, options, subtitle_path=subtitle)
        self.assertTrue(result["manifest"]["logo"]["visible_effect"])
        self.assertTrue(result["manifest"]["subtitles"]["visible_effect"])
        qa = quality_check(output, options)
        self.assertTrue(qa["passed"], qa)
        self.assertTrue(qa["checks"]["full_decode"]["passed"])
        self.assertFalse(result["manifest"]["logo"]["semantic_identity_verified"])

    def test_transparent_logo_is_not_claimed_visible(self):
        logo = self.directory / "transparent.png"
        write_png(logo, transparent=True)
        options = self.settings(logo_path=str(logo), branding={"width": 32})
        output = self.directory / "transparent-logo.mp4"
        render_video(self.source, output, options)
        qa = quality_check(output, options)
        self.assertFalse(qa["passed"])
        self.assertIn("logo_visible_render", qa["errors"])

    def test_missing_logo_provenance_and_wrong_resolution_fail(self):
        qa = quality_check(self.source, self.settings(width=320, logo_path="missing.png"))
        self.assertFalse(qa["passed"])
        self.assertIn("resolution", qa["errors"])
        self.assertIn("logo_visible_render", qa["errors"])

    def test_corrupt_video_fails_actual_decode(self):
        broken = self.directory / "corrupt.mp4"
        broken.write_bytes(self.source.read_bytes()[:500])
        qa = quality_check(broken, self.settings())
        self.assertFalse(qa["passed"])
        self.assertIn("media_readable", qa["errors"])

    def test_frame_payload_corruption_is_rejected_despite_valid_container(self):
        broken = self.directory / "corrupt-frames.mp4"
        content = bytearray(self.source.read_bytes())
        position = 0
        while position + 8 < len(content):
            size = struct.unpack(">I", content[position:position + 4])[0]
            kind = content[position + 4:position + 8]
            if kind == b"mdat":
                start = position + 8
                content[start:start + min(10000, size - 8)] = b"\x00" * min(10000, size - 8)
                break
            if size < 8:
                self.fail("Geçerli MP4 kutuları bekleniyordu.")
            position += size
        else:
            self.fail("Kaynak dosyada mdat bulunamadı.")
        broken.write_bytes(content)
        qa = quality_check(broken, self.settings())
        self.assertTrue(qa["checks"]["video_stream"]["passed"], qa)
        self.assertFalse(qa["passed"], qa)
        self.assertIn("media_readable", qa["errors"])

    def test_trim_speed_and_source_duration_gate(self):
        output = self.directory / "trimmed.mp4"
        options = self.settings(duration=1, speed=1.5, trim_start=.25)
        render_video(self.source, output, options)
        qa = quality_check(output, options)
        self.assertTrue(qa["passed"], qa)
        with self.assertRaisesRegex(MediaError, "Kaynak video kısa"):
            render_video(self.source, output, self.settings(duration=10))

    def test_safe_inspection_and_compressed_transcription_audio(self):
        info = inspect_media(self.source)
        self.assertTrue(info["has_audio"])
        self.assertEqual((info["width"], info["height"]), (160, 240))
        audio = self.directory / "transcription.m4a"
        extracted = extract_audio(self.source, audio, duration=1, trim_start=.25, speed=1.5)
        self.assertAlmostEqual(extracted["duration"], 1, delta=.1)
        self.assertEqual(extracted["sample_rate"], 16000)
        self.assertFalse(inspect_media(audio)["has_video"])
        self.assertLess(audio.stat().st_size, 25 * 1024 * 1024)

    def test_voice_pitch_changes_frequency_while_preserving_duration(self):
        voice = self.directory / "pitch-input.wav"
        samples = b"".join(struct.pack("<h", round(8000 * math.sin(2 * math.pi * 440 * index / 16000))) for index in range(32000))
        with wave.open(str(voice), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(samples)
        output = self.directory / "pitched.mp4"
        options = self.settings(original_audio_volume=0, voice={"pitch": 12})
        render_video(self.source, output, options, voice_path=voice)
        self.assertAlmostEqual(inspect_media(output)["duration"], 2, delta=.1)
        decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-vn", "-ss", "0.25", "-t", "1.5",
                                  "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"], check=True, capture_output=True, timeout=30).stdout
        waveform = [value[0] for value in struct.iter_unpack("<h", decoded)]
        crossings = sum(a <= 0 < b for a, b in zip(waveform, waveform[1:]))
        frequency = crossings / (len(waveform) / 16000)
        self.assertAlmostEqual(frequency, 880, delta=25)

    def test_intro_outro_crossfade_and_music(self):
        music = self.directory / "music.wav"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=880:sample_rate=48000", "-t", "0.5", str(music)], check=True, capture_output=True, timeout=30)
        options = self.settings(intro_path=str(self.source), outro_path=str(self.source),
                                music_path=str(music), transition_seconds=.2)
        output = self.directory / "combined.mp4"
        result = render_video(self.source, output, options, voice_path=music)
        self.assertAlmostEqual(result["duration"], 5.6, places=1)
        qa = quality_check(output, options)
        self.assertTrue(qa["passed"], qa)

    def test_black_silent_clip_rejected(self):
        black = self.directory / "black.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=black:s=160x240:r=30", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                        "-t", "2", "-c:v", "libx264", "-c:a", "aac", "-threads", "2", str(black)],
                       check=True, capture_output=True, timeout=30)
        qa = quality_check(black, self.settings())
        self.assertFalse(qa["passed"])
        self.assertIn("black_screen", qa["errors"])
        self.assertIn("audio_loudness", qa["errors"])
        json.dumps(qa, allow_nan=False)
        optional = quality_check(black, self.settings(qa={"require_audio": False}))
        self.assertIn("black_screen", optional["errors"])
        self.assertTrue(optional["checks"]["audio_loudness"]["passed"])
        self.assertTrue(optional["checks"]["audio_loudness"]["skipped"])
        json.dumps(optional, allow_nan=False)

    def test_text_and_filenames_cannot_be_filter_or_shell_expressions(self):
        source = self.directory / "quoted'$(touch SHOULD_NOT_EXIST).mp4"
        shutil.copyfile(self.source, source)
        output = self.directory / "safe-text.mp4"
        render_video(source, output, self.settings(text_overlay="100% %{system:touch SHOULD_NOT_EXIST}"))
        self.assertTrue(output.is_file())
        self.assertFalse((self.directory / "SHOULD_NOT_EXIST").exists())

    def test_manifest_cannot_attest_to_changed_artifact(self):
        output = self.directory / "tampered.mp4"
        render_video(self.source, output, self.settings())
        manifest_path = output.with_suffix(".mp4.render.json")
        manifest = json.loads(manifest_path.read_text())
        manifest["output_sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest))
        qa = quality_check(output, self.settings())
        self.assertFalse(qa["passed"])
        self.assertIn("render_provenance", qa["errors"])


class SubtitleTests(unittest.TestCase):
    def test_interpolation_is_labeled_and_invalid_segments_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "subtitle.srt"
            result = write_subtitles("Bir iki üç dört beş altı yedi sekiz", 4, output)
            self.assertEqual(result["timing"], "estimated_word_distribution")
            self.assertIn("00:00:04,000", output.read_text())
            with self.assertRaises(MediaError):
                write_subtitles("", 4, output, [{"start": 2, "end": 1, "text": "Geçersiz"}])
            with self.assertRaises(MediaError):
                write_subtitles("", 4, output, [{"start": 0, "end": 3, "text": "Bir"},
                                                {"start": 2, "end": 4, "text": "İki"}])


class MediaSecurityTests(unittest.TestCase):
    def test_renamed_playlists_and_svg_are_rejected_before_subprocess(self):
        malicious = ["#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nhttp://127.0.0.1/private\n",
                     "ffconcat version 1.0\nfile '/etc/passwd'\n",
                     '<svg xmlns="http://www.w3.org/2000/svg"><image href="file:///etc/passwd"/></svg>']
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "disguised.mp4"
            for payload in malicious:
                path.write_text(payload)
                with patch("factory.media.engine.subprocess.run", side_effect=AssertionError("FFmpeg must not start")):
                    with self.assertRaisesRegex(MediaError, "Medya imzası"):
                        render_video(path, Path(temporary) / "output.mp4", {"width": 160, "height": 240})

    def test_file_and_output_resource_caps_are_checked_before_processing(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "large.mp4"
            with path.open("wb") as stream:
                stream.truncate(MAX_MEDIA_BYTES + 1)
            with patch("factory.media.engine.subprocess.run", side_effect=AssertionError("FFmpeg must not start")):
                with self.assertRaisesRegex(MediaError, "512 MiB"):
                    _input_args(path)
                with self.assertRaisesRegex(MediaError, "Çıktı çözünürlüğü"):
                    generate_mock_video(Path(temporary) / "output.mp4", width=3840, height=3840)

    def test_mov_external_references_are_disabled_and_demuxer_is_forced(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "container.mp4"
            path.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 24)
            options = _input_args(path)
            self.assertEqual(options[options.index("-f") + 1], "mov")
            self.assertEqual(options[options.index("-protocol_whitelist") + 1], "file")
            self.assertEqual(options[options.index("-enable_drefs") + 1], "0")
            self.assertEqual(options[options.index("-use_absolute_path") + 1], "0")


if __name__ == "__main__":
    unittest.main()
