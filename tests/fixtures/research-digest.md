# Research digest — fixture (tpro landscape scan)

Recorded example of the research digest contract. Used to smoke-test the
fold-into-brainstorm.md path offline and to anchor the contract in tests.

## Findings
- Jellyfin publishes an official Android TV Kotlin SDK: authenticated client,
  WebSocket support, Jellyfin-specific types.
- Material 3 for TV (`androidx.tv:tv-material`) is on a stable channel as of 2025.
- ExoPlayer (media3) plays Jellyfin's HLS/DASH transcoded streams.

## Surprises / didn't-know-to-ask
- An **official Jellyfin Android TV client** already exists — decide whether we're
  replacing it or differentiating, not building blind.
- The Jellyfin **SDK** exists at all — avoids hand-rolling a REST client.

## Sources
- https://jellyfin.org/docs/general/clients/
- https://github.com/jellyfin/jellyfin-sdk-kotlin
- https://developer.android.com/training/tv

## Freshness & confidence
- High; androidx.tv stable confirmed 2025; SDK actively maintained.
- Updated a stale wiki note that called tv-material "experimental".

## Wiki provenance
- Wiki had a 2024 note (tv-material experimental) — refreshed.
- SDK + official-client facts were new from the web; saved back to the wiki.
