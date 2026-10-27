"""Command line interface for QuinePack."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .errors import (
    ExistsError,
    QuinePackError,
    UsageError,
)
from .model import Payload
from .naming import (
    FORMAT_KEYS,
    artifact_name,
    ensure_artifact_not_in_payload,
    ensure_output_not_inside_payload,
    format_is_compressed,
    seed_name,
)
from .scan import scan_payload
from .verify import verify_archive

DEFAULT_PRESET = 6


class _Parser(argparse.ArgumentParser):
    """Argument parser that raises UsageError instead of exiting."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(message)


class _Reporter:
    """Progress output on stderr; errors are handled by main()."""

    def __init__(self, quiet: bool, verbose: bool) -> None:
        self.quiet = quiet
        self.verbose = verbose

    def info(self, message: str) -> None:
        if not self.quiet:
            print(message, file=sys.stderr)

    def detail(self, message: str) -> None:
        if self.verbose:
            print(message, file=sys.stderr)

    def warning(self, message: str) -> None:
        print(f"warning: {message}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="quinepack",
        description="Build self-containing (quine) archives from local payloads.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    pack = subparsers.add_parser("pack", help="build a quine archive")
    pack.add_argument("input", type=Path, help="payload file or directory")
    pack.add_argument(
        "--format",
        dest="formats",
        metavar="LIST",
        help="comma separated 7z, 7z-store, txz, or all",
    )
    outputs = pack.add_mutually_exclusive_group()
    outputs.add_argument("-o", "--output", type=Path, help="output file (one format only)")
    outputs.add_argument("--outdir", type=Path, help="output directory")
    pack.add_argument(
        "--preset",
        type=int,
        default=None,
        help=f"7z compression preset 0-9 (default {DEFAULT_PRESET})",
    )
    pack.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="exclude paths matching GLOB, relative to the payload root",
    )
    pack.add_argument("--force", action="store_true", help="overwrite existing outputs")
    pack.add_argument(
        "--verify",
        action="store_true",
        help="verify each archive with external 7-Zip after writing",
    )
    pack.add_argument("--7zr", dest="seven_zip", metavar="PATH", help="7-Zip executable")
    _add_verbosity(pack)

    verify = subparsers.add_parser("verify", help="verify a quine archive")
    verify.add_argument("archive", type=Path, help="archive to verify")
    verify.add_argument(
        "--against",
        type=Path,
        help="payload to compare the extracted tree with",
    )
    verify.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="exclude paths matching GLOB when comparing with --against",
    )
    verify.add_argument("--7zr", dest="seven_zip", metavar="PATH", help="7-Zip executable")
    _add_verbosity(verify)

    subparsers.add_parser("version", help="print the QuinePack version")
    return parser


def _add_verbosity(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--quiet", action="store_true", help="suppress progress output")
    group.add_argument("--verbose", action="store_true", help="print stage details")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "version":
            return _command_version()
        if args.command == "pack":
            return _command_pack(args)
        if args.command == "verify":
            return _command_verify(args)
        raise UsageError(f"unknown command: {args.command}")
    except QuinePackError as exc:
        print(f"quinepack: error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("quinepack: interrupted", file=sys.stderr)
        return 130
    except OSError as exc:
        print(f"quinepack: error: {exc}", file=sys.stderr)
        return 1


def _command_version() -> int:
    print(f"quinepack {__version__}")
    return 0


def _command_pack(args: argparse.Namespace) -> int:
    reporter = _Reporter(args.quiet, args.verbose)
    _validate_preset(args.preset)

    formats: tuple[str, ...] | None = None
    if args.formats is not None:
        formats = _parse_formats(args.formats)
        if args.output is not None and len(formats) != 1:
            raise UsageError("-o/--output requires exactly one format")
    elif not _is_interactive():
        raise UsageError("--format is required when not running in a terminal")

    reporter.detail(f"scanning {args.input}")
    payload = scan_payload(args.input, tuple(args.exclude))
    reporter.detail(f"payload: {len(payload.entries)} entries, {payload.total_bytes} bytes")

    if formats is None:
        formats = _select_formats_interactive(payload)

    planned = _plan_outputs(payload, args.input, formats, args.output, args.outdir)
    selected = _select_targets(planned, reporter, force=args.force)

    preset = DEFAULT_PRESET if args.preset is None else args.preset
    if args.preset is not None and any(
        not format_is_compressed(format_key) for format_key, _ in selected
    ):
        reporter.warning("--preset has no effect on 7z-store or txz")

    for format_key, output in selected:
        output.parent.mkdir(parents=True, exist_ok=True)
        reporter.info(f"building {output.name}")
        data = _build(payload, format_key, output.name, preset)
        _write_atomic(output, data)
        reporter.info(f"wrote {output} ({_human_size(len(data))})")

        if args.verify:
            reporter.info(f"verifying {output}")
            verify_archive(
                output,
                against=args.input,
                seven_zip=args.seven_zip,
                excludes=tuple(args.exclude),
            )

    return 0


def _command_verify(args: argparse.Namespace) -> int:
    reporter = _Reporter(args.quiet, args.verbose)
    reporter.detail(f"verifying {args.archive}")
    verify_archive(
        args.archive,
        against=args.against,
        seven_zip=args.seven_zip,
        excludes=tuple(args.exclude),
    )
    reporter.info(f"verified {args.archive}")
    return 0


def _validate_preset(preset: int | None) -> None:
    if preset is None:
        return
    if not 0 <= preset <= 9:
        raise UsageError("--preset must be between 0 and 9")


def _parse_formats(value: str) -> tuple[str, ...]:
    raw = [item.strip().lower() for item in value.split(",")]
    if any(not item for item in raw):
        raise UsageError("--format contains an empty value")
    if "all" in raw:
        if len(raw) != 1:
            raise UsageError("--format all cannot be combined with other formats")
        return FORMAT_KEYS
    unknown = [item for item in raw if item not in FORMAT_KEYS]
    if unknown:
        raise UsageError(
            f"unknown format: {unknown[0]} (expected 7z, 7z-store, txz, or all)"
        )
    return tuple(dict.fromkeys(raw))


def _plan_outputs(
    payload: Payload,
    input_path: Path,
    formats: tuple[str, ...],
    output: Path | None,
    outdir: Path | None,
) -> list[tuple[str, Path]]:
    if output is not None:
        if len(formats) != 1:
            raise UsageError("-o/--output requires exactly one format")
        planned = [(formats[0], output)]
    else:
        directory = outdir if outdir is not None else input_path.parent
        planned = [
            (format_key, directory / artifact_name(input_path, format_key))
            for format_key in formats
        ]

    for format_key, target in planned:
        ensure_artifact_not_in_payload(payload, target.name)
        ensure_output_not_inside_payload(payload, target)
    return planned


def _select_targets(
    planned: list[tuple[str, Path]],
    reporter: _Reporter,
    *,
    force: bool,
) -> list[tuple[str, Path]]:
    interactive = _is_interactive()
    selected: list[tuple[str, Path]] = []
    for format_key, output in planned:
        if not output.exists() or force:
            selected.append((format_key, output))
        elif interactive:
            if _confirm_overwrite(output.name):
                selected.append((format_key, output))
            else:
                reporter.info(f"skipped {output.name}")
        else:
            raise ExistsError(f"output already exists: {output} (use --force to overwrite)")
    return selected


def _build(
    payload: Payload,
    format_key: str,
    artifact: str,
    preset: int,
) -> bytes:
    try:
        if format_key == "txz":
            from .formats.tarxz import build_archive

            result = build_archive(
                payload,
                quine_name=artifact,
                seed_name=seed_name(artifact),
            )
        else:
            from .formats.sevenzip import build_archive

            result = build_archive(
                payload,
                quine_name=artifact,
                seed_name=seed_name(artifact),
                compressed=format_is_compressed(format_key),
                preset=preset,
            )
    except ImportError as exc:
        raise QuinePackError(
            f"the {format_key} writer is not available in this build"
        ) from exc
    except QuinePackError:
        raise
    except Exception as exc:  # noqa: BLE001 - writer failures become runtime errors
        raise QuinePackError(f"failed to build {artifact}: {exc}") from exc

    data = getattr(result, "data", None)
    if not isinstance(data, (bytes, bytearray)):
        raise QuinePackError(f"the {format_key} writer returned no archive data")
    return bytes(data)


def _write_atomic(output: Path, data: bytes) -> None:
    partial = output.with_name(f"{output.name}.partial")
    try:
        with open(partial, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, output)
    except BaseException:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            # Best effort: report the original write failure, not cleanup noise.
            pass
        raise


def _is_interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _select_formats_interactive(payload: Payload) -> tuple[str, ...]:
    label = payload.source.name or str(payload.source)
    print(f"QuinePack: {label} ({_human_size(payload.total_bytes)})")
    print("Select output format(s):")
    print("  1) 7z        compressed, requires 7-Zip        [default]")
    print("  2) 7z-store  compatibility, not compressed")
    print("  3) tar.xz    Linux/BusyBox, not compressed")
    print("  a) all")

    while True:
        try:
            answer = input("> ")
        except EOFError:
            raise UsageError("format selection aborted: no input") from None
        selection = _parse_menu_answer(answer)
        if selection is not None:
            return selection
        print("Invalid selection. Enter 1, 2, 3, a, or a comma separated list.")


def _parse_menu_answer(answer: str) -> tuple[str, ...] | None:
    value = answer.strip().lower()
    if not value:
        return ("7z",)
    if value in {"a", "all"}:
        return FORMAT_KEYS
    mapping = {"1": "7z", "2": "7z-store", "3": "txz"}
    selected: list[str] = []
    for item in value.split(","):
        format_key = mapping.get(item.strip())
        if format_key is None:
            return None
        selected.append(format_key)
    return tuple(dict.fromkeys(selected))


def _confirm_overwrite(name: str) -> bool:
    try:
        answer = input(f"{name} already exists. Overwrite? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def _human_size(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"  # pragma: no cover - unreachable