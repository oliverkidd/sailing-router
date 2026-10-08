# What to test

Two pieces of work are in the app: the router changes from the course-quality pass, and live GPS race mode.

## Router

Your Solent loop on the GFS wind from 6 Oct 2026 06:00 UTC is 68.1 nm and 16.1 h, with 2 tacks and 5 gybes. Before the pass it was 70.8 nm, 16.3 h, 4 tacks and 10 gybes, and the track gybed onto a guidance dot at Hurst. The straight line across that corner is the spit, so one turn there remains. The shallowest water on that track is 5.6 m.

Re-run that course from the planner. The API reloads with the code.

Also checked:

- The same loop finishes with the wind from the north, east, south, west, and northwest. A west beat down the Solent is still a full-width zigzag, about 18 tacks, because the channel is about a mile wide and the upwind angle runs into each shore.
- Round the Island finishes in a north-easterly (59 nm, 2 tacks, 4 gybes), a southerly (52 nm, 2 tacks), a westerly (65 nm, 13 tacks), and a south-westerly (~64 nm, ~17 tacks). The south-westerly used to scribble on the Shingles Bank north of the Needles. Two fixes: the Needles mark sits in deep water SW of the rocks, and a geometric “layline” through a shoal no longer counts as a fetch.
- Short triangles, a hairpin, a 25 nm beat-reach-run, a zigzag, and a triangle around a synthetic island all finish. `python -m eval.run --suite robust` is 17/17.
- **Full chart races now finish clean:**
  - Aegean 600 with Kea (Santorini→Kasos→Kos→Agathonisi→Kea), steady and veering — ~7 / ~5 tacks.
  - Middle Sea Messina→Stromboli→Favignana→Malta, NW and rotating — ~9 / ~7 tacks (was a 140-tack thrash on Cape Murro, then died leaving Stromboli).
  - Fastnet Rock (Cowes→Needles→Lizard→Fastnet→Plymouth), steady SW and veering — ~20 / ~5 tacks.
  - Giraglia finishes (~2 tacks).
- Track plots for review are in `docs/route-reviews/`.
- Open-water beat is one tack and about 1.6% off the proved time. Fast suite median regret about **0.1%**. Solent acceptance suite passes.

### Router fixes that landed this pass

1. **Wrong-side headland guidance** — children inherit a guide index rounded north of a spit, then drift south; rem aimed through land. Guide index now walks back when the active bend is blocked and the prior bend is still reachable (Needles proximity advance still sticks inside the pass radius).
2. **Post-mark tack lockout** — `_sailed_since_tack_nm` used to walk into the previous leg’s tack history after a mark snap, locking the fan to the inbound board (Stromboli→Favignana died in two steps). Mark nodes end the walk.
3. **Shore lockout fall-through** — when the held board is into land, the fan no longer falls back to every heading (Messina-coast tack/gybe thrash). Only a clear opposite board is offered.
4. **Adaptive geo margin** — compact coastal races get a 1° pad so the chain is not pinned to a cape; ocean-spanning courses (Fastnet, span ≥6°) stay at 0.5° so the return leg does not die inside a country-sized land mask.

## Live / offline smoke

`node apps/web/scripts/test-live-race.mjs` checks ahead/behind against a planned track (on time / ahead / behind), reroute eligibility after depart, and the voyage tile budget cap. CDN chart URLs use `NEXT_PUBLIC_CHART_BASE` (`apps/web/src/lib/chartUrls.ts`) — CDN hosting itself is left for later.

## Performance (long races)

Polar VMG/speed and depth/land cell reads are cached. Killing the Messina thrash dropped Middle Sea from a multi-minute scribble to ~25 s. Current wall times (product params, cached geo):

| Case | Runtime | Tacks |
| --- | --- | --- |
| Round the Island SW | ~3.5 s | ~17 |
| Middle Sea NW | ~25 s | 9 |
| Aegean 600 N | ~55 s | 7 |
| Fastnet SW | ~62 s | 20 |
| Robust suite CPU | ~21 s | 17/17 |

## Delivery

The chart on disk is `coastline.pmtiles` (575 MB) and `depth.pmtiles` (805 MB). GitHub blocks a file over 100 MB, so these cannot be committed. Keep the code in the git repo. Put the two archives on object storage or a CDN and point the map at that URL (left for a later pass).

On the device, chart ranges and terrain tiles go into IndexedDB.

- Phone budget is 400 MB, or 20% of the origin quota if that is smaller. Desktop is 1.2 GB, with the same 20% rule. The floor is 80 MB.
- When a new tile would pass the budget, the oldest tiles that are not part of a saved voyage are deleted and can be downloaded again later.
- A finished route is already stored in this browser. **Keep this course offline** also downloads zoom 9 and 11 chart tiles for the course box (at most 280 per archive) and pins them so eviction will not remove them.
- The service worker caches the app shell. After one online visit, the planner can open with no signal. Ahead/behind uses the stored route and the GPS clock, so it does not need the network.
- Wind still needs a connection the first time. Once the route exists, comparing your position with the plan does not.

## Live race mode

On the planner map, bottom left, **Live** turns on the browser's GPS.

- If the course has no start, the first fix becomes the start. A start you already set is left alone.
- **Set start to my position** overwrites the start with the fix.
- The cyan dot is you. The first fix also moves the map to you.
- After a route exists, the boat icon is where the plan says you should be at the current time. A dashed line joins you to that boat.
- **Vs plan** is ahead or behind. It compares the time of the nearest point on the planned track with the clock. Ahead means you have reached a place the plan does not reach until later.
- **Off track** is how far you are to port or starboard of that point.
- Once the planned departure time has passed, **Reroute from here** sets the start to your position, sets departure to now, and runs the route again with the same marks, finish, boat, and forecasts.
- Dragging the time bar stops following the clock. **Show the plan at now** puts the boat icon back on the current time. The faded boat, when you have scrubbed away, is still the plan at now.

## Still open

- Beat-12 is still about 1.6% slow on one tack. Chart races are feasibility, not proved regret.
- CDN upload of the PMTiles archives (wiring is in place; left alone this pass as requested).
- Browser map screenshots of every leg under shifting wind were not taken — use `docs/route-reviews/*.png` for track review.
- `gradient-beat-12` oracle A* can fail on a large window (pre-existing oracle limit); the router itself still finishes the tune set.
