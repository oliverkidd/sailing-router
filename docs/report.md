# Routing eval

Suite: `ids`  
When: 2026-10-08T22:45:05.275333+00:00  
Git: `82be2af`  

## Cases

| Case | Split | Feasible | Regret | Router | Reference | Route time | Tag |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `rti-w` | tune | yes | — | 9.20 h | — | 0.70 s | no_reference |
| `rti-ne` | tune | yes | — | 8.60 h | — | 0.55 s | no_reference |
| `rti-s` | tune | yes | — | 6.89 h | — | 0.61 s | no_reference |
| `rti-sw` | select | yes | — | 9.30 h | — | 0.66 s | no_reference |
| `belle-ile` | select | yes | — | 4.19 h | — | 0.24 s | no_reference |
| `giraglia` | confirm | yes | — | 30.99 h | — | 1.18 s | no_reference |
| `rorc-channel` | confirm | yes | — | 25.76 h | — | 1.19 s | no_reference |
| `fastnet-sw` | confirm | yes | — | 82.60 h | — | 17.06 s | no_reference |
| `fastnet-veer` | confirm | yes | — | 83.93 h | — | 15.78 s | no_reference |
| `aegean-600-n` | confirm | yes | — | 69.41 h | — | 12.61 s | no_reference |
| `aegean-600-shift` | confirm | yes | — | 60.60 h | — | 8.81 s | no_reference |
| `middle-sea-nw` | confirm | yes | — | 81.02 h | — | 7.32 s | no_reference |
| `middle-sea-rotate` | confirm | yes | — | 88.04 h | — | 20.07 s | no_reference |


## By split

- **tune**: median regret —, worst —, 3/3 feasible
- **select**: median regret —, worst —, 2/2 feasible
- **confirm**: median regret —, worst —, 8/8 feasible

Regret is `(router time − reference time) / reference time` on the same boat and the same wind. Negative would mean the router beat the reference. Chart races without a cached oracle are feasibility and route-time only.
