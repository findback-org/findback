from copy import deepcopy

from findback.showcase_setup import canonical_browser_origin, normalize_room_urls


def test_private_room_deep_links_become_browser_links_on_configured_origin():
    config = {
        "mattermost_url": "http://127.0.0.1:57898/",
        "staff_room": {"id": "staff", "url": "mattermost://localhost:57898/switch/channels/review"},
        "cases": {
            "C001": {"room_id": "priya", "url": "mattermost://localhost:57898/switch/channels/priya"},
            "C002": {"room_id": "alex", "url": "http://localhost:57898/switch/channels/alex"},
        },
    }
    result = normalize_room_urls(deepcopy(config))
    assert result["staff_room"]["url"] == "http://127.0.0.1:57898/switch/channels/review"
    assert result["cases"]["C001"]["url"] == "http://127.0.0.1:57898/switch/channels/priya"
    assert result["cases"]["C002"]["url"] == "http://127.0.0.1:57898/switch/channels/alex"
    assert result["cases"]["C001"]["room_id"] == "priya"
    assert normalize_room_urls(deepcopy(result)) == result


def test_canonical_browser_site_origin_covers_community_without_changing_api():
    config = {"mattermost_url": "http://127.0.0.1:57898", "staff_room": {"url": "mattermost://localhost:57898/switch/channels/staff"},
              "cases": {"C001": {"url": "http://127.0.0.1:57898/switch/channels/priya"}},
              "showcase": {"community_room": {"url": "http://127.0.0.1:57898/switch/channels/campus"}}}
    result = normalize_room_urls(config, "http://localhost:57898")
    assert result["mattermost_url"] == "http://127.0.0.1:57898"
    assert result["showcase"]["browser_origin"] == "http://localhost:57898"
    for room in [result["staff_room"], result["cases"]["C001"], result["showcase"]["community_room"]]:
        assert room["url"].startswith("http://localhost:57898/switch/channels/")
    assert normalize_room_urls(deepcopy(result)) == result


def test_browser_origin_does_not_adopt_a_different_server():
    api = "http://127.0.0.1:57898"
    for site in ["https://example.com:57898", "http://localhost:57899", "http://localhost.evil.test:57898", "http://localhost:invalid"]:
        assert canonical_browser_origin(api, site) == api
