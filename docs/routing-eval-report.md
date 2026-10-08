# Routing eval

Original fast-suite baseline: median about 3.4% slower than the proved time, worst 7.5% on the 16-knot run. Beats tacked two or three times. A fast wind oscillation never reached the mark.

## Proved cases now

Steady wind, no land. The reference is the straight line or one layline tack or gybe.

| Case | Before | After |
| --- | --- | --- |
| Reaches (beam, close, broad, 8–16 kn) | about +3.4% | 0 to +0.2% |
| Dead downwind, 12 and 16 kn | +3.3% and +7.5% | −0.3% and −1.0% |
| Beats | +2.5% to +4.1%, 2–3 tacks | +1.0% to +2.8%, usually 1 tack |
| Mark 20° off the wind | +2.0% | +2.5%, 1 tack |
| One 20° shift | +2.5% | +3.3%, 1 tack |

On the steady-wind set the middle of the errors is about **0.6%**. The worst proved case is about **2.8%**. The runs sit on the proved time; a negative tenth of a percent is the 6-minute-scale arrival at the mark circle, not a faster-than-polar boat.

## Other cases

- Fast oscillation: finishes (was unreachable), two tacks. Slightly quicker than libweatherrouting on the same wind.
- Island across a reach: legal, 3729 s. The grid search is 3722 s.
- Forecast that updates at 20 minutes: +0.1% versus perfect foresight (5795 s vs 5792 s).
- A 35° shift that was not in the tuning set: +5.9%, two tacks. The router now starts on the side that shift will lift.
- Solent acceptance suite passes. The Cowes cove leg finishes; it had briefly become unreachable while the land probe was too long, and that is fixed.

## What the router does differently

When land blocks the rhumb line, remaining distance follows a headland chain, so sailing around the land is not counted as stalling. The boat aims past a corner once that water is open, instead of sailing onto the corner and snapping. A heading that turns away from the mark is not taken while a heading toward the mark is still clear. A commit ends when the other board fetches the race mark. Upwind and downwind it holds the long board until the other VMG heading enters the mark circle, then tacks or gybes once. A tack inside the first mile is still suppressed, except when the board in hand is into land or the other board already lays the race mark. It stops on that circle instead of sailing through it. A commit whose straight chord would cross land is cut back. The heading fan looks far enough to see an island on the rhumb line, and falls back to a short probe when the boat is already against the shore. The first board of a leg, and only that, looks ahead for a shift that is still there half an hour later.

## Finishing a course

The editor's Solent loop (west of the Needles, south of the island, back in) used to die with an empty frontier. On the GFS wind from the run on 6 Oct 2026 it is 68.1 nm and 16.1 h, with 2 tacks and 5 gybes. Before this pass it was 70.8 nm, 16.3 h, 4 tacks and 10 gybes, and the track gybed onto a guidance dot at Hurst. The same loop finishes in a northerly, north-easterly, easterly, southerly, south-westerly, westerly, and north-westerly. The north-westerly dropped from 31 h to 9.6 h. A westerly that flips 18° every four minutes also finishes. A 0.22 nm triangle, a 0.4 nm triangle, a 6 nm hairpin, a 25 nm beat-reach-run, a zigzag of 150° turns, and a triangle around an island also finish. `python -m eval.run --suite robust` fails if any of them do not.

Round the Island in a north-easterly is a clean lap (59 nm, 2 tacks). A southerly is 52 nm. A westerly finishes (65 nm, 13 tacks). A south-westerly finishes cleanly (64 nm, 13 tacks, 2 gybes) after the Needles mark was moved into deep water SW of the rocks and a geometric fetch through a shoal stopped counting as a layline. Belle-Ile, a short Giraglia offshore triangle, and the Fastnet Channel legs (Needles → Lizard → Plymouth) leave navigable starts and finish. A dead beat west down the Solent is still a full-width channel beat.

Polar VMG/speed and depth-cell caches cut long-race wall time by about 4–14× without changing those routes.

## Still open

Beat-12 still uses one tack and is 1.6% slow. A wind that is stronger on one side of the course still has no trusted reference. Chart races were not given a proved time. The Solent acceptance suite still passes, including the Cowes cove leg. Full Fastnet Rock and Aegean 600 one-hop crossings still need a longer waypoint corridor.
