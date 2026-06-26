from core.ledger import EventType
from core.strategy import parse_recommendation_output
from utilities.ImageStorage import ImageStorage


def test_recommendation_parser_normalises_order_plan():
    rec = parse_recommendation_output(
        '{'
        '"recommendation":"BUY",'
        '"confidence":0.74,'
        '"market_classification":{"long_term_regime":"bull_channel"},'
        '"latest_dynamics":{"preferred_direction":"buy"},'
        '"action_plan":{"order_plan":[{"side":"buy","allocation_fraction":0.1},{"side":"buy","allocation_pct":20}]}'
        '}'
    )

    assert rec.status == "BUY"
    assert rec.confidence == 0.74
    assert rec.action_plan["total_allocation_pct"] == 30
    assert rec.action_plan["order_plan"][0]["leg_id"] == "L1"
    assert rec.action_plan["order_plan"][0]["allocation_pct"] == 10


def test_recommendation_event_type_is_available():
    assert EventType.RECOMMENDATION.value == "RECOMMENDATION"


def test_advice_json_upload_uses_timestamp_folder_and_symbol_filename():
    class FakeStorage(ImageStorage):
        def _upload_json_to_gcs(self, payload, blob_name):
            self.seen_payload = payload
            self.seen_blob_name = blob_name
            return {
                "gcs_uri": f"gs://test-bucket/{blob_name}",
                "web_url": f"https://storage.googleapis.com/test-bucket/{blob_name}",
            }

    storage = FakeStorage(provider="gcs", bucket_name="test-bucket")
    ref = storage.put_json_entry({"recommendation": {"status": "HOLD"}}, "EUR/USD", 202606241630).ledger_ref

    assert storage.seen_blob_name == "advice/202606241630/EUR_USD.json"
    assert storage.seen_payload["recommendation"]["status"] == "HOLD"
    assert ref["uploaded"] is True
    assert ref["gcs_uri"] == "gs://test-bucket/advice/202606241630/EUR_USD.json"


def test_recommendation_parser_exposes_invalid_json_debug():
    rec = parse_recommendation_output("not json at all")

    assert rec.status == "ERROR"
    assert rec.error == "empty or invalid advisory JSON"
    assert rec.debug["stage"] == "extract_json"
    assert rec.debug["json_found"] is False
    assert "raw_preview" in rec.debug
    assert "json_error" in rec.debug


def test_recommendation_parser_accepts_status_alias():
    rec = parse_recommendation_output('{"status":"HOLD","confidence":0.12,"action_plan":{}}')

    assert rec.status == "HOLD"
    assert rec.confidence == 0.12


def test_json_blob_upload_uses_static_pointer_path():
    class FakeStorage(ImageStorage):
        def _upload_json_to_gcs(self, payload, blob_name):
            self.seen_payload = payload
            self.seen_blob_name = blob_name
            return {
                "gcs_uri": f"gs://test-bucket/{blob_name}",
                "web_url": f"https://storage.googleapis.com/test-bucket/{blob_name}",
            }

    storage = FakeStorage(provider="gcs", bucket_name="test-bucket")
    ref = storage.put_json_blob({"uid": 202606241630}, "advice/latest").ledger_ref

    assert storage.seen_blob_name == "advice/latest.json"
    assert storage.seen_payload["uid"] == 202606241630
    assert ref["gcs_uri"] == "gs://test-bucket/advice/latest.json"


def test_latest_pointer_manifest_contains_processed_artifact_urls():
    from datetime import datetime, timezone

    from app import AlphardApp

    class FakeStorage(ImageStorage):
        def __init__(self):
            super().__init__(provider="gcs", bucket_name="test-bucket")
            self.uploads = []

        def _upload_json_to_gcs(self, payload, blob_name):
            self.uploads.append((blob_name, payload))
            return {
                "gcs_uri": f"gs://test-bucket/{blob_name}",
                "web_url": f"https://storage.googleapis.com/test-bucket/{blob_name}",
            }

    class FakeLedger:
        def __init__(self):
            self.events = []

        def log(self, event_type, symbol, uid, strategy, data, timeframe=None):
            self.events.append((event_type, symbol, uid, strategy, data, timeframe))

    app = AlphardApp.__new__(AlphardApp)
    app.artifact_storage = FakeStorage()
    app.ledger = FakeLedger()
    app.publish_latest_advice_pointer(
        app_now=datetime(2026, 6, 24, 16, 30, tzinfo=timezone.utc),
        results=[
            {
                "symbol": "GBPUSD",
                "uid": 202606241731,
                "processed": True,
                "status": "PROCESSED",
                "recommendation": "BUY",
                "confidence": 0.65,
                "artifact": {
                    "advice": {"gcs_uri": "gs://test-bucket/advice/202606241731/GBPUSD.json"},
                    "charts": {
                        "global": {"gcs_uri": "gs://test-bucket/charts/GBPUSD/global.png"},
                        "detail": {"gcs_uri": "gs://test-bucket/charts/GBPUSD/detail.png"},
                    },
                },
            }
        ],
    )

    assert [name for name, _ in app.artifact_storage.uploads] == [
        "advice/202606241731/manifest.json",
        "advice/latest.json",
    ]
    manifest = app.artifact_storage.uploads[0][1]
    pointer = app.artifact_storage.uploads[1][1]
    assert manifest["uid"] == 202606241731
    assert manifest["results"][0]["artifact"]["advice"]["gcs_uri"].endswith("/GBPUSD.json")
    assert manifest["results"][0]["artifact"]["charts"]["global"]["gcs_uri"].endswith("global.png")
    assert pointer["manifest"]["gcs_uri"] == "gs://test-bucket/advice/202606241731/manifest.json"
