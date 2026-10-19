# hf2ms

[User guide](docs/README.md) | **English** | [简体中文](README_CN.md)

This document contains the full option reference, implementation details, recovery semantics, and development notes.

Stream Hugging Face models and datasets to ModelScope without saving large file contents to disk. Supports parallel file transfers, file-level recovery, automatic destination repository creation, and incremental updates. Requires Python 3.9+ and provides a pure Python wheel for Linux, macOS, and Windows.

## Installation and quick start

```bash
uv sync
uv run hf2ms org/source user/destination --ms-token "$MODELSCOPE_API_TOKEN"
```

`--ms-token` **must be passed explicitly** and must have permission to write to or create the destination repository. Even when `MODELSCOPE_API_TOKEN` is set, pass it to the option as shown above. `--hf-token` is optional and defaults to `HF_TOKEN`; public sources usually need no token, while private or gated sources require an authorized token.

```bash
# Private or gated HF source
uv run hf2ms org/source user/destination \
  --ms-token "$MODELSCOPE_API_TOKEN" --hf-token "$HF_TOKEN"

# Read-only preview
uv run hf2ms org/source user/destination \
  --ms-token "$MODELSCOPE_API_TOKEN" --dry-run

# Select revisions and filter paths
uv run hf2ms org/source user/destination \
  --ms-token "$MODELSCOPE_API_TOKEN" \
  --hf-revision v1.2 --ms-revision release \
  --include '*.json' '*.safetensors' --exclude 'original/*'
```

Windows PowerShell:

```powershell
$env:MODELSCOPE_API_TOKEN = 'your MS token'
hf2ms org/source user/destination --ms-token $env:MODELSCOPE_API_TOKEN
```

Install the wheel to use the `hf2ms` console script directly:

```bash
uv tool install ./dist/hf2ms-0.3.1-py3-none-any.whl
hf2ms --help
# Or install into an existing Python environment:
pip install ./dist/hf2ms-0.3.1-py3-none-any.whl
```

HTTPS requires a Python build using OpenSSL 1.1.1+. Older macOS/Xcode Python builds may use LibreSSL; a uv-managed Python is recommended, for example `uv sync --python 3.11`.

## Options

| Option | Default / behavior |
|---|---|
| `SOURCE DESTINATION` | Required; both use `owner/repository` |
| `--ms-token TOKEN` | **Required**; does not implicitly read an MS token environment variable |
| `--hf-token TOKEN` | Optional; defaults to `HF_TOKEN`, with the explicit option taking precedence |
| `--repo-type auto\|model\|dataset` | `auto`: query HF to identify the source as a model or dataset; select the unique match, or require an explicit type if both exist |
| `--hf-revision REVISION` | `main`; source branch, tag, or commit |
| `--ms-revision BRANCH` | `master`; writable destination branch, independent of the HF revision |
| `--ms-visibility auto\|private\|public` | `auto`; applies only when creating a destination repository |
| `--on-conflict overwrite\|error\|skip` | `overwrite`; handles different or unverified content at an existing path |
| `--include PATTERN [PATTERN ...]` | Include globs; repeatable |
| `--exclude PATTERN [PATTERN ...]` | Exclude globs; repeatable and takes precedence over include |
| `--cache-dir PATH` | `~/.cache/hf2ms/`; supports `~` expansion |
| `--cache-max-size SIZE` | `256MiB`; minimum `64KiB` |
| `--restart` | Ignore local progress and resolve the source again; identical remote content can still be reused |
| `--dry-run` | Read-only preflight; outputs a per-file plan and a JSON summary |
| `--chunk-size SIZE` | `1MiB`; streaming read buffer size per active file |
| `--workers N` | `4`; concurrent file transfers, `1` for serial mode |
| `--attempts N` | `3`; maximum attempts per operation |
| `--progress auto\|on\|off` | `auto`; one file counter and a temporary byte bar per active file; disabled for redirected stderr |
| `--timeout SECONDS` | `120`; socket read timeout |
| `--hf-endpoint URL` | `HF_ENDPOINT` or `https://huggingface.co` |
| `--ms-endpoint URL` | `MODELSCOPE_ENDPOINT` or `https://modelscope.cn` |
| `--version` | Print the package version |

Sizes accept integer bytes or KiB/MiB/GiB and KB/MB/GB suffixes. Patterns use case-sensitive `fnmatchcase` matching against repository-relative paths, not regular expressions. Quote patterns to prevent shell expansion. Put the source and destination positional arguments before multi-value filter options.

### What `--repo-type auto` means

`auto` detects the repository type by querying HF, rather than guessing from file extensions. It checks both the model and dataset namespaces for the supplied repository ID:

- Exactly one accessible type: use it automatically, and use the same type for the destination.
- Both a model and a dataset exist under the same `owner/repository`: report ambiguity and require `--repo-type model` or `--repo-type dataset`.
- Neither can be found: report that the repository is missing or inaccessible with the current token.
- HF may return 401/403 for a missing or hidden namespace. Auto checks both namespaces and selects the unique successful match. If neither succeeds, authentication/permission errors are reported. Network and server errors still abort detection.

Private repositories may hide their existence with 401/403/404 responses, so detection is limited to what the current HF token can see. An explicit repository type queries only that namespace.

## Repositories, revisions, and updates

- Missing destination repositories are created automatically. With `--ms-visibility auto`, a public source creates a public destination; a private/gated source or unknown source visibility creates a private destination. Existing repository visibility is unchanged.
- Creation uses HF `cardData.license` when a single license string is available; otherwise it submits ModelScope's `Other` value rather than inventing Apache/MIT licensing. License files are transferred normally. A license value rejected by the server produces an error.
- New repositories start on `master`. To target another branch, the repository and branch must already exist. This version does not create branches or tags automatically.
- Destination file size and SHA-256 are compared with the source. Identical content is skipped; different content is updated by default through a new commit. Unverified equality follows the conflict policy. An existing repository or different cross-platform commit IDs are not errors by themselves.
- `--on-conflict error` checks selected paths before uploading and rejects any conflict, including unverified equality. A real run may stream the source to compute a missing SHA-256; dry-run never downloads file contents.
- `--on-conflict skip` preserves different content at existing paths while uploading missing files.
- Destination-only files are retained. File/directory conflicts never implicitly delete directories.
- Each file is committed separately; there is no repository-wide atomic transaction. Preflight cannot lock the remote branch, so avoid concurrent writers to the same destination branch.

## Recovery and cache

Run **the same command** after an interruption to resume; no `--resume` flag is needed. SQLite checkpoints are named by a hash of the task identity and contain metadata only, never tokens, signed URLs, or file contents. Repositories, revisions, filters, and conflict policies have separate state. Concurrent processes cannot run the same task.

- Unfinished tasks keep their pinned HF commit even if the source branch advances.
- Running a completed task again resolves the source revision anew to pick up updates.
- Current remote files are checked on each run. External edits or deletions are handled according to the conflict policy rather than trusting local completion records alone.
- If a blob was uploaded but its commit failed, renegotiation reuses the complete blob when the server still retains it.
- After a process restart or MS upload disconnect, an incomplete file is transferred again from the beginning. Within a running upload, HF read interruptions can recover through Range requests; see the HF connection recovery section.
- `--restart` clears the task's local progress and resolves the source again; it does not force the server to accept identical blob bytes again.

When the cache exceeds its budget, the least recently used **completed, unlocked task databases** are removed first. Running and unfinished tasks are protected. If insufficient space can be reclaimed, checkpoints are preserved and the command fails; increase the budget or manually remove unwanted task state. The budget is checked at file completion and task boundaries, so the current SQLite transaction and journal can temporarily exceed it; this is not a hard filesystem quota. Cache cleanup never deletes remote content.

`--dry-run` does not create cache directories, prune cache, modify checkpoints, create repositories, or call upload negotiation/PUT/commit. With an unfinished checkpoint it previews the pinned source version; `--restart --dry-run` previews the current source version. Files whose equality cannot be established from metadata are marked `unknown`. The JSON `bytes` field is the total selected file size, not guaranteed network traffic. `would_commit`, `would_skip`, and `would_error` count planned file outcomes under the current policy. A successful preview does not prove upload permission.

## Memory and protocol limitations

Large files flow from an HF HTTP response into a ModelScope PUT through a bounded read buffer, 1 MiB by default. This buffer is not a server-side multipart chunk. HF LFS SHA-256 metadata avoids a preliminary hashing pass; large files without a hash require a streaming hash pass followed by a streaming upload pass, both without writing contents to disk.

Non-LFS small files (≤1 MiB) are encoded and committed in memory. README.md, .gitattributes, and configuration.json remain regular files and fail if they exceed the inline limit. Memory also includes HTTP/TLS buffers, directory pages, and small-file Base64/JSON, so the read buffer size is not a process RSS limit. Each worker caches at most one destination directory with 3000 entries. ModelScope model directories that cannot be listed completely fail explicitly instead of treating truncated results as missing files.

Network failures, HTTP 429, and selected 5xx responses receive bounded retries. Large-file length and SHA-256 are checked during streaming. Serial, per-file commits can encounter rate limits for repositories containing many small files. HF HTTP resolve may restrict very large files; this version does not reconstruct Xet files or fall back to downloading whole files to disk.

## Parallel transfers and progress

```bash
uv run hf2ms org/source user/destination \
  --ms-token "$MODELSCOPE_API_TOKEN" --workers 4
```

`--workers` defaults to **4**. Each thread handles one file at a time using its own HTTP sessions. The scheduler keeps at most that many files in flight instead of queuing the entire repository. Large file contents still flow directly from HF into MS without temporary payload files. Memory for read buffers, HTTP sessions, small inline files, and directory metadata grows with the worker count, not with total payload size. Increasing concurrency helps when latency or individual transfers leave bandwidth unused; it cannot exceed network or service limits.

On a terminal, `Files` tracks successfully committed or skipped files against the exact filtered total. Each active file has its own byte bar with speed, ETA, and phase. Completed file bars disappear and their rows are reused. Hashing and upload are separate phases; a whole-file retry resets that file's byte count; successful HF Range recovery does not. A byte bar reaching 100% means the uploader consumed the bytes, not that MS committed the file: the row remains through `Waiting to commit` and `Committing`. Reused blobs do not invent transferred bytes. `--progress off` uses ordinary logs, `--progress on` forces bars, and `auto` disables bars for redirected stderr. Progress uses stderr; dry-run JSON stays on stdout.

Only branch commits and short SQLite operations are serialized; download/upload streaming holds neither lock. Each file has independent retries. If a file exhausts retries, no further files are scheduled, active transfers finish and save their checkpoints, and the command exits unsuccessfully. Failed files do not advance the total. Ctrl-C requests cancellation at the next stream read, retry wait, or pre-commit check. An already-running network call must return or time out first; a commit already sent can still finish and be checkpointed. Repeated Ctrl-C during shutdown does not close clients or release the checkpoint lock while workers remain active. Commit order can differ from source listing order. Small-file workloads may still be limited by per-file serial commits.

**Existing unfinished serial tasks resume without migration.** Run the same command with `--workers 4`; keep the repository, revisions, filters, conflict policy, endpoints, visibility, and cache directory unchanged. Do not add `--restart`. The SQLite schema and task identity are unchanged: neither `--workers` nor `--progress` participates in the identity. The unfinished task retains its pinned HF commit, and already committed identical remote files are skipped. Switching back to `--workers 1` works the same way. An unfinished file restarts at byte zero.

Threads suit this workload: blocking [I/O releases the GIL](https://docs.python.org/3/c-api/threads.html), and [hashlib releases it for updates larger than 2047 bytes](https://docs.python.org/3/library/hashlib.html). The default buffer is 1 MiB. Processes would add interpreter memory, IPC for progress/checkpoints, and platform-specific startup complexity while still needing serial commits. Hugging Face's [snapshot downloader also exposes concurrent file threads](https://huggingface.co/docs/huggingface_hub/en/package_reference/file_download#huggingface_hub.snapshot_download). hf2ms keeps the same general progress convention while streaming onward to MS.

## Python API

```python
import os
from hf2ms import Transfer, TransferOptions

result = Transfer(
    TransferOptions(
        source="org/model",
        destination="user/model",
        repo_type="auto",
        hf_revision="main",
        ms_revision="master",
        on_conflict="overwrite",
        workers=4,
    ),
    ms_token=os.environ["MODELSCOPE_API_TOKEN"],
    hf_token=os.getenv("HF_TOKEN"),
).run()
print(result)
```

Version 0.2.2 handles metadata API redirects within the same origin, between explicit official domain aliases, and from `hf-mirror.com` to official Hugging Face (one-way). Redirected POST requests preserve their method and body only for 307/308; consumed upload streams are never automatically replayed. HTTP errors include the method and hostname without exposing signed query parameters. Both HF namespaces are checked even when one returns 401/403.

Version 0.2.1 renames the CLI option `--visibility` to `--ms-visibility`. The Python API's `visibility` field and existing cache identity remain unchanged.

Version 0.2.0 replaced `--revision`, `--destination-revision`, `--state`, and `--recheck` with `--hf-revision`, `--ms-revision`, `--cache-dir`, and `--restart`, respectively, with corresponding Python option changes. Legacy `.hf2ms/state.sqlite3` files are not migrated automatically; complete remote blobs can still be reused.

## Development and validation

```bash
uv sync --locked
uv run pytest
uv run ruff check .
uv build
```

Tests use local HTTP servers to exercise actual streaming PUTs, interrupted transfers, repository creation, conflict policies, previews, cache protection, and token arguments. CI is configured for Linux/macOS/Windows and Python 3.9–3.14. A live authenticated dataset transfer has verified reuse of an existing LFS blob, a successful file commit, and matching destination size/SHA-256. Full-repository live validation and live streaming PUT validation remain pending. The HF mirror may redirect metadata requests to the official HF host, which must be reachable from your network.

Protocol references: [ModelScope REST/LFS](https://github.com/modelscope/modelscope_hub/blob/main/src/modelscope_hub/_legacy_api.py), [HF Hub API](https://huggingface.co/docs/hub/api).

Version 0.2.3 also retries missing, duplicate, or malformed LFS batch object lists, up to `--attempts` (default: 3, including the first attempt). Exhausted retries stop the run with a nonzero exit code and preserve progress. Run the same command again without `--restart`: identical committed files are skipped; an incomplete file restarts from byte zero. Missing or malformed object-list fields remain errors; see the 0.2.5 correction below for explicit empty lists.

Version 0.2.4 fixes resuming datasets whose revision-list endpoint returns 404. For the default `master` only, hf2ms verifies access using the file-tree endpoint and leaves write authorization to the commit endpoint. Other branches still require a working revision listing; 401/403 and tree errors are not ignored. ModelScope API errors now include the original route without query parameters or signed redirect URLs.

Version 0.2.5 fixes legacy ModelScope LFS reuse: an explicit `objects: []` means no blob upload is needed, matching the official SDK. hf2ms then commits the file reference and records completion only after commit succeeds. Missing/malformed object lists and nonempty lists with duplicate or mismatched OIDs still fail.

Version 0.2.6 adds two progress rows: `Files` counts successfully committed and skipped files; the current file shows bytes, speed, and ETA, then disappears after processing. Hashing is a separate phase that resets before upload. `HF -> MS` counts bytes consumed by the streaming uploader (not server acknowledgements); `Committing` remains until commit succeeds. Small inline files show `HF read` then `Committing`. Reused blobs do not invent transferred bytes. Retries reset the current file, and failed files never advance the file counter. Warnings and errors remain visible without keeping completed file bars.

Use `--progress on` to force display or `--progress off` for ordinary per-file logs. Progress goes to stderr; dry-run JSON on stdout is unchanged. Changing this option does not reset checkpoints. To obtain an exact filtered file total without retaining all metadata, enabled progress performs an extra streaming metadata-list pass; file contents are never cached on disk.

Version 0.3.0 adds bounded file-level parallelism (`--workers`, default 4), isolated worker HTTP sessions, serial commits/checkpoint writes, and reusable progress rows per active file. Existing unfinished checkpoints remain compatible.

Version 0.3.1 fixes interruption cleanup and cache-budget checks under concurrency: repeated Ctrl-C cannot close worker resources prematurely, and the current checkpoint is checked between SQLite transactions so a temporary rollback journal cannot cause a false budget failure.

## HF connection recovery (0.3.2)

Interrupted HF reads reconnect at the last byte returned to the uploader, using the pinned commit and HTTP Range. The response must be HTTP 206 with the exact Content-Range and matching length (when supplied), without content encoding. The running MS PUT, SHA-256 state, and progress count are preserved. A read that raises may have consumed internal bytes; those bytes are fetched again rather than skipped. Payloads remain off disk, with bounded reads per worker.

Each source-reading pass (hashing, inline read, or upload) permits at most `--attempts - 1` reconnections in total, using cancellable exponential backoff. Invalid Range responses or exhausted recovery fall back to the existing file retry policy. A disconnected MS upload still requires a whole-file retry; a stopped process resumes at file granularity. Errors identify `HF read`, `MS negotiate`, `MS upload`, `MS verify`, or `MS commit`, without signed URLs or tokens.

Existing checkpoints need no migration: the SQLite schema, task identity, and pinned-commit rules are unchanged. Reuse the original command and cache directory without `--restart`.

As checked on 2026-10-19, the public ModelScope SDK's [LFS transport](https://github.com/modelscope/modelscope_hub/blob/main/src/modelscope_hub/_legacy_api.py) negotiates a whole-blob URL and sends one PUT. Its [upload tracker](https://github.com/modelscope/modelscope_hub/blob/main/src/modelscope_hub/_upload.py) tracks files, not acknowledged byte ranges. The [OpenAPI file upload](https://github.com/modelscope/modelscope_hub/blob/main/src/modelscope_hub/_openapi.py) uses a single form upload (documented there for files up to 5 MiB); multipart/form-data is not resumable multipart object upload. No supported part/session/offset API for this model/dataset LFS path was identified in these public interfaces. This is not a claim about unpublished backend capabilities. Destination-side byte resume remains unavailable in hf2ms; enabling it requires a verified server protocol. No live authenticated MS upload was performed for this investigation.
