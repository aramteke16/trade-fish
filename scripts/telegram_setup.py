"""One-shot Telegram channel setup for the trading pipeline.

What it does, in order:
  1. Verifies the bot token with /getMe.
  2. Calls /getUpdates with offset=-1 to find every chat the bot has seen,
     prints them as a numbered menu (channels first).
  3. Asks you to pick the channel (or accepts --chat-id if you already
     know it).
  4. Writes telegram_bot_token / telegram_chat_id /
     telegram_notifications_enabled to the running pipeline's app_config.
  5. Sends a test message to confirm the wiring end-to-end.

Run:
    python scripts/telegram_setup.py <BOT_TOKEN>
    python scripts/telegram_setup.py <BOT_TOKEN> --chat-id -1001234567890

Pre-requisites (Telegram side, one time only):
  - Create the private channel in Telegram.
  - Add your bot as an admin to that channel with "Post Messages" enabled.
  - Send any message in the channel so /getUpdates has something to show.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Optional

import requests


# Make ``tradingagents`` importable when running directly under scripts/.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _api(token: str, method: str, **params: Any) -> dict:
    """Thin wrapper around the Telegram Bot HTTP API."""
    r = requests.get(
        f"https://api.telegram.org/bot{token}/{method}", params=params, timeout=15
    )
    body = r.json()
    if not body.get("ok"):
        raise SystemExit(
            f"Telegram /{method} failed ({r.status_code}): "
            f"{body.get('description', body)}"
        )
    return body


def _discover_chats(token: str) -> list[dict]:
    """Return every distinct chat the bot has seen recently.

    ``getUpdates`` only returns recent updates — if you haven't messaged
    the bot or posted in the channel since the last poll, the result is
    empty. ``offset=-1`` asks for the latest update unconditionally.
    """
    seen: dict[int, dict] = {}
    upd = _api(token, "getUpdates", offset=-1, timeout=0).get("result", [])
    for u in upd:
        for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
            msg = u.get(key)
            if not msg:
                continue
            chat = msg.get("chat") or {}
            cid = chat.get("id")
            if cid is not None and cid not in seen:
                seen[cid] = chat

    # Channels first (most likely target), then groups, then private.
    def _rank(c: dict) -> int:
        t = c.get("type")
        return {"channel": 0, "supergroup": 1, "group": 2, "private": 3}.get(t, 9)

    return sorted(seen.values(), key=_rank)


def _print_chats(chats: list[dict]) -> None:
    if not chats:
        print(
            "\nNo chats found in the bot's recent updates.\n"
            "Send a message in your channel (with the bot added as admin) "
            "and re-run this script, OR pass --chat-id directly.\n"
        )
        return
    print("\nChats the bot can see:")
    print(f"  {'#':>2}  {'type':<11}  {'id':>16}  title")
    print(f"  {'-'*2}  {'-'*11}  {'-'*16}  {'-'*30}")
    for i, c in enumerate(chats):
        title = c.get("title") or c.get("first_name") or c.get("username") or ""
        print(f"  {i:>2}  {c.get('type', '?'):<11}  {c.get('id'):>16}  {title}")


def _patch_config(key: str, value: Any) -> None:
    """Write through the same code path the FastAPI route uses, so the
    write goes to the running database regardless of where the script
    runs from."""
    from tradingagents.web.config_service import set_config
    set_config(key, value)
    print(f"  • app_config.{key} ← {value if not isinstance(value, str) or len(value) < 30 else value[:8] + '…'}")


def _send_test(token: str, chat_id: str) -> dict:
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "text": (
                "<b>Trading pipeline · setup OK</b>\n"
                "If you can see this, the channel is wired correctly.\n"
                "Every precheck, order, monitor tick, exit and EOD summary "
                "from this pipeline will now land here."
            ),
        },
        timeout=15,
    )
    body = r.json()
    if not body.get("ok"):
        raise SystemExit(
            f"sendMessage failed ({r.status_code}): {body.get('description', body)}\n"
            f"Common causes:\n"
            f"  - chat_id missing '-100' prefix for channels\n"
            f"  - bot isn't an admin in the channel\n"
            f"  - 'Post Messages' admin permission disabled"
        )
    return body


def main() -> int:
    p = argparse.ArgumentParser(description="Wire a private Telegram channel into the trading pipeline.")
    p.add_argument("token", help="Bot token from @BotFather")
    p.add_argument("--chat-id", help="Skip discovery; use this chat id directly (e.g. -1001234567890)")
    p.add_argument("--no-test-message", action="store_true", help="Don't send a test message at the end.")
    p.add_argument("--keep-disabled", action="store_true",
                   help="Save token+chat_id but leave telegram_notifications_enabled=false.")
    args = p.parse_args()

    # 1. Verify token
    me = _api(args.token, "getMe").get("result", {})
    print(f"\nBot OK: @{me.get('username')} — {me.get('first_name')} (id={me.get('id')})")

    # 2/3. Pick chat
    chat_id: Optional[str] = args.chat_id
    if not chat_id:
        chats = _discover_chats(args.token)
        _print_chats(chats)
        if not chats:
            return 1
        # Auto-pick if exactly one channel is present
        channels = [c for c in chats if c.get("type") == "channel"]
        if len(channels) == 1:
            chat_id = str(channels[0]["id"])
            print(f"\nAuto-selected the only channel: {chat_id} "
                  f"({channels[0].get('title')})")
        else:
            try:
                pick = input("\nPick a row number (0-based) to use as the target chat: ").strip()
            except EOFError:
                print("\nNo selection. Re-run with --chat-id <id>.")
                return 1
            try:
                chat_id = str(chats[int(pick)]["id"])
            except (ValueError, IndexError):
                print(f"Invalid selection {pick!r}.")
                return 1

    # 4. Patch the running pipeline's app_config
    print(f"\nWriting config (target chat_id={chat_id}):")
    _patch_config("telegram_bot_token", args.token)
    _patch_config("telegram_chat_id", str(chat_id))
    _patch_config("telegram_notifications_enabled", not args.keep_disabled)

    # 5. Verify with a test message
    if args.no_test_message:
        print("\nSkipped test message (--no-test-message). Done.")
        return 0
    print("\nSending test message…")
    _send_test(args.token, str(chat_id))
    print("Delivered. Check your channel — you should see the test message.")
    print("\nDone. Every precheck / order / monitor-tick / exit / EOD event "
          "from the trading pipeline will now stream here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
