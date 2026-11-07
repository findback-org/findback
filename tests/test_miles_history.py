"""Guardrails for GET-only retrieval of one approved browser-generated story."""
import hashlib
import importlib.util
import json
from pathlib import Path

import httpx
import pytest


spec = importlib.util.spec_from_file_location("miles_history", Path(__file__).parents[1] / "scripts/download_miles_history.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def item(text="Alex’s case.", date=200, ident="new-story"):
    a = {"characters": list(text), "character_start_times_seconds": [0] * len(text),
         "character_end_times_seconds": [1] * len(text)}
    return {"voice_id": mod.VOICE_ID, "text": text, "date_unix": date, "history_item_id": ident,
            "model_id": "eleven_multilingual_v2", "voice_name": mod.VOICE_NAME,
            "alignments": {"alignment": a, "normalized_alignment": a}}


def source(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    text = "Alex’s case.\n"
    (src / "narration-script.txt").write_text(text)
    (src / "story-plan.json").write_text(json.dumps({"status": "ROOT_SCRIPT_APPROVED", "script_sha256": hashlib.sha256(text.encode()).hexdigest()}))
    return src


def test_only_quotes_and_spacing_normalize():
    assert mod.normalize_text('Alex’s  case.\n') == mod.normalize_text("Alex's case.")
    assert mod.normalize_text("Alex's cases.") != mod.normalize_text("Alex's case.")


def test_strict_voice_text_and_generation_window():
    assert mod.is_match(item(), "Alex's case.", 200, 201)
    assert not mod.is_match(item(date=199), "Alex's case.", 200, None)
    assert not mod.is_match(item(date=201), "Alex's case.", 200, 201)
    assert not mod.is_match({**item(), "voice_id": "other"}, "Alex's case.", 200, None)
    assert not mod.is_match(item(text="Old test."), "Alex's case.", 200, None)


def test_selects_newest_exact_but_refuses_tied_history():
    assert mod.select_latest([item(date=199), item(date=201, ident="latest")], "Alex’s case.", 200, None)["history_item_id"] == "latest"
    with pytest.raises(mod.HistoryError, match="same timestamp"):
        mod.select_latest([item(), item(ident="duplicate")], "Alex’s case.", 200, None)


def test_success_downloads_only_validated_audio_and_preserves_alignment(tmp_path):
    src = source(tmp_path)
    requests = []
    detail = item()
    def handler(request):
        requests.append((request.method, request.url.path))
        assert request.url.host == "api.elevenlabs.io"
        if request.url.path == "/v1/history":
            return httpx.Response(200, json={"history": [item(text="Old test."), detail], "has_more": False})
        if request.url.path.endswith("/audio"):
            return httpx.Response(200, content=b"audio" * 300, headers={"content-type": "audio/mpeg"})
        return httpx.Response(200, json=detail)
    out = tmp_path / "out"
    (out / "independent-qa").mkdir(parents=True)
    (out / "independent-qa/preflight.json").write_text('{}')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = mod.download(client, source=src, out=out, not_before=200)
    assert requests == [("GET", "/v1/history"), ("GET", "/v1/history/new-story"), ("GET", "/v1/history/new-story/audio")]
    assert result["downloader_generated_speech"] is False
    assert json.loads((out / "global-alignment.json").read_text()) == detail["alignments"]
    assert (out / "narration-script.txt").read_bytes() == (src / "narration-script.txt").read_bytes()


@pytest.mark.parametrize("change", [{"voice_id": "wrong"}, {"text": "Unrelated story."}, {"date_unix": 100},
                                    {"model_id": "unexpected"}, {"alignments": None}])
def test_detail_revalidation_blocks_audio_request(tmp_path, change):
    src = source(tmp_path)
    paths = []
    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={**item(), **change})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(mod.HistoryError):
            mod.download(client, source=src, out=tmp_path / "out", not_before=200, history_id="new-story")
    assert paths == ["/v1/history/new-story"]
    assert not (tmp_path / "out").exists()


def test_http_error_does_not_expose_headers_or_body(tmp_path):
    src = source(tmp_path)
    def handler(request):
        return httpx.Response(401, json={"secret": "PRIVATE-CREDENTIAL"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(mod.HistoryError) as caught:
            mod.download(client, source=src, out=tmp_path / "out", not_before=200)
    assert "401" in str(caught.value)
    assert "PRIVATE-CREDENTIAL" not in str(caught.value)


def test_duration_fields_only_converted_after_independent_delta_proof():
    a = {"alignment": {"characters": list('abc'), "character_start_times_seconds": [0, 0.1, 0.3],
                       "character_end_times_seconds": [0.1, 0.2, 0.2]}}
    converted, evidence = mod.standardize_alignment(a)
    assert converted['alignment']['character_end_times_seconds'] == [0.1, 0.30000000000000004, 0.5]
    assert a['alignment']['character_end_times_seconds'] == [0.1, 0.2, 0.2]
    assert evidence['alignment']['independent_next_start_deltas_checked'] == 2
    a['alignment']['character_end_times_seconds'][0] = 0.05
    with pytest.raises(mod.HistoryError, match='neither valid'):
        mod.standardize_alignment(a)
