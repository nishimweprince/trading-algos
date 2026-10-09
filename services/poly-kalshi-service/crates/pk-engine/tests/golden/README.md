# Golden journals

`tape.txt.gz` is a synthetic tape (4 assets × 8 windows, ~133k lines) and
`base.jsonl` / `offset_depth_lag.jsonl` are the journals the **Python** engine
(`role="full"`, default shadow variants) wrote on it at build `1286ad5`, the last
build with the Python service. `make_golden.py` made all three and documents both
configurations; it imported the Python package, so it cannot run once that is
deleted, and it does not need to: the files are the reference.

`tests/parity.rs` replays the tape through the Rust engine and requires the same
records, in the same order, field by field (numbers within 1e-9; `sec_into`,
derived from a float division of a nanosecond clock, within 1 ms).

The last line of each journal (`"type": "final"`) is not a journal record: it
holds the engine's counters and the ledger snapshot after a final line 200 s
past the tape's end.
