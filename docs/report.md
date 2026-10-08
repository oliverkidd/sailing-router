# Routing eval

Suite: `robust`  
When: 2026-10-08T21:22:19.546591+00:00  
Git: `f219ca2`  

## Cases

| Case | Split | Feasible | Regret | Router | Reference | Route time | Tag |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `robust-solent-loop-0` | confirm | yes | — | 7.67 h | — | 5.22 s | no_reference |
| `robust-solent-loop-45` | confirm | yes | — | 8.66 h | — | 7.69 s | no_reference |
| `robust-solent-loop-90` | confirm | no | — | 3.91 h | — | 0.89 s | unreachable |
| `robust-solent-loop-180` | confirm | no | — | 0 s | — | 1.16 s | unreachable |
| `robust-solent-loop-225` | confirm | yes | — | 9.04 h | — | 3.67 s | no_reference |
| `robust-solent-loop-270` | confirm | no | — | 3.97 h | — | 1.59 s | unreachable |
| `robust-solent-loop-315` | confirm | no | — | 3.20 h | — | 1.81 s | unreachable |
| `robust-triangle-0.4-beat` | confirm | yes | — | 15.6 min | — | 0.02 s | no_reference |
| `robust-triangle-0.4-reach` | confirm | yes | — | 13.4 min | — | 0.01 s | no_reference |
| `robust-triangle-0.4-run` | confirm | yes | — | 17.7 min | — | 0.01 s | no_reference |
| `robust-hairpin-6` | confirm | yes | — | 2.43 h | — | 0.05 s | no_reference |
| `robust-long-25` | confirm | yes | — | 13.19 h | — | 0.30 s | no_reference |
| `robust-island-triangle` | confirm | yes | — | 1.81 h | — | 0.06 s | no_reference |
| `robust-solent-triangle` | confirm | yes | — | 13.3 min | — | 0.01 s | no_reference |
| `robust-solent-loop-osc` | confirm | no | — | 0 s | — | 0.96 s | unreachable |
| `robust-zigzag` | confirm | yes | — | 1.68 h | — | 0.06 s | no_reference |
| `robust-triangle-0.22` | confirm | yes | — | 10.0 min | — | 0.01 s | no_reference |


## By split

- **confirm**: median regret —, worst —, 12/17 feasible

Regret is `(router time − reference time) / reference time` on the same boat and the same wind. Negative would mean the router beat the reference. Chart races without a cached oracle are feasibility and route-time only.
