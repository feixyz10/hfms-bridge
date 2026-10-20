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
