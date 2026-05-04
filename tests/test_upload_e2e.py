"""End-to-end tests for Governor Chat file upload.

Runs against a local autopilot (localhost:8001) with Playwright
controlling a local dapp frontend (localhost:8082 or any static server).
"""
import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests
from truesight_dao_client import generate_keypair
from truesight_dao_client.edgar_client import load_private_key
from PIL import Image
from playwright.sync_api import Browser, Page, sync_playwright

API_BASE = os.environ.get("CHAT_API_URL", "http://localhost:8001")
DAPP_URL = os.environ.get("DAPP_URL", "http://127.0.0.1:8082")


def _ensure_test_image() -> Path:
    """Create a small QR-code test image in /tmp."""
    img_path = Path("/tmp/test_qr.png")
    if img_path.exists():
        return img_path
    img = Image.new("RGB", (200, 200), "white")
    for x in range(0, 200, 20):
        for y in range(0, 200, 20):
            if (x // 20 + y // 20) % 2 == 0:
                for dx in range(10):
                    for dy in range(10):
                        if 0 <= x + dx < 200 and 0 <= y + dy < 200:
                            img.putpixel((x + dx, y + dy), (0, 0, 0))
    img.save(img_path)
    return img_path


def _ensure_signature_keys() -> tuple[str, str]:
    """Return (public_spki_b64, private_pkcs8_b64) using dao_client."""
    keys_path = Path("/tmp/test_governor_keys.json")
    if keys_path.exists():
        data = json.loads(keys_path.read_text())
        return data["public_key"], data["private_key"]
    pub, priv = generate_keypair()
    data = {"public_key": pub, "private_key": priv}
    keys_path.write_text(json.dumps(data))
    return pub, priv


# ─── Backend-only tests ────────────────────────────────────────────


class TestBackendUpload:
    def test_health(self):
        r = requests.get(f"{API_BASE}/health", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"

    def test_upload_auth_required(self):
        """Upload without auth must return 401."""
        r = requests.post(
            f"{API_BASE}/chat/upload",
            files={"file": ("test.txt", b"hello", "text/plain")},
            timeout=10,
        )
        assert r.status_code == 401

    def test_upload_with_auth(self):
        """Upload with a real RSA-signed payload returns SSE stream."""
        pub, priv = _ensure_signature_keys()
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        payload = {
            "message": "what is this image?",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "nonce": f"test-nonce-001-{time.time_ns()}",
        }
        payload_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        private_key = load_private_key(priv)
        sig = private_key.sign(payload_str.encode(), padding.PKCS1v15(), hashes.SHA256())
        signature = base64.b64encode(sig).decode()

        img = _ensure_test_image()
        with open(img, "rb") as f:
            r = requests.post(
                f"{API_BASE}/chat/upload",
                files={"file": ("test_qr.png", f, "image/png")},
                headers={
                    "X-Public-Key": pub,
                    "X-Payload": payload_str,
                    "X-Signature": signature,
                },
                timeout=30,
            )
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text[:200]}"
        assert r.headers.get("content-type", "").startswith("text/event-stream")
        body = r.text
        assert "data:" in body, "Expected SSE data events"
        # Should have a done event with a response from the LLM
        assert '"done"' in body, f"No done event found: {body[:300]}"
        assert '"response"' in body, "Expected response in done event"

    def test_upload_no_file(self):
        """Upload without a file but with a message — should still work."""
        pub, priv = _ensure_signature_keys()
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        payload = {
            "message": "hello without file",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "nonce": f"test-nonce-002-{time.time_ns()}",
        }
        payload_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        private_key = load_private_key(priv)
        sig = private_key.sign(payload_str.encode(), padding.PKCS1v15(), hashes.SHA256())
        signature = base64.b64encode(sig).decode()

        r = requests.post(
            f"{API_BASE}/chat/upload",
            files={"file": ("", b"", "application/octet-stream")},
            headers={
                "X-Public-Key": pub,
                "X-Payload": payload_str,
                "X-Signature": signature,
            },
            timeout=30,
        )
        assert r.status_code == 200

    def test_upload_heic_conversion(self):
        """HEIC files should be converted to JPEG on the backend."""
        pub, priv = _ensure_signature_keys()
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        payload = {
            "message": "what is in this heic?",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "nonce": f"test-nonce-003-{time.time_ns()}",
        }
        payload_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        private_key = load_private_key(priv)
        sig = private_key.sign(payload_str.encode(), padding.PKCS1v15(), hashes.SHA256())
        signature = base64.b64encode(sig).decode()

        img = _ensure_test_image()
        import shutil
        heic_path = Path("/tmp/test_upload.heic")
        shutil.copy(img, heic_path)

        with open(heic_path, "rb") as f:
            r = requests.post(
                f"{API_BASE}/chat/upload",
                files={"file": ("test.heic", f, "image/heic")},
                headers={
                    "X-Public-Key": pub,
                    "X-Payload": payload_str,
                    "X-Signature": signature,
                },
                timeout=30,
            )
        assert r.status_code == 200
        body = r.text
        assert "data:" in body, "Expected SSE data"
        assert '"done"' in body, "No done event"
        assert '"response"' in body, "Expected response"


# ─── Playwright e2e tests ─────────────────────────────────────────


@pytest.fixture(scope="module")
def governor_page(browser: Browser) -> Page:
    context = browser.new_context(
        storage_state=None,
        viewport={"width": 1280, "height": 900},
    )
    page = context.new_page()
    page.goto(DAPP_URL, wait_until="load")

    pub, priv = _ensure_signature_keys()
    page.evaluate("""(args) => {
        localStorage.setItem('publicKey', args[0]);
        localStorage.setItem('privateKey', args[1]);
        localStorage.setItem('governorChatApiUrl', args[2]);
    }""", [pub, priv, API_BASE])

    page.goto("about:blank", wait_until="domcontentloaded")
    page.goto(DAPP_URL, wait_until="domcontentloaded")
    # Wait for the greeting message to be rendered by the JS
    page.wait_for_function(
        "() => document.querySelectorAll('#chat-messages .message.bot').length >= 1",
        timeout=15000,
    )
    return page


class TestUploadE2E:
    def test_send_text_message(self, governor_page: Page):
        """A plain text message sends JSON and gets a response."""
        page = governor_page
        input_el = page.locator("#chat-input")
        send_btn = page.locator("#send-btn")

        input_el.fill("hello")
        send_btn.click()

        # Wait for the user message to appear, then eventually the bot response
        page.wait_for_selector("#chat-messages .message.user", timeout=5000)
        page.wait_for_selector("#chat-messages .message.bot:not(:first-child)", timeout=90000)

        messages = page.locator("#chat-messages .message")
        count = messages.count()
        assert count >= 2, f"Expected at least 2 messages (greeting + response), got {count}"
        last_text = messages.nth(count - 1).locator(".text").inner_text()
        assert len(last_text) > 0, "Bot response should not be empty"

    def test_send_image_upload(self, governor_page: Page):
        """Attach an image file and send with a message."""
        page = governor_page
        img = _ensure_test_image()

        # Click attach button
        page.locator("#attach-btn").click()
        # Upload via the hidden file input
        file_input = page.locator("#file-input")
        file_input.set_input_files(str(img))

        # Verify preview shows up
        preview = page.locator("#attachment-preview")
        page.wait_for_selector("#attachment-preview", timeout=3000)
        assert preview.is_visible()

        # Type a message
        page.locator("#chat-input").fill("what QR code is in this image?")
        page.locator("#send-btn").click()

        # Wait for bot response
        page.wait_for_selector("#chat-messages .message.user", timeout=5000)
        page.wait_for_selector("#chat-messages .message.bot:not(:first-child)", timeout=120000)

        messages = page.locator("#chat-messages .message")
        count = messages.count()
        assert count >= 3, f"Expected 3+ messages, got {count}"

        # The preview should disappear after sending
        preview_exists = page.locator("#attachment-preview").is_visible()
        assert not preview_exists, "Preview should hide after send"

    def test_cancel_attachment(self, governor_page: Page):
        """Attach a file then remove it before sending."""
        page = governor_page
        img = _ensure_test_image()

        page.locator("#attach-btn").click()
        file_input = page.locator("#file-input")
        file_input.set_input_files(str(img))

        preview = page.locator("#attachment-preview")
        page.wait_for_selector("#attachment-preview", timeout=3000)

        # Click the × to remove
        page.locator("#remove-attach").click()
        page.wait_for_timeout(500)
        assert not preview.is_visible()

        # Normal text send should still work
        page.locator("#chat-input").fill("hello again")
        page.locator("#send-btn").click()
        page.wait_for_selector("#chat-messages .message.user", timeout=5000)

    def test_send_without_file_and_without_message_blocked(self, governor_page: Page):
        """Send button should do nothing when both input and file are empty."""
        page = governor_page
        input_el = page.locator("#chat-input")
        send_btn = page.locator("#send-btn")

        # Clear everything
        input_el.fill("")

        initial_msgs = page.locator("#chat-messages .message").count()
        send_btn.click()
        page.wait_for_timeout(2000)
        after_msgs = page.locator("#chat-messages .message").count()
        assert after_msgs == initial_msgs, "No new messages should appear"
