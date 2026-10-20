# hfms-bridge

[User guide](docs/README.md) | **English** | [简体中文](README_CN.md)

Stream models and datasets between HuggingsFace (HF) and ModelScope (MS), without saving file payloads to disk. Python 3.9+, Linux, macOS and Windows.

## Development quick start

```bash
uv sync --locked
uv run hfms-bridge hf2ms org/source user/destination --ms-token "$MODELSCOPE_API_TOKEN"
uv run hfms-bridge ms2hf org/source user/destination --hf-token "$HF_TOKEN"
uv run hfms-bridge ms2hf --help
```

The destination token must be explicit. Optional source tokens default to `HF_TOKEN` or `MODELSCOPE_API_TOKEN`. Private or gated sources require authorized read access.


## Options and behavior

Both directions share these options:

| Option                                 | Default / behavior                                                                                                           |
| -------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `SOURCE DESTINATION`                 | Required `owner/repository` IDs                                                                                            |
| `--repo-type auto\|model\|dataset`     | Probe the source model and dataset namespaces; select the unique accessible match; require an explicit choice when ambiguous |
| `--hf-token TOKEN`                   | Required for `ms2hf`; otherwise optional, defaults to `HF_TOKEN`                                                         |
| `--ms-token TOKEN`                   | Required for `hf2ms`; otherwise optional, defaults to `MODELSCOPE_API_TOKEN`                                             |
| `--hf-revision REV`                  | `main`; source revision or existing destination branch                                                                     |
| `--ms-revision REV`                  | `master`; source revision or existing destination branch                                                                   |
| `--visibility auto\|public\|private`   | New destination only; auto mirrors public sources, otherwise private                                                         |
| `--include GLOB [GLOB ...]`          | Repeatable case-sensitive repository-relative globs                                                                          |
| `--exclude GLOB [GLOB ...]`          | Repeatable, takes precedence over include                                                                                    |
| `--on-conflict overwrite\|error\|skip` | `overwrite`; different or unverifiable destination content                                                                 |
| `--workers N`                        | `4`; concurrent file transfers, `1` for serial                                                                           |
| `--attempts N`                       | `3`; maximum attempts per operation, including the first                                                                   |
| `--chunk-size SIZE`                  | `1MiB`; streaming buffer per active file                                                                                   |
| `--timeout SECONDS`                  | `120`; socket timeout                                                                                                      |
| `--progress auto\|on\|off`             | `auto`; enabled on terminal stderr                                                                                         |
| `--cache-dir PATH`                   | `~/.cache/hfms-bridge`                                                                                                     |
| `--cache-max-size SIZE`              | `256MiB`; minimum `64KiB`, oldest completed tasks evicted first                                                          |
| `--restart`                          | Ignore local progress and resolve the source again; identical remote data can still be reused                                |
| `--dry-run`                          | Read-only plan, no payload downloads, remote writes or checkpoint changes                                                    |
| `--hf-endpoint URL`                  | `HF_ENDPOINT` or `https://huggingface.co`                                                                                |
| `--ms-endpoint URL`                  | `MODELSCOPE_ENDPOINT` or `https://modelscope.cn`                                                                         |

Use `hfms-bridge --version` for the version. Sizes accept bytes, KiB/MiB/GiB or KB/MB/GB. Quote globs and place positional arguments before multi-value filters. A missing/inaccessible source produces an error; auto detection cannot discover repositories hidden from the current token.

Missing destinations are created automatically on their platform's default branch. Other destination branches must already exist. Existing repositories are updated through per-file commits; unrelated destination files are not deleted. Visibility is only set on creation. MS creation uses the source license when available, otherwise `Other`; license and README files are transferred normally. HF repository creation does not synthesize a license card.

Identical destination content is skipped using SHA-256 or, for ordinary HF files, Git blob SHA-1. Ordinary HF comparisons may require reading the source again. Dry runs do not download data to establish missing hashes, so some equality checks remain unknown. `--on-conflict error` checks all selected paths before remote writes; it does not provide an atomic transaction against concurrent external writers.

## Architecture and recovery

For `ms2hf`, configuring `https://hf-mirror.com` as the HF endpoint routes destination requests directly to `https://huggingface.co`. This avoids replaying streamed commits after a mirror redirect and requires access to official HF. The configured endpoint remains part of the checkpoint identity, so existing mirror-configured tasks resume with the same command. `hf2ms` source downloads still use the configured mirror.

`hubs/hf.py` and `hubs/ms.py` implement source and destination adapters. `transfer.py`, `http.py`, `progress.py`, and `state.py` share scheduling, retry, checked streaming, progress and checkpoint logic across both directions.

HF sources pin a commit. MS sources store a SQLite manifest containing each file's immutable revision, size and digest; the root listing marker is checked before and after enumeration. An unfinished run reuses this manifest even when the source branch changes. This is a file-revision snapshot, not a server-side atomic repository snapshot. No file payload is stored locally. Metadata disk usage grows with the number of files; the cache budget protects active tasks from eviction and fails if they cannot fit.

Large payloads pass through bounded buffers. SHA-256 is computed while reading and validated against available source metadata before completion is recorded. Missing digests may require an extra source pass. HF preupload determines ordinary/LFS storage; ordinary file commits stream Base64 NDJSON, while LFS uses basic PUT or server-negotiated multipart upload. MS uses its existing LFS PUT/commit protocol. MS repository metadata files have a 1 MiB inline limit.

Source connection loss can resume inside a running upload using a strict HTTP 206 Range response. MS Range recovery is enabled only for LFS files; ordinary MS files restart their read because their Range behavior is unreliable. The extension (`.pt`, `.jsonl`, etc.) does not decide this: the server's LFS metadata does.

A destination upload failure retries the file. HF multipart parts are streamed sequentially, without buffering a whole part or persisting upload sessions; failed parts are not resumed independently. **Process exit or Ctrl-C resumes at file granularity, not within an unfinished file.** Completed blobs may be reused if the destination reports them present. Run the same command without `--restart` to continue; workers and progress settings can change without changing task identity.

Threads overlap network I/O with isolated HTTP sessions. Only commits and short checkpoint operations serialize. Progress has one overall file counter and a temporary row per active file. Byte counts reflect bytes consumed by the uploader, not server acknowledgements. Source Range recovery preserves progress; a whole-file retry resets it. Exhausted retries stop scheduling new files while active workers finish. Ctrl-C waits for active network calls to return or time out before releasing resources.

Credentials are excluded from checkpoints and stripped from cross-origin download redirects. Metadata redirects are restricted to trusted origins; streamed uploads and commits are not blindly replayed on redirects.

## Python API

```python
import os
from hfms_bridge import Transfer, TransferOptions

result = Transfer(
    TransferOptions(
        source="org/source",
        destination="user/destination",
        direction="ms2hf",  # or "hf2ms"
        workers=4,
    ),
    hf_token=os.environ["HF_TOKEN"],
    ms_token=os.getenv("MODELSCOPE_API_TOKEN"),
).run()
```

## Validation and build

```bash
uv sync --locked
uv run ruff check .
uv run pytest
uv build
```

Tests use local HTTP servers for both directions, actual streaming requests, multipart completion, Range recovery, pinned-manifest recovery, conflict handling, cache limits, cancellation and progress. CI covers Python 3.9–3.14 on Linux/macOS/Windows. Public MS model/dataset downloads and HF metadata endpoints are checked separately; local server tests do not replace authenticated end-to-end service validation.

Protocol references: [HF LFS implementation](https://github.com/huggingface/huggingface_hub/blob/main/src/huggingface_hub/lfs.py), [HF commits](https://github.com/huggingface/huggingface_hub/blob/main/src/huggingface_hub/_commit_api.py), [ModelScope REST/LFS](https://github.com/modelscope/modelscope_hub/blob/main/src/modelscope_hub/_legacy_api.py).

## Download repositories locally

The same package also provides `hfms-download`. It uses the shared HF/MS source adapters and network retry policy, and writes directly to local files without a global download cache.

```bash
uv run hfms-download hf Qwen/Qwen3-0.6B --local-dir ./model
uv run hfms-download ms owner/repo --local-dir ./models --layout repo
uv run hfms-download hf owner/repo config.json --local-dir ./model
uv run hfms-download ms owner/repo --preset author
uv run hfms-download hf --help
```

`--local-dir` and `--preset author` are mutually exclusive, and one is required. The default `direct` layout puts repository contents directly in `--local-dir`; `--layout repo` appends `owner/repository`. Internal repository subdirectories are always preserved.

The opt-in `author` preset uses `HF_MODELS` for models or `HF_DATASETS` for datasets and appends the repository ID. Only the variable for the detected repository type is required. It must be nonempty and point to an existing directory; no fallback directory is used. The preset cannot be combined with `--layout`. Both hubs use the same two variables, which are this tool's local-directory convention.

| Option | Behavior |
|---|---|
| `hf` / `ms` | Required source subcommand |
| `repo_id [filenames ...]` | Repository plus optional exact file paths; missing explicit paths are errors |
| `--repo-type auto\|model\|dataset` | Same source-type detection as transfers |
| `--revision REV` | HF `main`, MS `master` by default |
| `--token TOKEN` | Optional source token; defaults to `HF_TOKEN` or `MODELSCOPE_API_TOKEN` |
| `--endpoint URL` | Source endpoint override; defaults to its environment variable or official URL |
| `--include / --exclude` | Repeatable case-sensitive globs; filters also apply to explicit filenames |
| `--max-workers N` | 4 concurrent files by default; each file downloads sequentially |
| `--attempts / --timeout / --chunk-size` | Defaults: 3 attempts, 120-second socket read timeout, 1MiB buffer |
| `--progress auto\|on\|off` | Overall file count plus temporary byte bars |
| `--force-download` | Resolve source again, discard old partials, redownload and permit replacing local files |
| `--dry-run` | Read-only JSON plan; no payload reads or directory/state writes |
| `--quiet` | Hide progress and informational logs; retain warnings/errors |

Recovery state lives in a sibling directory: `./model` has `./.model.hfms-download`; `./models/org/repo` has `./models/org/.repo.hfms-download`. Upload only the final repository directory to avoid including state. The sibling directory must be on the same filesystem as the destination. Do not edit its files; deleting it loses partial-download recovery, but not completed repository files.

A JSON manifest pins source versions for an unfinished run. Per-file metadata identifies the partial content; the `.incomplete` file's actual length is the resume offset. No percentage or per-chunk database updates are needed. On restart, the existing prefix is rehashed and downloaded bytes are appended only after strict Range validation. MS ordinary files restart; MS LFS and compatible HF responses resume. Invalid ranges restart the file within the retry budget. Completed files are validated and atomically moved into place. SHA-256 or Git blob SHA-1 is checked when the source supplies it; otherwise a warning states that only length was verified. Hash checking uses bounded buffers; manifest memory scales with repository file count.

Existing identical files are skipped after verification. Unknown or locally modified files require `--force-download`; previously downloaded, unchanged files can be updated after the source changes. Updates retain the old final file until its replacement is verified, which temporarily requires space for both versions. Successful runs refresh source versions on the next invocation; unfinished runs retain the original snapshot. A destination cannot be reused for another repository, revision argument or endpoint identity without moving/removing its state or choosing a different directory. Unrelated local files are not deleted.

One process owns a destination at a time. Ctrl-C stops workers before releasing the lock and leaves recoverable partials; an active network read may need to time out. Paths that escape the destination, destination symlinks, Windows-reserved names and conflicting file paths are rejected. Download metadata does not store tokens.
