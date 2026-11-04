# Jingles

Drop singing bowl recordings here (WAV, FLAC, MP3, OGG, AIFF). When any are
present, episodes start and end with a random selection of them instead of the
built-in synthesized bowls.

- Each file should be one strike that rings all the way out to silence; files
  that end abruptly get a 2.5 s fade-out (and a warning in the logs).
- Levels are normalized automatically; mono or stereo is fine.
- CC0 sources: freesound.org (search "singing bowl", filter licence CC0).

The folder is mounted read-only at /app/jingles; no restart needed.
