#!/usr/bin/env python3
"""Re-authorize a Gmail user token with calendar + drive scopes (manual code exchange)."""
import json
import os
import sys
import time
from google_auth_oauthlib.flow import InstalledAppFlow

TOKENS_DIR = "/opt/truesight_autopilot/config/gmail"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
]
CODE_FILE = "/tmp/oauth_code.txt"

def client_config(existing):
    return {
        "installed": {
            "client_id": existing["client_id"],
            "client_secret": existing["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost:8080/"],
        }
    }

def main():
    account = sys.argv[1]
    token_path = os.path.join(TOKENS_DIR, f"{account}_token.json")
    existing = json.load(open(token_path))
    flow = InstalledAppFlow.from_client_config(client_config(existing), SCOPES, redirect_uri="http://localhost:8080/")
    url, _ = flow.authorization_url(prompt="consent")
    print(url, flush=True)
    code = None
    for _ in range(180):
        if os.path.exists(CODE_FILE):
            code = open(CODE_FILE).read().strip()
            if code:
                break
        time.sleep(5)
    if not code:
        print("TIMEOUT: no code provided", flush=True)
        return 1
    flow.fetch_token(code=code)
    creds = flow.credentials
    backup = token_path + ".bak"
    if not os.path.exists(backup):
        os.rename(token_path, backup)
    out = json.loads(creds.to_json())
    out["scopes"] = list(creds.scopes)
    json.dump(out, open(token_path, "w"), indent=2)
    try:
        os.remove(CODE_FILE)
    except OSError:
        pass
    print(f"OK: {account} token updated; scopes={creds.scopes}", flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
