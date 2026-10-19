# hf2ms

Transfer Hugging Face models and datasets to ModelScope without downloading the entire repository to disk first.

`hf2ms` streams file contents through memory, uploads multiple files in parallel, and resumes interrupted tasks at the file level. It can create a missing destination repository and update an existing one.

- **Low disk usage:** file contents are not cached locally; only small progress records are saved.
- **Parallel uploads:** transfer four files at a time by default, with configurable concurrency.
- **Automatic resume:** rerun the same command to skip files already committed with identical content.
- **Live progress:** see the total file count and a temporary progress bar for each active file.
- **Models and datasets:** automatically detect the source repository type or choose it explicitly.

Requires Python 3.9+ on Linux, macOS, or Windows.

## Install

Install the command in an isolated environment with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install hf2ms
```

Or install it into your Python environment with pip:

```bash
pip install hf2ms
```

Check the installation:

```bash
hf2ms --version
hf2ms --help
```

## Quick start

Pass the Hugging Face source and ModelScope destination as `owner/repository`:

```bash
hf2ms org/source user/destination --ms-token "$MODELSCOPE_API_TOKEN"
```

`--ms-token` is required. Use a ModelScope token that can write to the destination or create it. The environment variable above is a convenient way to pass the token; hf2ms does not read it automatically.

Public Hugging Face repositories usually need no HF token. For private or gated repositories, use an authorized token with `--hf-token`, or set `HF_TOKEN`:

```bash
hf2ms org/private-source user/destination \
  --ms-token "$MODELSCOPE_API_TOKEN" --hf-token "$HF_TOKEN"
```

For Windows PowerShell, use `$env:NAME` to read environment variables:

```powershell
hf2ms org/source user/destination --ms-token $env:MODELSCOPE_API_TOKEN
```

By default, hf2ms reads HF revision `main` and writes MS branch `master`. If the destination is missing, it is created automatically. New destinations follow the source visibility: public sources produce public destinations, while private/gated sources or unknown visibility produce private destinations. Use `--ms-visibility private` to explicitly create a private destination. Existing repository visibility is unchanged.

The default `--repo-type auto` queries both HF model and dataset repositories and uses the unique accessible match. If both exist under the same name, specify `--repo-type model` or `--repo-type dataset`.

## Common tasks

Preview changes without uploading files or creating a destination:

```bash
hf2ms org/source user/destination \
  --ms-token "$MODELSCOPE_API_TOKEN" --dry-run
```

Choose parallelism and progress display:

```bash
hf2ms org/source user/destination \
  --ms-token "$MODELSCOPE_API_TOKEN" --workers 4 --progress auto
```

`Files` counts successfully committed or skipped files. Each active file has a byte progress bar with speed and estimated time remaining. Its bar disappears when processing finishes, and the row is reused. Use `--workers 1` for serial transfers, `--progress off` for plain logs, or `--progress on` to force progress bars. By default, bars appear only when stderr is a terminal.

Select files with quoted glob patterns; exclusions take priority:

```bash
hf2ms org/source user/destination \
  --ms-token "$MODELSCOPE_API_TOKEN" \
  --include '*.safetensors' '*.json' 'README.md' --exclude 'original/*'
```

Select a source version and an existing destination branch:

```bash
hf2ms org/source user/destination \
  --ms-token "$MODELSCOPE_API_TOKEN" \
  --hf-revision v1.2 --ms-revision release
```

New destinations start on `master`; other MS branches must already exist. HF and MS revision names do not need to match.

## Resume an interrupted transfer

**Run the same command again. No `--resume` flag is needed.** Already committed files with identical remote content are skipped. You can change `--workers` or `--progress`, including when resuming an older serial task, without resetting progress.

Keep the original repositories, revisions, filters, conflict policy, endpoints, visibility setting, and cache directory. An unfinished task continues its original HF snapshot even if the source branch has changed. Do not add `--restart` unless you intend to discard local progress and resolve the source revision again.

Recovery after restarting the process is **file-level**: an incomplete file restarts from the beginning. During a running transfer, interrupted HF reads can reconnect with HTTP Range while keeping the current MS upload alive; byte progress does not reset. This requires a valid range response from HF and an MS connection that remains open. If range recovery fails or the MS upload connection breaks, the file restarts from byte zero. If a complete file blob was uploaded but its commit failed, hf2ms can reuse that blob when ModelScope still has it.

Progress records are stored in `~/.cache/hf2ms/` by default, with a 256 MiB cache budget. Completed idle task records are removed first when space is needed; unfinished tasks are preserved. If the budget cannot accommodate them, increase `--cache-max-size` and rerun the command.

Errors identify the failing stage (`HF read`, `MS negotiate`, `MS upload`, `MS verify`, or `MS commit`). Each source-reading pass allows at most `--attempts - 1` reconnections, with bounded backoff; this budget is separate from the existing whole-file retry budget. File payloads remain off disk.

## Useful options

| Option | Default | Purpose |
| --- | --- | --- |
| `--repo-type` | `auto` | Detect the source type, or choose `model` / `dataset` |
| `--hf-revision` | `main` | HF branch, tag, or commit |
| `--ms-revision` | `master` | Existing MS branch; `master` also works for a new repository |
| `--ms-visibility` | `auto` | Visibility of a newly created destination |
| `--workers` | `4` | Number of concurrent file transfers |
| `--progress` | `auto` | `auto`, `on`, or `off` |
| `--on-conflict` | `overwrite` | Overwrite different/unverified content, `skip` it, or report an `error` |
| `--attempts` | `3` | Maximum attempts per operation, including the first |
| `--cache-dir` | `~/.cache/hf2ms/` | Location of progress records |
| `--cache-max-size` | `256MiB` | Budget for progress records |
| `--dry-run` | Off | Preview changes without remote writes or file-content downloads |

Use `hf2ms --help` for all options, including timeouts, read buffer size, and custom endpoints. `HF_ENDPOINT` is supported; a mirror can redirect requests to the official HF service, so those requests still need access to that service.

## What to expect

- Existing identical files are skipped. Different or unverified content is overwritten by default; destination-only files are retained.
- Each file is committed separately. If one file fails after retries, no new files are scheduled; other active files finish before the command exits with an error. Rerun to resume.
- File contents stay off disk, but memory usage grows with the number of workers. Large files without an HF SHA-256 need two streaming reads: one to calculate the hash and one to upload.
- A byte bar at 100% means the uploader consumed the bytes. The file is not complete until the subsequent commit succeeds.
- Automatic branch/tag creation and destination-side byte-offset resume are not supported. Repository metadata files such as `README.md` larger than 1 MiB are not supported. Some very large HF files may be unavailable through HTTP downloads.

## Documentation and license

The source distribution includes [README.md](https://github.com/feixyz10/hf2ms/blob/main/README.md) (English) and [README_CN.md](https://github.com/feixyz10/hf2ms/blob/main/README_CN.md) (Chinese), covering the full option reference, Python API, implementation details, and local development instructions.

hf2ms is distributed under the MIT License; see [LICENSE](https://github.com/feixyz10/hf2ms/blob/main/LICENSE). Transferred models and datasets retain their own licenses.
