"""
Voice notes pipeline for Goldberg.

Deliberately free of nextcord and voice-receive dependencies: everything in
here runs from a plain terminal so the transcribe -> summarize path can be
tested with a pre-recorded file before any Discord plumbing exists. The live
voice capture (recorder.py) and the cog land on top of this, not inside it.
"""
