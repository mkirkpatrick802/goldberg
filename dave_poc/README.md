# DAVE proof-of-concept (throwaway)

A go/no-go probe. It answers one question that reading the source can't:

> When a bot on nextcord 3.2 + dave-py joins a **DAVE-required** voice channel as
> a passive listener, does the MLS group establish, and can we get a decryption
> **key ratchet** for a speaker?

If yes, building live voice-receive in-house is real-but-doable engineering
(the `dave.Decryptor` primitive exists). If no, passive receive on DAVE channels
is a dead end and we use an external recorder. **It decrypts no audio** — that's
Stage 2, only worth building if this passes.

## Two safety rules

1. **Its own venv — never the bot's `.venv`.** This needs nextcord **3.2**, and
   3.2 breaks the `async def setup` pattern the production cogs use. Installing
   it into the shared venv would take the whole bot down.
2. **One gateway session per bot token.** Running this with the live token while
   the production bot is up will fight over the session. **Stop the production
   bot first**, or make a separate test-bot application and export its token as
   `BOT_TOKEN` before running.

## Run it (on the server)

```bash
# 1. Isolated venv + deps (server Python is 3.12; if pip can't find a dave-py
#    wheel for it, tell me — that's itself a finding).
cd ~/maple-server/goldberg/dave_poc
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# 2. Free up the bot token.
cd ~/maple-server && ./stop_maple.sh

# 3. Get a VOICE channel ID: Discord Developer Mode on ->
#    right-click the voice channel -> Copy Channel ID.
#    Then JOIN that channel yourself and keep talking during the run.

# 4. Probe (token is read from ../.env automatically).
cd ~/maple-server/goldberg/dave_poc
./.venv/bin/python poc_connect.py <voice_channel_id>

# 5. Bring the real bot back.
cd ~/maple-server && ./start_maple.sh
```

Paste the whole `[DAVE-PoC]` output back to me.

## Reading the result

| What you see | Meaning |
|---|---|
| `connect() FAILED` / `closed with 4017` | Even 3.2 + dave-py can't negotiate DAVE here. **NO-GO.** |
| `MLS GROUP ESTABLISHED` | Connection + group work for a passive bot. Big step. |
| `get_key_ratchet(...) -> <non-None> << KEY AVAILABLE` | Decryption keys reachable. **GO** for Stage 2. |
| group established but all ratchets `None` | Retry while someone is actively talking; if still none, strong **NO-GO**. |
| group did **not** establish in 30s | A listener can't get into the group. Likely **NO-GO**. |

## Cleanup

Throwaway. When we're done deciding, delete the whole `dave_poc/` folder (and
its `.venv`). Nothing else depends on it.
