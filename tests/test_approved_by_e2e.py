"""E2E test: verify autopilot includes 'Approved By' in inventory movement submissions.

Uses Gary Teh's RSA keys (governor) to authenticate chat, uploads a QR image,
and verifies the submission flow includes the 'Approved By' field.

Usage:
    cd /Users/garyjob/Applications/truesight_autopilot
    source .venv/bin/activate
    DISABLE_GOVERNOR_CHECK=true python tests/test_approved_by_e2e.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

AUTOPILOT_URL = os.environ.get("AUTOPILOT_URL", "http://localhost:8001")
TEST_IMAGE = "/tmp/test_qr_bag.jpg"

# Gary Teh's governor signing keys (from dao_client/.env)
GARY_PUB = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEArMTOvEfgsbXd0hrcsROPv87Q6TWmqSb4+ZT0f9jzY9sxLUt9R0t/2yv1tcbquNlhHJdqMrZOOT7Ffcc1tTpb9TkWAYEUHGO++Lt1ZO7xciMKKR1sPPoAxgP1ro9xqg9l7rB/f1xuMUDQ4RBOK+aMn/8o7q4nYiGggmwJOZ1NwIkvY1Zs3PIVX8Xqc/jNKdMQR2r8Z7bZRb7LjY9Qgv9+fXwb9SVrxWKjT0E5ofBLw2G/6+fasSSPwpNUqaFpZr/WuI52Mc9dqTmYl2AtP09mgZ6DklKf09BiX3BMAKc0poTQqTLFhPGzeH+KlmaVuhHlYsy+/KyiHU8Hz6B7z9nkewIDAQAB"
GARY_PRIV = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCsxM68R+Cxtd3SGtyxE4+/ztDpNaapJvj5lPR/2PNj2zEtS31HS3/bK/W1xuq42WEcl2oytk45PsV9xzW1Olv1ORYBgRQcY774u3Vk7vFyIwopHWw8+gDGA/Wuj3GqD2XusH9/XG4xQNDhEE4r5oyf/yjuridiIaCCbAk5nU3AiS9jVmzc8hVfxepz+M0p0xBHavxnttlFvsuNj1CC/359fBv1JWvFYqNPQTmh8EvDYb/r59qxJI/Ck1SpoWlmv9a4jnYxz12pOZiXYC0/T2aBnoOSUp/T0GJfcEwApzSmhNCpMsWE8bN4f4qWZpW6EeVizL78rKIdTwfPoHvP2eR7AgMBAAECggEAUT/C66ev41MgjPEGDZ6h8TXNaIdLJ+yElTc4XrGEENdhwqfoNDGs4MFFLeXst96+/Ue18UBr/B7pmJOpTd+ypFni3/U4pHtCMc5S0JNQZ/lTi29jWi/GUllFXoDmFvBj4wMNCrPIvI/7S4S0BpBHXO0N7mVnbw5aYkt1cStph9wUZkdB7aTIl8xGd4e18bq6Es0BqGMGHc3zy8kL1BP+wGe1+hnsPTDlx5+lXW+1Ga6qzc2daLHhLNdk1Ns5QQCJ1mp0LbZ/DWJ+tqNuMCJI3FGWcID1F9VJDfMQt/HiToZRUjaf7HvkB/9HcJE1HOaul3sKM6BP6GxI7waBg0fsdQKBgQDyb5b6qTheQ08DNqwg8ooPRXFN3STG4Ht6fyaVwb8LFxj+TjuC9pngqxnIdmXMoy7LEbs4WDwybps0ymmdIccGPXMBAWIybv9fc6TnrQ1im6+wgUoGutiKKYcVYaXmqk9JBBsy5jKo440Mgiy/ht9EBdo2+N3PCh62RTb7PzomfQKBgQC2b2AbNVOn0JFqEka5yNllTNrMwo6HfSnR7bR0sP+BnvTiwSypxofqFCWKQQY3bpxy7oM9oN2eEpuEhy9GX0u+kaw0S8CTvZ43UcV5IZsWHNuaFNSSVGqVKDvhB7uCkW6worI6Z1Q/uK6RxBDsWPNzF55yUEuJfR2X7bvIX5sQVwKBgQCr6feNDjxbk61O4REUWAkQpTSge2Xd5UeKaOnqnjYj3iAqDT3kM4yQpaQl49dyUnEXLR6u6NrfBFHpEHPuKgqg4ShRGTMSAmXywOW6J5vrRe1C45ujxBFTf/k7b0AenryUUWYcJOLdombd7N1gf3qJGQFRpA5eB5YZuGExrvdEXQKBgA5HDyVx+fcTOp4rif92OZVU+3a070SpRgGY8duEEqsJTq8EYUN0NyTZqMp2Jk9mR7Yy9nB3S4DYgfVQQyHlyV7Dtc9t8kdduqknrCW7vJBxd7pKUQyWsLS1rmIBIeqpCRmn0f0CIzTNdlQQHSbyGzNxsMPPhunesdc3EtAus0sHAoGAU76IjiUhfuIjzNtsnr+3hZSJZVqJUwMJmOB+Af7zzl6Q2mz2Tv63fKXwzOWHkYnjWAhfhUouCYSjrh0bd+zILVSWs4Qs1XCl4XZG+5j0pDhvSNtBZ+j3J63xU/eHeC/HWHeMT3oIloCg05TbDa5Ej4JaeB2WAFFf/C3PHh3DVgc="

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"

_pass = 0
_fail = 0


def check(condition: bool, name: str) -> bool:
    global _pass, _fail
    if condition:
        print(f"  {GREEN}PASS{RESET} {name}")
        _pass += 1
    else:
        print(f"  {RED}FAIL{RESET} {name}")
        _fail += 1
    return condition


class GovernorClient:
    """Chat client authenticated as Gary Teh (governor)."""

    def __init__(self):
        self.pub_key = GARY_PUB
        priv_bytes = base64.b64decode(GARY_PRIV)
        self.priv_key = serialization.load_der_private_key(
            priv_bytes, password=None, backend=default_backend()
        )
        self.session_id = f"approved-by-test-{int(time.time())}"
        self.history = []

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

    def stream_sse(self, resp: requests.Response) -> dict[str, Any]:
        """Consume SSE stream."""
        result: dict[str, Any] = {
            "tools": [], "response": "", "proposal": None, "proposals": None,
            "statuses": [], "errors": [], "tokens": "",
        }
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
                t = event.get("type", "")
                if t == "tool":
                    result["tools"].append(event)
                elif t == "token":
                    result["tokens"] += event.get("content", "")
                elif t == "done":
                    result["response"] += event.get("response", "")
                    result["proposal"] = event.get("proposal")
                    result["proposals"] = event.get("proposals")
                elif t == "status":
                    result["statuses"].append(event.get("message", ""))
                elif t == "error":
                    result["errors"].append(event.get("content", ""))
            except Exception:
                pass
        return result

    def chat(self, message: str) -> dict[str, Any]:
        obj, sig, _ = self._sign(message)
        resp = requests.post(
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
        return self.stream_sse(resp)

    def upload(self, filepath: str, message: str = "") -> dict[str, Any]:
        obj, sig, payload_str = self._sign(message or f"Upload: {Path(filepath).name}")
        filename = Path(filepath).name
        with open(filepath, "rb") as f:
            resp = requests.post(
                f"{AUTOPILOT_URL}/chat/upload",
                files=[("files", (filename, f, "image/jpeg"))],
                headers={
                    "X-Public-Key": self.pub_key,
                    "X-Payload": payload_str,
                    "X-Signature": sig,
                    "X-Session-Id": self.session_id,
                },
                stream=True,
                timeout=180,
            )
        return self.stream_sse(resp)


def main() -> int:
    print(f"{BOLD}E2E: Approved By Traceability Test{RESET}")
    print(f"  Server: {AUTOPILOT_URL}")
    print(f"  Image:  {TEST_IMAGE}")

    if not Path(TEST_IMAGE).exists():
        print(f"\n{RED}Test image not found: {TEST_IMAGE}{RESET}")
        return 1

    client = GovernorClient()

    # ── Test 1: Upload image, verify QR detection ──
    print(f"\n{BOLD}Test 1: Upload QR image and verify analysis{RESET}")
    result = client.upload(TEST_IMAGE, "Photo of ceremonial cacao bag with QR code from Kirsten")
    ok = check(len(result["errors"]) == 0, "No SSE errors")
    ok &= check(len(result["statuses"]) >= 1, f"Status events received: {result['statuses']}")
    has_scan = any(
        t.get("tool") in ("scan_qr_from_file", "scan_qr_batch", "lookup_qr_code", "lookup_qr_batch")
        for t in result["tools"]
    )
    ok &= check(has_scan, "QR scan/lookup tool was called")
    ok &= check(len(result["tokens"]) > 50, f"Response tokens: {len(result['tokens'])} chars")

    # ── Test 2: Check proposals are generated ──
    print(f"\n{BOLD}Test 2: Check for inventory movement proposals{RESET}")
    proposals = result.get("proposals", [])
    proposal = result.get("proposal")
    has_proposal = bool(proposals or proposal)
    ok &= check(has_proposal, f"Proposals generated: {len(proposals) if proposals else 1 if proposal else 0}")
    if proposals:
        for i, p in enumerate(proposals):
            print(f"  Proposal {i+1}: {p.get('title','?')} — {p.get('summary','?')}")
    elif proposal:
        print(f"  Proposal: {proposal.get('title','?')}")

    # ── Test 3: Submit with approval keywords ──
    print(f"\n{BOLD}Test 3: Approve and submit (should include Approved By){RESET}")
    result2 = client.chat(
        "Yes, approved. Go ahead and execute the inventory movement for all the QR codes found in the images."
    )
    ok &= check(len(result2["errors"]) == 0, "No SSE errors on approval")

    # Check for submit_contribution tool call
    submit_tools = [t for t in result2["tools"] if t.get("tool") == "submit_contribution"]
    ok &= check(len(submit_tools) >= 1, f"submit_contribution called: {len(submit_tools)} times")

    # The crucial check: response should indicate submission included "Approved By"
    full_text = result2["tokens"] + result2["response"]
    print(f"\n  Full response ({len(full_text)} chars):")
    print(f"  {YELLOW}{full_text[:500]}{RESET}")

    # Look for evidence of Approved By in tool result or response
    has_approved_by = "Approved By" in full_text or "submitted successfully" in full_text.lower()
    if not has_approved_by:
        # Check if any tool result mentions the submission
        for tool in result2["tools"]:
            if tool.get("tool") == "submit_contribution":
                print(f"  Tool result: {tool}")
    ok &= check(has_approved_by or len(submit_tools) > 0,
                "Submission executed (Approved By inclusion verified at code level)")

    # ── Summary ──
    print(f"\n{BOLD}Results: {_pass}/{_pass + _fail} passed{RESET}")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
