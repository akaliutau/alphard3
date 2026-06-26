from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any



def encode_image_to_data_url(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".") or "png"
    mime = "image/png" if suffix == "png" else f"image/{suffix}"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


@dataclass(frozen=True)
class ImageRef:
    prompt_part: dict[str, Any]
    ledger_ref: dict[str, Any]


@dataclass(frozen=True)
class JsonRef:
    ledger_ref: dict[str, Any]


class ImageStorage:
    def __init__(self, provider: str = "local", bucket_name: str | None = None, public_read: bool = False):
        self.provider = provider
        self.bucket_name = bucket_name
        self.public_read = public_read

    def get_image_entry(self, image_path: Path, symbol: str, uid: int) -> ImageRef:
        if self.provider == "local":
            url = encode_image_to_data_url(image_path)
            return ImageRef(
                prompt_part={"type": "image_url", "image_url": {"url": url}},
                ledger_ref={"provider": "local", "path": str(image_path)},
            )

        if self.provider == "gcs":
            if not self.bucket_name:
                raise ValueError("GCS image provider requires GCS_BUCKET_NAME/BUCKET_NAME")
            blob_name = f"charts/{symbol}/{image_path.name}"
            refs = self._upload_to_gcs(image_path, blob_name)
            # LiteLLM/OpenAI-compatible multimodal messages use image_url. Public HTTPS is most portable.
            prompt_url = refs["web_url"] if self.public_read else refs["gcs_uri"]
            return ImageRef(
                prompt_part={"type": "image_url", "image_url": {"url": prompt_url}},
                ledger_ref={"provider": "gcs", **refs},
            )

        raise ValueError(f"Unsupported IMAGE_PROVIDER={self.provider!r}")

    def put_json_entry(
        self,
        payload: dict[str, Any],
        symbol: str,
        uid: int,
        *,
        prefix: str = "advice",
    ) -> JsonRef:
        """Persist one symbol's advisory JSON beside chart artifacts.

        GCS layout is timestamp-first so all symbol advice for a basket is
        grouped together while avoiding multi-symbol overwrite:

            gs://<bucket>/advice/<uid>/<symbol>.json
        """
        safe_symbol = _safe_blob_component(symbol)
        blob_name = f"{prefix.strip('/')}/{uid}/{safe_symbol}.json"
        return self.put_json_blob(payload, blob_name)

    def put_json_blob(self, payload: dict[str, Any], blob_name: str) -> JsonRef:
        """Persist JSON at an explicit blob path.

        Used for slot manifests and the stable latest pointer, where callers need
        deterministic names such as ``advice/<uid>/manifest.json`` and
        ``advice/latest.json``.
        """
        blob_name = blob_name.strip("/")
        if not blob_name:
            raise ValueError("JSON blob name must not be empty")
        if not blob_name.endswith(".json"):
            blob_name = f"{blob_name}.json"

        if self.provider == "local":
            return JsonRef(
                ledger_ref={
                    "provider": "local",
                    "uploaded": False,
                    "blob_name": blob_name,
                    "reason": "IMAGE_PROVIDER is local; GCS JSON upload skipped",
                }
            )

        if self.provider == "gcs":
            if not self.bucket_name:
                raise ValueError("GCS JSON upload requires GCS_BUCKET_NAME/BUCKET_NAME")
            refs = self._upload_json_to_gcs(payload, blob_name)
            return JsonRef(ledger_ref={"provider": "gcs", "uploaded": True, **refs})

        raise ValueError(f"Unsupported IMAGE_PROVIDER={self.provider!r}")

    def _upload_to_gcs(self, image_path: Path, blob_name: str) -> dict[str, str]:
        from google.cloud import storage
        client = storage.Client()
        bucket = client.bucket(self.bucket_name)
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(str(image_path), content_type="image/png")
        return {
            "gcs_uri": f"gs://{self.bucket_name}/{blob_name}",
            "web_url": f"https://storage.googleapis.com/{self.bucket_name}/{blob_name}",
        }

    def _upload_json_to_gcs(self, payload: dict[str, Any], blob_name: str) -> dict[str, str]:
        from google.cloud import storage
        client = storage.Client()
        bucket = client.bucket(self.bucket_name)
        blob = bucket.blob(blob_name)
        blob.upload_from_string(
            json.dumps(payload, default=str, ensure_ascii=False, indent=2, sort_keys=True),
            content_type="application/json; charset=utf-8",
        )
        return {
            "gcs_uri": f"gs://{self.bucket_name}/{blob_name}",
            "web_url": f"https://storage.googleapis.com/{self.bucket_name}/{blob_name}",
        }


def _safe_blob_component(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value.strip()) or "unknown"


