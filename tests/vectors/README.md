# Test vectors

Plain JSON cases that pin down behavior any whos-listening implementation must share. `tests/test_vectors.py` runs them. All data is made up.

- `routes.json`: a route (provider, model, base_url) and optional settings, and the tier it must get.
- `strip.json`: text a request may carry, and what a Shared route must receive instead.
- `fingerprints.json`: pairs of memory texts and whether a tombstone for one blocks the other.
- `hub/`: cases for hub mode. See [`docs/hub-protocol.md`](../../docs/hub-protocol.md#test-cases) for what each file pins down. `tests/test_hub_vectors.py` checks they are well formed; `tests/test_hub.py` runs them against the hub client and the fake hub.
