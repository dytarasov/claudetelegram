"""Ждёт первое сообщение боту и прописывает автора в ALLOWED_USER_IDS."""
import json, re, sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
env = ROOT / ".env"
token = re.search(r"^TELEGRAM_BOT_TOKEN=(.+)$", env.read_text(), re.M).group(1).strip()
api = f"https://api.telegram.org/bot{token}"


def call(method, **params):
    url = f"{api}/{method}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=40) as r:
        return json.load(r)


import urllib.parse  # noqa: E402

me = call("getMe")
if not me.get("ok"):
    sys.exit(f"токен не принят: {me}")
print(f"бот: @{me['result']['username']} ({me['result']['first_name']})", flush=True)
print("жду сообщение боту…", flush=True)

deadline = time.time() + int(sys.argv[1] if len(sys.argv) > 1 else 240)
offset = 0
while time.time() < deadline:
    upd = call("getUpdates", offset=offset, timeout=25)
    for u in upd.get("result", []):
        offset = u["update_id"] + 1
        msg = u.get("message") or u.get("edited_message") or {}
        frm = msg.get("from")
        if not frm:
            continue
        uid = frm["id"]
        print(f"FOUND {uid} @{frm.get('username')} {frm.get('first_name')}", flush=True)
        text = env.read_text()
        text = re.sub(r"^ALLOWED_USER_IDS=.*$", f"ALLOWED_USER_IDS={uid}", text, flags=re.M)
        env.write_text(text)
        print("записан в .env", flush=True)
        sys.exit(0)
sys.exit("никто не написал за отведённое время")
