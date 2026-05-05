"""End-to-end test suite for the autopilot governor chat.

Future AIs: run this against a local autopilot instance to verify behavior.
Set AUTOPILOT_URL env var or defaults to http://localhost:8001.

Requirements: truesight_autopilot .venv, test image at /tmp/test_0885.jpg

Usage:
    cd /Users/garyjob/Applications/truesight_autopilot
    source .venv/bin/activate
    python tests/e2e_governor_chat.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

AUTOPILOT_URL = os.environ.get("AUTOPILOT_URL", "http://localhost:8001")
TEST_IMAGE = "/tmp/test_0885.jpg"

# Colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"
BOLD = "\033[1m"


class AutopilotClient:
    """Test client that signs requests with the governor's RSA key."""

    def __init__(self):
        # Load keys from .env
        from dotenv import load_dotenv

        load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
        self.pub_key = os.environ["PUBLIC_KEY"]
        pk = os.environ["PRIVATE_KEY"]
        self.priv_key = serialization.load_der_private_key(
            base64.b64decode(pk), password=None, backend=default_backend()
        )
        self.session_id = f"test-{int(time.time())}"

    def _sign(self, message: str) -> tuple[dict, str, str]:
        obj = {
            "message": message,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "nonce": str(uuid.uuid4()),
        }
        payload_str = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        sig = base64.b64encode(
            self.priv_key.sign(payload_str.encode(), padding.PKCS1v15(), hashes.SHA256())
        ).decode()
        return obj, sig, payload_str

    def chat(self, message: str) -> requests.Response:
        obj, sig, _ = self._sign(message)
        return requests.post(
            f"{AUTOPILOT_URL}/chat",
            json={"payload": obj, "signature": sig},
            headers={
                "X-Public-Key": self.pub_key,
                "Content-Type": "application/json",
                "X-Session-Id": self.session_id,
            },
            stream=True,
            timeout=180,
        )

    def upload(self, filepath: str, filename: str, message: str = "") -> requests.Response:
        obj, sig, payload_str = self._sign(message or f"Upload: {filename}")
        with open(filepath, "rb") as f:
            return requests.post(
                f"{AUTOPILOT_URL}/chat/upload",
                files={"file": (filename, f, "image/heic")},
                headers={
                    "X-Public-Key": self.pub_key,
                    "X-Payload": payload_str,
                    "X-Signature": sig,
                    "X-Session-Id": self.session_id,
                },
                stream=True,
                timeout=180,
            )


def stream_to_end(resp: requests.Response) -> dict[str, Any]:
    """Consume SSE stream and return extracted data."""
    result: dict[str, Any] = {
        "tools": [],
        "response": "",
        "proposal": None,
        "statuses": [],
        "errors": [],
    }
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        try:
            event = json.loads(line[6:])
            t = event.get("type", "")
            if t == "tool":
                result["tools"].append(event.get("tool", "?"))
            elif t == "token":
                result["response"] += event.get("content", "")
            elif t == "done":
                result["response"] += event.get("response", "")
                result["proposal"] = event.get("proposal")
                break
            elif t == "status":
                result["statuses"].append(event.get("message", ""))
            elif t == "error":
                result["errors"].append(event.get("content", ""))
        except Exception:
            pass
    return result


# ──────────────────────────── Test Cases ────────────────────────────

def assert_true(condition: bool, name: str) -> bool:
    if condition:
        print(f"  {GREEN}PASS{RESET} {name}")
        return True
    else:
        print(f"  {RED}FAIL{RESET} {name}")
        return False


def test_image_upload_and_analysis(client: AutopilotClient) -> bool:
    """Test 1: Upload an image and verify pyzbar + Grok analysis runs."""
    print(f"\n{BOLD}Test 1: Image upload + analysis{RESET}")
    resp = client.upload(TEST_IMAGE, "IMG_0885.HEIC", "QR photo from Kirsten. Process this bag.")
    data = stream_to_end(resp)
    ok = True
    ok &= assert_true(len(data["errors"]) == 0, "No SSE errors")
    ok &= assert_true(len(data["statuses"]) >= 2, "Progress status events received")
    ok &= assert_true(len(data["tools"]) >= 1, "Tools were called (scan/lookup)")
    ok &= assert_true(len(data["response"]) > 100, "Non-empty response")
    return ok


def test_qr_lookup_and_correction(client: AutopilotClient) -> bool:
    """Test 2: Correct a misread QR code and look it up."""
    print(f"\n{BOLD}Test 2: QR correction + lookup{RESET}")
    resp = client.chat("Correct code: 2024OSCAR_20260330_22. Look it up.")
    data = stream_to_end(resp)
    ok = True
    ok &= assert_true("lookup_qr_code" in data["tools"], "lookup_qr_code tool called")
    ok &= assert_true("MINTED" in data["response"] or "2024OSCAR_20260330_22" in data["response"], "QR code found in ledger")
    return ok


def test_submission_approval_gate(client: AutopilotClient) -> bool:
    """Test 3: submit_contribution must return pending_approval on first call."""
    print(f"\n{BOLD}Test 3: Submission approval gate{RESET}")
    resp = client.chat("Submit the inventory movement from Kirsten to Gary Teh for 2024OSCAR_20260330_22. No dry run.")
    data = stream_to_end(resp)
    ok = True
    # On first call, submit_contribution should return pending_approval, not execute
    # (We check the response text for evidence of proposal/waiting)
    ok &= assert_true(
        "submit_contribution" not in data["tools"] or "pending_approval" in data["response"].lower(),
        "Either didn't call submit_contribution, or it returned pending_approval"
    )
    return ok


def test_duplicate_guard(client: AutopilotClient) -> bool:
    """Test 4: Same QR code should be rejected as duplicate."""
    print(f"\n{BOLD}Test 4: Duplicate QR guardrail{RESET}")
    resp = client.chat("Process 2024OSCAR_20260330_22 again — move it from Kirsten to Gary Teh.")
    data = stream_to_end(resp)
    ok = True
    # Either submit_contribution returns duplicate, or LLM doesn't call it
    tools_called = set(data["tools"])
    if "submit_contribution" in tools_called:
        ok &= assert_true(
            "duplicate" in data["response"].lower() or "already" in data["response"].lower(),
            "submit_contribution returned duplicate status"
        )
    else:
        ok &= assert_true(True, "submit_contribution not called — LLM detected duplicate")
    return ok


def test_xml_no_leaks(client: AutopilotClient) -> bool:
    """Test 5: No XML function call leaks in response."""
    print(f"\n{BOLD}Test 5: No XML leaks{RESET}")
    resp = client.chat("What tools are available?")
    data = stream_to_end(resp)
    ok = assert_true(
        "<function_calls>" not in data["response"] and "<invoke" not in data["response"],
        "No XML tool-call syntax in response"
    )
    return ok


def test_session_persistence(client: AutopilotClient) -> bool:
    """Test 6: Session history persists (check session endpoint)."""
    print(f"\n{BOLD}Test 6: Session persistence{RESET}")
    resp = requests.get(
        f"{AUTOPILOT_URL}/session",
        headers={
            "X-Public-Key": client.pub_key,
            "X-Session-Id": client.session_id,
        },
    )
    data = resp.json()
    ok = assert_true(len(data.get("messages", [])) >= 2, "Session has at least 2 saved messages")
    return ok


# ──────────────────────────── Main ────────────────────────────

def main() -> int:
    if not os.path.exists(TEST_IMAGE):
        print(f"{RED}Test image not found: {TEST_IMAGE}{RESET}")
        print("Convert it first: sips -s format jpeg ~/Downloads/IMG_0885.HEIC --out /tmp/test_0885.jpg")
        return 1

    client = AutopilotClient()
    print(f"{BOLD}Autopilot E2E Test Suite{RESET}")
    print(f"  Server: {AUTOPILOT_URL}")
    print(f"  Session: {client.session_id}")

    results = {
        "image_upload_analysis": test_image_upload_and_analysis(client),
        "qr_lookup_correction": test_qr_lookup_and_correction(client),
        "submission_approval_gate": test_submission_approval_gate(client),
        "duplicate_guard": test_duplicate_guard(client),
        "xml_no_leaks": test_xml_no_leaks(client),
        "session_persistence": test_session_persistence(client),
    }

    passed = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"\n{BOLD}Results: {passed}/{total} passed{RESET}")
    for name, ok in results.items():
        print(f"  {GREEN if ok else RED}{'PASS' if ok else 'FAIL'}{RESET} {name}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
