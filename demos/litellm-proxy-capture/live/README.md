# Live demo kit

Tooling for running the WRIG-1482 demo **by hand**, in several terminals, with
everything MemoryHub does visible on screen. `demo.sh` runs the same story
unattended; this is the version you can talk over.

| file | what it is |
|---|---|
| `check.sh` | preflight: packages, whether the ONNX embedding model is really there, whether the extraction endpoint answers, whether the port is free. The personal edition has no `memoryhub doctor` (that command belongs to the cluster CLI). |
| `env.sh` | shared environment, sourced in every terminal. Isolated database in `out/live-data`, `MEMORYHUB_CAPTURE_SINK=local`, extraction every 2 user turns. Deliberately does **not** source `.env`, so nothing points at the cluster and no keys appear on screen. It also links the real ONNX model cache into the isolated data dir — the model lives under `$XDG_DATA_HOME/memoryhub/models`, so an isolated `XDG_DATA_HOME` would otherwise hide it and silently fall back to mock embeddings. |
| `proxy.sh` | starts LiteLLM with the capture callback. `./live/proxy.sh` uses canned answers; `./live/proxy.sh real` reads only `POC_MODEL` / `POC_MODEL_API_KEY` / `ANTHROPIC_API_KEY` from `.env`. |
| `watch.py` | follows `out/live-observations.jsonl` and prints one readable line per intercepted call: session key and how it was derived, delta size, what was stored, whether extraction ran. |
| `peek.py` | read-only look inside the SQLite database — threads and cursor (`state`), stored messages (`thread`), the exact window the next extraction will read (`next`), memories with provenance (`memories`), and every create/update/skip with its similarity score (`decisions`). No backend start-up, so it answers instantly. |

Run `source live/env.sh && ./live/check.sh` once before recording.

Suggested layout: terminal 1 `proxy.sh`, terminal 2 `agent.py --interactive`,
terminal 3 `watch.py`, terminal 4 `peek.py`.

Semantic search needs the embedding model and stays in `verify.py`:

```bash
python verify.py search "dark mode"
```
