# Voice receive over DAVE

How `notes/recorder.py` captures Discord voice, and why it is written the way it
is. Everything here was established empirically against a live call — if you
change the recipe, verify it the same way rather than reasoning from the docs,
which are ambiguous in at least one place that cost us a day.

## The problem

Discord voice channels now require **DAVE**, an MLS-based end-to-end encryption
layer. A client that doesn't speak it gets the voice websocket closed with
**close code 4017** ("E2EE/DAVE protocol required"). The bot appears to join the
channel — that's just the gateway voice *state* — but the voice connection never
completes, and `is_connected()` stays false.

No Python Discord library implements DAVE **receive**. nextcord and disnake both
implement the *encrypt* half only, and say so in comments: bots send audio, they
don't listen. libdave's own AFL fuzzer never sets a key ratchet, so the
with-a-key decrypt path we depend on is unfuzzed upstream.

What makes this possible at all: a bot in the call is a legitimate member of the
MLS group, so it legitimately holds the keys. We're implementing the protocol,
not breaking it.

## Requirements

- **nextcord 3.2+** with **dave-py** (`pip install "nextcord[voice]"`).
  nextcord 3.1 has no DAVE whatsoever and always gets 4017.
- **libopus** on the host (`apt install libopus0`) for decoding.
- nextcord 3.2 rejects `async def setup(bot)` in cogs — all of ours are sync.

## The pipeline

Per received RTP packet:

```
RTP packet
  -> transport AEAD unwrap   (aead_xchacha20_poly1305_rtpsize)
  -> strip RTP padding
  -> skip the RTP extension body
  -> DAVE unwrap             (dave.Decryptor + that speaker's key ratchet)
  -> Opus decode
  -> 16kHz mono WAV on a shared clock
```

### 1. Transport unwrap

The mode Discord negotiates is `aead_xchacha20_poly1305_rtpsize`. The framing —
confirmed on 60/60 real packets, and the one place the official docs are
genuinely ambiguous:

| Part | Extent |
|---|---|
| **AAD** (unencrypted header) | fixed 12 bytes + CSRCs + the **4-byte extension preamble only** |
| **Ciphertext** | everything after that, minus the trailing 4 bytes |
| **Nonce** | those trailing 4 bytes, placed at the **front** of a 24-byte zero-padded nonce |

The docs say CSRCs and "the extension preamble" are unencrypted. They mean the
preamble *literally* — the 4-byte `0xBEDE`+length header. **The extension body
is inside the ciphertext**, so after decrypting you skip `ext_words * 4` bytes to
reach the payload. Including the body in the AAD fails authentication on every
single packet.

### 2. RTP padding

If the padding bit (`0x20`) is set in the first byte, the final byte of the
decrypted payload gives how many trailing bytes to drop.

### 3. Is it even a DAVE frame?

A real DAVE frame **ends with the magic bytes `0xFAFA`**. Discord's ~11-byte Opus
silence/comfort-noise frames are not DAVE frames. Check before calling
`Decryptor.decrypt`.

### 4. DAVE unwrap

Route by SSRC. The voice websocket's **SPEAKING event (op 5)** carries
`{ssrc, user_id}`; `DaveVoiceClient` hooks it to build the map. Each speaker's
frames must go to a `Decryptor` holding **that user's** ratchet, from
`e2ee_state._session.get_key_ratchet(str(user_id))`.

## Three rules that will bite you

1. **A key ratchet is single-use.** `transition_to_key_ratchet` *moves* the
   ratchet into the Decryptor (nanobind relinquishes the Python object). Reusing
   one raises "attempted to access a relinquished instance". Fetch a fresh
   ratchet per Decryptor — we recreate periodically anyway, which conveniently
   keeps up with MLS re-keying when people join or leave.
2. **libdave/mlspp is not thread-safe.** The capture thread does `recv()` and
   nothing else; all crypto runs on the event loop.
3. **nextcord's Opus decoder corrupts the heap on mono packets.** It sizes the
   PCM buffer by the *packet's* channel count, but the decoder is stereo and
   libopus always writes interleaved stereo — so a mono packet gets half the
   space libopus then writes, and you get `malloc(): invalid size (unsorted)`.
   `SafeOpusDecoder` sizes by the decoder's own channel count, which is what
   nextcord already does in its packet-loss branch. **This is an upstream bug
   and has not been reported.**

## The shared clock

Each speaker gets their own WAV, but they must share one timeline. Frames are
written at their real arrival offset, gaps padded with silence, and every track
padded to the same length at the end.

Without this, a speaker's quiet stretches are simply deleted, their audio is
compressed, tracks drift apart, and `notes/pipeline.py` — which interleaves
segments by timestamp to reconstruct the conversation — attributes the right
words to the right people in the wrong order.

Audio is downsampled to **16kHz mono on the way in**, which is what Whisper wants
anyway: ~230MB per speaker for a two-hour meeting instead of ~1.4GB.

## Verifying a change

`dave_poc/` holds the throwaway probes used to establish all of the above. They
run in their own venv against a live channel and print per-stage counters, so you
can tell exactly which layer broke:

- `poc_connect.py` — connect, MLS group, ratchet availability
- `poc_framing.py` — brute-forces the transport framing and reports what authenticates
- `poc_dave.py` — dumps decrypted frame structure and tries user/offset combinations
- `poc_decrypt.py` — the full pipeline to per-speaker WAVs

A healthy run is ~100% at every stage. If `transport decrypt FAIL` is high the
framing is wrong; if `DAVE decrypt None` is high it's the ratchet or routing.
