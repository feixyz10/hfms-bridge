# hfms-bridge

Transfer models and datasets between **Hugging Face and ModelScope** with minimal local disk usage. File contents stream directly between services; only small metadata checkpoints are saved locally.

- Both directions: `hfms-bridge hf2ms` and `hfms-bridge ms2hf`.
- Parallel file transfers with overall and per-file progress bars.
- Automatic destination creation and incremental updates.
- File-level resume and recovery from interrupted source downloads.
- Python 3.9+, Linux, macOS and Windows.

## Install

```bash
pip install hfms-bridge
# Or install as an isolated command:
uv tool install hfms-bridge
```

## Transfer

From Hugging Face to ModelScope:

```bash
hfms-bridge hf2ms org/source user/destination --ms-token "$MODELSCOPE_API_TOKEN"
```

From ModelScope to Hugging Face:

```bash
hfms-bridge ms2hf org/source user/destination --hf-token "$HF_TOKEN"
```

The destination token is required explicitly and needs write/create permission. Public sources normally need no token. For private sources, pass `--hf-token` or `--ms-token`, or set the source token environment variable (`HF_TOKEN` or `MODELSCOPE_API_TOKEN`).

In PowerShell, use `$env:HF_TOKEN` or `$env:MODELSCOPE_API_TOKEN` instead of the shell variables above.

## Common options

```bash
# Preview without downloading payloads or modifying repositories/cache
hfms-bridge ms2hf org/source user/destination --hf-token "$HF_TOKEN" --dry-run

# Select files and concurrency
hfms-bridge hf2ms org/source user/destination --ms-token "$MODELSCOPE_API_TOKEN" \
  --workers 4 --include '*.safetensors' '*.json' --exclude 'original/*'

hfms-bridge hf2ms --help
hfms-bridge ms2hf --help
```

`--repo-type auto` checks the source's model and dataset namespaces and selects the unique accessible match. If both exist, choose `--repo-type model` or `--repo-type dataset` explicitly. The destination uses the same type.

`--hf-revision` defaults to `main`; `--ms-revision` defaults to `master`. Source revisions can be selected independently. A missing destination is created on its default branch; other destination branches must already exist. `--visibility auto|public|private` controls new repositories; auto creates private destinations for private, gated or unknown-visibility sources.

Existing identical files are skipped; different content is overwritten by default. Choose `--on-conflict error` or `--on-conflict skip` to change that behavior. Files unrelated to the transfer are not deleted.

## Resume and resource usage

If `HF_ENDPOINT` or `--hf-endpoint` is `https://hf-mirror.com`, `ms2hf` sends destination requests directly to official HF (`https://huggingface.co`) to avoid upload redirects. Official HF must be reachable. Existing tasks can resume using the same command and cache; `hf2ms` downloads still use the mirror.

Run the same command again after an error or interruption. Completed files are compared and skipped when identical. Source file versions are pinned for unfinished tasks. Changing `--workers` or `--progress` does not discard progress. `--restart` resolves the source again; it does not force identical remote blobs to upload again.

No payload is cached on disk. Metadata checkpoints default to `~/.cache/hfms-bridge`, with a `256MiB` budget; change this with `--cache-dir` and `--cache-max-size`. Old completed tasks are evicted first, and active tasks are protected. Memory scales mainly with worker count and read buffers (`--chunk-size`, default `1MiB`).

Interrupted source reads can continue through HTTP Range while the process is running. MS supports this path for LFS files; ordinary MS files restart their read. HF uploads can use server-negotiated multipart uploads, with each part streamed directly. **Destination upload failures and process restarts do not support file-internal resume:** unfinished files may restart from byte zero. Complete remote blobs may still be reused.

For ordinary HF destination files, checking equality may require another source read. Large ordinary files are committed using streaming Base64. MS repository metadata files have a 1MiB inline limit. Network, permission, service file-size and rate limits still apply.


Source and developer documentation: [English](https://github.com/feixyz10/hfms-bridge/blob/main/README.md), [简体中文](https://github.com/feixyz10/hfms-bridge/blob/main/README_CN.md).

## Download to a local directory

`hfms-download` downloads from either hub using the same package:

```bash
hfms-download hf Qwen/Qwen3-0.6B --local-dir ./model
hfms-download ms owner/repo --local-dir ./models --layout repo
hfms-download hf owner/repo config.json --local-dir ./model
hfms-download ms --help
```

Choose `--local-dir` or `--preset` explicitly; there is no global-cache fallback. The default layout writes directly into `--local-dir`. `--layout repo` appends `owner/repository`, preserving all subdirectories inside the repository.

The optional `--preset` flag selects an existing base directory from `HF_MODELS` for models or `HF_DATASETS` for datasets and appends `owner/repository`:

```bash
hfms-download ms owner/repo --preset
```

Only the selected type's variable is required. An unset/empty variable or a nonexistent directory is an error. The preset is not enabled automatically and cannot be combined with `--local-dir` or `--layout`.

Use `--repo-type`, `--revision`, `--include` and `--exclude` to select content. Public sources need no token; `--token` defaults to `HF_TOKEN` or `MODELSCOPE_API_TOKEN`. `--endpoint` supports source endpoint overrides, including an HF download mirror. `--max-workers` defaults to four concurrent files, with sequential reads within each file. `--dry-run` produces a JSON plan without downloading payloads or writing local state.

Partial files and metadata are stored beside the final repository directory: `./model` uses `./.model.hfms-download`. Uploading only `./model` excludes these internal files. The two directories must be on the same filesystem. Run the same command after interruption to resume unfinished HF/MS LFS files with validated Range responses; ordinary MS files restart. Completed files are verified and moved into place, and identical existing files are skipped. Unknown or modified local files require `--force-download` to replace them. Updating an existing file temporarily needs space for the old and new versions.

Unfinished tasks retain their source versions. Successfully completed tasks check the current source version on the next run. `--force-download` resolves versions again and discards old partials. State binds the destination to its repository, revision argument and endpoint. No token is stored in download state. Use `--quiet` to suppress progress and informational logs; successful downloads print the destination path.
