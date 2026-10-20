"""hfms-download: explicit local destinations for both hubs."""

import argparse
import json
import logging
import os

import requests
from filelock import Timeout

from . import __version__
from .cli import byte_size, nonempty_token
from .download import Download, DownloadOptions
from .http import TransferError


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hfms-download",
        description="Download HF or MS repositories directly to a local directory.",
    )
    parser.add_argument(
        "--version", action="version", version=__version__, help="Show version and exit"
    )
    commands = parser.add_subparsers(dest="hub", required=True, help="Source platform")
    for hub, name, env in (
        ("hf", "Hugging Face", "HF_TOKEN"),
        ("ms", "ModelScope", "MODELSCOPE_API_TOKEN"),
    ):
        command = commands.add_parser(
            hub,
            help=f"Download from {name}",
            description=f"Download from {name}.",
            epilog="File data and recovery metadata stay beside your chosen destination, never in a global cache. "
            "Quote globs; put repository ID and filenames before multi-value filters.",
        )
        command.add_argument("repo_id", help="Source owner/repository")
        command.add_argument(
            "filenames", nargs="*", help="Exact repository file paths; default: entire repository"
        )
        destination = command.add_mutually_exclusive_group(required=True)
        destination.add_argument(
            "--local-dir", help="Destination base directory; created if missing"
        )
        destination.add_argument(
            "--preset",
            choices=("author",),
            help="Author's preset: use existing HF_MODELS "
            "or HF_DATASETS directory according to repository type, with owner/repository subdirectories",
        )
        command.add_argument(
            "--layout",
            choices=("direct", "repo"),
            help="With --local-dir: direct writes into "
            "that directory (default), repo appends owner/repository; cannot combine with --preset",
        )
        command.add_argument(
            "--repo-type",
            choices=("auto", "model", "dataset"),
            default="auto",
            help="Default auto: probe both source namespaces; require an explicit type if ambiguous",
        )
        command.add_argument(
            "--revision", help="Source branch, tag or commit (default: main for HF, master for MS)"
        )
        command.add_argument(
            "--token",
            type=nonempty_token,
            default=os.getenv(env) or None,
            help=f"Optional source read token (default: {env}); needed for private/restricted sources",
        )
        command.add_argument(
            "--endpoint",
            help="Service base URL; default: HF_ENDPOINT / MODELSCOPE_ENDPOINT "
            "for the selected hub, otherwise its official endpoint",
        )
        command.add_argument(
            "--include",
            action="extend",
            nargs="+",
            default=[],
            help="Repeatable case-sensitive path globs; default: all selected files",
        )
        command.add_argument(
            "--exclude",
            action="extend",
            nargs="+",
            default=[],
            help="Repeatable exclude globs; take priority over include (default: none)",
        )
        command.add_argument(
            "--max-workers",
            type=int,
            default=4,
            help="Concurrent files (default: 4); each file downloads sequentially",
        )
        command.add_argument(
            "--attempts",
            type=int,
            default=3,
            help="Attempts per operation, including the first (default: 3)",
        )
        command.add_argument(
            "--timeout", type=int, default=120, help="Socket read timeout in seconds (default: 120)"
        )
        command.add_argument(
            "--chunk-size",
            type=byte_size,
            default=1024**2,
            help="Read buffer per file (default: 1MiB); accepts KiB/MiB/GiB",
        )
        command.add_argument(
            "--progress",
            choices=("auto", "on", "off"),
            default="auto",
            help="Overall/per-file progress on stderr; auto enables on terminals (default: auto)",
        )
        command.add_argument(
            "--force-download",
            action="store_true",
            help="Resolve source again and redownload, "
            "discarding prior partials; permits replacing different local files after validation",
        )
        command.add_argument(
            "--dry-run",
            action="store_true",
            help="Read-only JSON plan; no directory creation, metadata changes or payload downloads",
        )
        command.add_argument(
            "--quiet",
            action="store_true",
            help="Suppress progress and ordinary logs; print destination on success, retain warnings/errors",
        )
    return parser


def main(argv=None):
    parser = build_parser()
    args = vars(parser.parse_args(argv))
    token = args.pop("token")
    if args["preset"] and args["layout"]:
        parser.error("--preset cannot be combined with --layout")
    logging.basicConfig(
        level=logging.WARNING if args["quiet"] else logging.INFO, format="%(levelname)s %(message)s"
    )
    log = logging.getLogger("hfms-bridge")
    try:
        result = Download(DownloadOptions(**args), token=token).run()
    except KeyboardInterrupt:
        log.warning("Interrupted. Run the same command to resume incomplete files.")
        return 130
    except Timeout:
        log.error("Another download is using this destination; retry after it finishes")
        return 1
    except requests.RequestException:
        log.error("Network request failed; run the same command to resume")
        return 1
    except (TransferError, ValueError, OSError) as exc:
        log.error("%s", exc)
        return 1
    print(
        json.dumps(result, ensure_ascii=False, indent=2) if args["dry_run"] else result["local_dir"]
    )
    return 0
