"""The hfms_bridge console entry point."""

import argparse
import json
import logging
import os
import re
import sqlite3
from pathlib import Path

import requests
from filelock import Timeout

from . import __version__
from .http import TransferError
from .transfer import Transfer, TransferOptions

log = logging.getLogger("hfms-bridge")


def byte_size(value):
    match = re.fullmatch(r"([0-9]+)\s*(B|KiB|MiB|GiB|KB|MB|GB)?", value, re.IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError("Use bytes or an integer with KiB/MiB/GiB (e.g. 256MiB)")
    unit = (match[2] or "B").upper()
    multiplier = {
        "B": 1,
        "KIB": 1024,
        "MIB": 1024**2,
        "GIB": 1024**3,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
    }[unit]
    result = int(match[1]) * multiplier
    if result <= 0:
        raise argparse.ArgumentTypeError("Size must be positive")
    return result


def nonempty_token(value):
    if not value.strip() or any(c in value for c in "\r\n"):
        raise argparse.ArgumentTypeError("Token must be non-empty and contain no line breaks")
    return value


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hfms-bridge",
        description="Stream models and datasets between Hugging Face (HF) and ModelScope (MS) "
        "without saving file contents to disk.",
    )
    parser.add_argument(
        "--version", action="version", version=__version__, help="Show the package version and exit"
    )
    commands = parser.add_subparsers(
        dest="direction",
        required=True,
        title="transfer directions",
        help="Choose source and target",
    )
    for direction in ("hf2ms", "ms2hf"):
        source, target = (
            ("Hugging Face", "ModelScope")
            if direction == "hf2ms"
            else ("ModelScope", "Hugging Face")
        )
        command = commands.add_parser(
            direction,
            help=f"Transfer from {source} to {target}",
            description=f"Stream models or datasets from {source} to {target}. "
            "Run the same command again to continue an interrupted transfer.",
            epilog="Quote glob patterns and place SOURCE DESTINATION before multi-value filters. "
            "Sizes accept bytes or KiB/MiB/GiB and KB/MB/GB suffixes. "
            "Process restarts resume at file granularity; unfinished files may restart from zero.",
        )
        _transfer_arguments(command, direction)
    return parser


def _transfer_arguments(parser, direction):
    source, target = ("HF", "MS") if direction == "hf2ms" else ("MS", "HF")
    parser.add_argument("source", help=f"{source} source owner/repository; must be accessible")
    parser.add_argument(
        "destination",
        help=f"{target} destination owner/repository; created if missing, updated if it exists",
    )
    for hub, env in (("hf", "HF_TOKEN"), ("ms", "MODELSCOPE_API_TOKEN")):
        is_target = (hub == "ms") == (direction == "hf2ms")
        parser.add_argument(
            f"--{hub}-token",
            type=nonempty_token,
            required=is_target,
            default=None if is_target else os.getenv(env) or None,
            help=f"Required {hub.upper()} token with destination write/create permission; "
            "must be passed explicitly"
            if is_target
            else f"Optional {hub.upper()} source read token (default: {env} environment variable); "
            "required for private or restricted sources",
        )
    parser.add_argument(
        "--repo-type",
        choices=("auto", "model", "dataset"),
        default="auto",
        help="Source repository type (default: auto). Auto probes source models and datasets, "
        "uses the unique accessible match, and requires an explicit type if both exist.",
    )
    for hub, default in (("hf", "main"), ("ms", "master")):
        is_target = (hub == "ms") == (direction == "hf2ms")
        parser.add_argument(
            f"--{hub}-revision",
            default=default,
            help=(
                f"{hub.upper()} destination branch (default: {default}); custom branches must "
                "already exist; new repositories start on the default branch"
                if is_target
                else f"{hub.upper()} source branch, tag or commit (default: {default}); "
                "file versions are pinned for unfinished transfers"
            ),
        )
    parser.add_argument(
        "--visibility",
        dest="visibility",
        choices=("auto", "private", "public"),
        default="auto",
        help="New destination visibility (default: auto): public sources create public repos; "
        "private, gated or unknown visibility creates private repos; existing repos are unchanged",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path.home() / ".cache" / "hfms-bridge",
        help="Metadata checkpoint directory (default: ~/.cache/hfms-bridge); "
        "stores progress, not file contents",
    )
    parser.add_argument(
        "--cache-max-size",
        type=byte_size,
        default=256 * 1024**2,
        help="Metadata cache budget (default: 256MiB; minimum: 64KiB); evict oldest completed "
        "tasks first, protect unfinished tasks, fail if protected state cannot fit",
    )
    parser.add_argument(
        "--hf-endpoint",
        default=os.getenv("HF_ENDPOINT", "https://huggingface.co"),
        help="HF service base URL (default: HF_ENDPOINT environment variable or "
        "https://huggingface.co); ms2hf routes hf-mirror.com to official HF for destination requests",
    )
    parser.add_argument(
        "--ms-endpoint",
        default=os.getenv("MODELSCOPE_ENDPOINT", "https://modelscope.cn"),
        help="MS service base URL (default: MODELSCOPE_ENDPOINT environment variable or "
        "https://modelscope.cn)",
    )
    parser.add_argument(
        "--chunk-size",
        type=byte_size,
        default=1024**2,
        help="Maximum source read buffer per active file (default: 1MiB); "
        "does not control server upload part size",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=3,
        help="Maximum attempts per operation, including the first (default: 3)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Concurrent file transfers (default: 4; use 1 for serial transfers)",
    )
    parser.add_argument(
        "--progress",
        choices=("auto", "on", "off"),
        default="auto",
        help="Overall file count and temporary per-file byte bars on stderr "
        "(default: auto, terminals only); on forces bars, off uses ordinary logs",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Network connect/read timeout in seconds (default: 120); not a whole-file deadline",
    )
    parser.add_argument(
        "--include",
        action="extend",
        nargs="+",
        default=[],
        help="Transfer paths matching any case-sensitive repository-relative glob; repeatable "
        "(default: all files); quote patterns such as '*.safetensors'",
    )
    parser.add_argument(
        "--exclude",
        action="extend",
        nargs="+",
        default=[],
        help="Exclude paths matching any case-sensitive repository-relative glob; repeatable "
        "and takes priority over --include (default: none)",
    )
    parser.add_argument(
        "--on-conflict",
        choices=("overwrite", "error", "skip"),
        default="overwrite",
        help="Handle different or unverifiable existing content (default: overwrite): "
        "overwrite updates it, error rejects conflicts before writes, skip leaves it unchanged; "
        "identical files are always skipped",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Ignore local progress and resolve source versions again (default: resume); "
        "identical remote files/blobs can still be reused; does not force re-upload",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print a read-only transfer plan and JSON summary; no payload downloads, remote "
        "writes or cache changes; missing hashes may leave content equality unknown",
    )
    return parser


def main(argv=None):
    args = vars(build_parser().parse_args(argv))
    ms_token, hf_token = args.pop("ms_token"), args.pop("hf_token")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        result = Transfer(TransferOptions(**args), ms_token=ms_token, hf_token=hf_token).run()
    except KeyboardInterrupt:
        log.info("Interrupted. Run the same command to resume.")
        return 130
    except Timeout:
        log.error("Another process is using this task/cache; retry after it finishes")
        return 1
    except requests.RequestException:
        log.error("Network request failed. Run the same command to resume.")
        return 1
    except sqlite3.Error:
        log.error("Checkpoint database error; check cache permissions and available disk space")
        return 1
    except (TransferError, ValueError, OSError) as exc:
        log.error("%s", exc)
        log.info("Progress is saved. Run the same command to resume.")
        return 1
    if result.get("dry_run"):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        log.info("Done: %s committed, %s skipped", result["committed"], result["skipped"])
    return 0
