"""Advertising fingerprint detection.

Pins the producer half of the ad-evidence layer: the API reads paid-ad tags
off the homepage markup and emits them on ``site.signals.advertising`` in the
shape RevCadence's ``ad_signals.classify`` consumes. These tests import only
``app.extract.signals`` (no crawl orchestrator), so they run anywhere.
"""
from app.extract.signals import detect_advertising, detect_landing_pages


def test_conversion_tag_and_pixel_score_and_ids():
    html = (
        '<script>gtag("config","AW-111222333")</script>'
        '<script src="https://connect.facebook.net/en_US/fbevents.js"></script>'
    )
    r = detect_advertising(html)
    assert r["score"] == 5  # conversion (3) + Meta pixel (2)
    assert "Google Ads" in r["platforms"] and "Meta" in r["platforms"]
    assert r["identifiers"]["google_conversion_id"] == "AW-111222333"


def test_meta_pixel_counts_once_even_with_two_fingerprints():
    html = (
        '<script src="https://connect.facebook.net/fbevents.js"></script>'
        '<script>fbq("init","999888777")</script>'
    )
    r = detect_advertising(html)
    assert r["score"] == 2
    assert r["evidence"].count("Meta: Meta Pixel") == 1
    assert r["identifiers"]["meta_pixel_id"] == "999888777"


def test_purchase_event_is_a_conversion_proof():
    r = detect_advertising('<script>fbq("track","Purchase")</script>')
    assert r["score"] == 3
    assert "Meta: purchase conversion event" in r["evidence"]


def test_attribution_software_is_supporting_weight_and_named_platform():
    r = detect_advertising('<script src="https://triplewhale.com/triplepixel.js"></script>')
    assert r["score"] == 1
    assert "Triple Whale" in r["platforms"]


def test_measurement_only_is_not_advertising():
    # Google Analytics and plain GTM are measurement, not spend. They must score 0.
    assert detect_advertising('<script src="https://www.googletagmanager.com/gtm.js"></script>')["score"] == 0
    assert detect_advertising('<script src="https://www.google-analytics.com/analytics.js"></script>')["score"] == 0


def test_clean_site_returns_the_full_empty_shape():
    r = detect_advertising("<html><body>a brochure</body></html>")
    assert r == {"score": 0, "platforms": [], "evidence": [], "identifiers": {}, "landing_pages": []}


def test_landing_pages_match_campaign_paths_only():
    urls = ["https://x.com/lp/spring", "https://x.com/about", "https://x.com/free-trial", "https://x.com/contact"]
    assert detect_landing_pages(urls) == ["https://x.com/lp/spring", "https://x.com/free-trial"]
