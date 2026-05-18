# Multicore Optimization — Next Steps

Identified after first real run (2026-05-17). Do not implement until current run completes and output is verified.

## Issues to fix

1. **Default workers to `os.cpu_count()`** in `process.py` — the plumbing is there, just not used.

2. **Presidio pool initializer** — `_analyzer`/`_anonymizer` in `pii.py` are globals that get re-created per subprocess. Use `ProcessPoolExecutor(initializer=_init_engines)` to initialize once per worker, not once per file.

3. **Finetune JSONL write race** — all workers currently append to the same `output/finetune/dataset.jsonl`. Fix: each worker writes to a temp file (`dataset.<pid>.jsonl`), merge at the end in `process_directory`.

4. **Review log write race** — same issue as finetune. Same fix: per-worker temp files, merge at end.

5. **Large-file parallelism** — spaCy NER is single-threaded per document, so the 814K-token Advocate record won't benefit from workers. Option: split very large documents (>100K tokens) into sections, process sections in parallel, merge results. Non-trivial but high value for large medical records.

## Order of work
Fix 3+4 first (correctness), then 2 (performance), then 1 (default), then 5 (optional).
