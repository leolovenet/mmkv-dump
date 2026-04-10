#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MMKV Dump -- a general-purpose MMKV database inspection tool.

Prerequisite:
    MMKV for Python must be installed before running this script.
    See: https://github.com/Tencent/MMKV/wiki/python_setup
"""

# Defer annotation evaluation so the runtime version guard below can fire
# with a friendly error on Python 3.9 and earlier (which would otherwise
# fail to parse the `str | None` annotations used throughout the script).
from __future__ import annotations

__version__ = "1.3"

import sys

if sys.version_info < (3, 10):
    sys.stderr.write(
        f"Error: mmkvdump requires Python 3.10 or newer "
        f"(found {sys.version.split()[0]})\n"
    )
    sys.exit(1)

import argparse
import json
import os
import re
import signal
from datetime import datetime
from typing import Any

try:
    import mmkv
except ImportError:
    sys.stderr.write(
        "Error: the 'mmkv' Python package is not installed.\n"
        "Install MMKV for Python first, see:\n"
        "  https://github.com/Tencent/MMKV/wiki/python_setup\n"
    )
    sys.exit(1)

try:
    from pygments import highlight
    from pygments.lexers import JsonLexer
    from pygments.formatters import TerminalFormatter
    _json_lexer = JsonLexer()
    _json_formatter = TerminalFormatter()
    _HAS_PYGMENTS = True
except ImportError:
    _HAS_PYGMENTS = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USAGE_EXAMPLES = """\
Prerequisite:
  MMKV for Python must be installed. See:
  https://github.com/Tencent/MMKV/wiki/python_setup

Examples:
  # Scan the directory for MMKV instances
  mmkvdump --dir /path/to/mmkv instances

  # List all keys
  mmkvdump --dir /path/to/mmkv --id MyMMKV keys

  # Filter keys by regex
  mmkvdump --dir /path/to/mmkv --id MyMMKV keys --grep '^user_'

  # Get a single key (auto-infer the type)
  mmkvdump --dir /path/to/mmkv --id MyMMKV get some_key

  # View the raw hex bytes for a key (no type inference)
  mmkvdump --dir /path/to/mmkv --id MyMMKV get some_key --raw

  # Force reading as a specific type
  mmkvdump --dir /path/to/mmkv --id MyMMKV get some_key --type string

  # Dump every key-value pair (auto-infer types)
  mmkvdump --dir /path/to/mmkv --id MyMMKV dump

  # Dump as JSON, suitable for piping into jq
  mmkvdump --dir /path/to/mmkv --id MyMMKV dump --format json | jq .

  # Show raw bytes plus every type interpretation
  mmkvdump --dir /path/to/mmkv --id MyMMKV raw some_key

  # Use an encryption key and single-process mode
  mmkvdump --dir /path/to/mmkv --id MyMMKV --crypt-key abc123 --single-process keys

  # Read the encryption key from a file (avoids leaking via `ps`)
  mmkvdump --dir /path/to/mmkv --id MyMMKV --crypt-key-file /secrets/mmkv.key keys

  # Use the default MMKV instance
  mmkvdump --dir /path/to/mmkv --default keys

  # Enable debug logging
  mmkvdump --dir /path/to/mmkv --id MyMMKV --log-level debug keys
"""

# Maximum column width for a truncated value in `dump` text output.
_DUMP_TRUNCATE_AT = 120
_DUMP_ELLIPSIS_AT = _DUMP_TRUNCATE_AT - 3  # leave room for "..."

# Preview length for string values shown by the `raw` subcommand.
_RAW_STRING_PREVIEW = 200
_RAW_STRING_ELLIPSIS = _RAW_STRING_PREVIEW - 3

# Valid Unix-epoch range for the ``raw`` command's "Time interpretations"
# block. 2001-01-01 (978307200) to 2200-01-01 (7258118400) in UTC seconds.
# The window rejects zero, small counters (1, 1000, etc.), and
# implausibly old/future values while still covering the realistic span
# of a timestamp field stored in an MMKV database.
_TIMESTAMP_MIN_SECONDS = 978307200
_TIMESTAMP_MAX_SECONDS = 7258118400

# Divisors to normalise a timestamp in the named unit to seconds.
_TIMESTAMP_UNITS: dict[str, int] = {
    "s": 1,
    "ms": 1000,
    "us": 1_000_000,
    "ns": 1_000_000_000,
}

_TYPE_CHOICES = (
    "string", "bool", "int32", "uint32", "int64", "uint64", "float", "bytes",
)

# Subcommands whose execution requires an instance selector (``--id`` or
# ``--default``) in addition to ``--dir``. ``instances`` is the sole
# exception; it discovers instance IDs in ``--dir`` rather than operating
# on a specific one. Both ``main()`` and the shell completion generators
# share this list -- keep them in sync (``main()``'s validation check
# comments refer back here).
_SUBCOMMANDS_NEEDING_INSTANCE = ("keys", "get", "dump", "raw")

# Long-option names of the mutex group that selects the MMKV instance.
# When a subcommand in ``_SUBCOMMANDS_NEEDING_INSTANCE`` is picked, at
# least one of these must be present on the command line. Completion
# generators use this list to narrow tab-suggestions so tab can't
# fill in a subcommand argparse will reject at parse time.
_INSTANCE_SELECTOR_LONGS = ("id", "default")

_LOG_LEVELS = {
    "none": mmkv.MMKVLogLevel.NoLog,
    "debug": mmkv.MMKVLogLevel.Debug,
    "info": mmkv.MMKVLogLevel.Info,
    "warning": mmkv.MMKVLogLevel.Warning,
    "error": mmkv.MMKVLogLevel.Error,
}

# Derived from _LOG_LEVELS: {enum_value: single_letter_tag}
_LOG_LEVEL_NAMES = {v: k[0].upper() for k, v in _LOG_LEVELS.items()}
assert len(_LOG_LEVEL_NAMES) == len(_LOG_LEVELS), \
    "log level first-letter collision in _LOG_LEVELS"


# ---------------------------------------------------------------------------
# Logging & error handlers
# ---------------------------------------------------------------------------

def _mmkv_logger(log_level, file, line, function, message) -> None:
    tag = _LOG_LEVEL_NAMES.get(log_level, "?")
    print(f"[{tag}] <{file}:{line}:{function}> {message}", file=sys.stderr)


def _error_handler(mmap_id, error_type) -> mmkv.MMKVErrorType:
    print(f"[{mmap_id}] error: {error_type}", file=sys.stderr)
    return mmkv.MMKVErrorType.OnErrorRecover


def _content_change_handler(mmap_id) -> None:
    print(f"[{mmap_id}] content changed by another process", file=sys.stderr)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _print_json(obj: Any, use_color: bool, indent: int = 2) -> None:
    """Print JSON with optional syntax highlighting.

    Color is applied only when (1) pygments is installed, (2) the caller
    allows it, and (3) stdout is a terminal. Output always ends with
    exactly one trailing newline regardless of path.

    Prefers strict JSON (``allow_nan=False``) so the output can be piped
    into jq and friends. If the value contains NaN/Infinity we fall back
    to Python's extended form and emit a warning -- losing strictness is
    better than crashing on an already-computed value.
    """
    try:
        text = json.dumps(obj, indent=indent, ensure_ascii=False, allow_nan=False)
    except ValueError:
        print(
            "Warning: value contains NaN/Infinity; output is not strict JSON",
            file=sys.stderr,
        )
        text = json.dumps(obj, indent=indent, ensure_ascii=False, allow_nan=True)
    if use_color and _HAS_PYGMENTS and sys.stdout.isatty():
        text = highlight(text, _json_lexer, _json_formatter)
    # Normalize trailing newline: highlight() may or may not append one.
    print(text.rstrip("\n"))


def _hex_dump(data: bytes, indent: str = "  ") -> str:
    """Format bytes as a hex dump with an ASCII sidebar.

    Returns a `(empty)` sentinel line for zero-length input so callers
    that forget to guard against empty data still produce visible
    output instead of a blank line.
    """
    if len(data) == 0:
        return f"{indent}(empty)"
    lines = []
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{indent}{offset:04x}  {hex_part:<48s}  {ascii_part}")
    return "\n".join(lines)


def _format_size(n: int) -> str:
    """Format a byte count in human-readable units."""
    size: float = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _format_value(type_name: str, value: Any) -> str:
    """Convert a typed value to its display form.

    For type_name=="bytes" the caller is expected to pass a pre-hexed
    string (as produced by _infer_and_read), not raw bytes. This
    function passes it through via str().
    """
    if value is None:
        return "None"
    if type_name == "bool":
        return "true" if value else "false"
    if type_name == "string" and value == "":
        return '""'
    return str(value)


def _truncate(s: str) -> str:
    if len(s) > _DUMP_TRUNCATE_AT:
        return s[:_DUMP_ELLIPSIS_AT] + "..."
    return s


def _format_timestamp(value: int, unit: str) -> str | None:
    """Format an integer as a local-time datetime when it is a plausible
    Unix epoch in the given unit.

    Returns the formatted string (``yyyy-MM-dd HH:mm:ss``) if ``value``
    falls in the 2001-2200 window after unit conversion, ``None``
    otherwise. The range gate rejects zero, small counters, and values
    that are almost certainly not timestamps. Local timezone is used
    because the primary use case is "when did this field last change",
    where the user wants to recognize the time against their own
    wall-clock, not compute timezone offsets in their head.
    """
    seconds = value / _TIMESTAMP_UNITS[unit]
    if not (_TIMESTAMP_MIN_SECONDS <= seconds <= _TIMESTAMP_MAX_SECONDS):
        return None
    try:
        return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# Type inference
# ---------------------------------------------------------------------------

def _key_exists(kv: mmkv.MMKV, key: str) -> bool:
    """Check whether a key exists in the MMKV instance.

    Prefers containsKey() (O(1)) when available in the Python binding,
    falls back to a linear scan of kv.keys().
    """
    try:
        return kv.containsKey(key)
    except AttributeError:
        return key in kv.keys()


def _is_printable_text(text: str) -> bool:
    """Check if a string is human-readable (no binary control characters)."""
    for ch in text:
        code = ord(ch)
        if code == 0:  # null byte -> binary data, not text
            return False
        if code < 0x20 and ch not in ("\t", "\n", "\r"):
            return False
    return True


def _infer_and_read(kv: mmkv.MMKV, key: str) -> tuple[str, Any]:
    """Heuristically infer the type of a key and return (type_name, value).

    The caller must ensure the key exists; this function assumes it.

    MMKV doesn't store type metadata, so we use heuristics. Note that
    getBytes() only returns data for values stored via setBytes(). For
    numeric types it returns empty bytes, so byte-length based type
    guessing is both useless (never triggers for numerics) and harmful
    (misclassifies real setBytes data whose length happens to be 1/4/8).

    Strategy:
      1. Try getString -- non-empty printable UTF-8 -> string/JSON
      2. getBytes() non-empty -> real setBytes() payload -> hex string
      3. Probe numeric getters for non-default values
      4. Fall back to empty string (or null as a defensive last resort)
    """
    raw = kv.getBytes(key)
    length = len(raw) if raw is not None else 0

    # --- Try string first ---
    str_value: str | None = None
    try:
        str_value = kv.getString(key)
    except UnicodeDecodeError:
        pass

    if str_value is not None and len(str_value) > 0 and _is_printable_text(str_value):
        # Sniff for JSON: look past any leading whitespace that might have
        # been stored alongside the payload, and accept the minimal empty
        # object/array (length 2) as well as anything larger.
        stripped = str_value.lstrip()
        if len(stripped) >= 2 and stripped[0] in "{[":
            try:
                return ("json", json.loads(str_value))
            except (json.JSONDecodeError, ValueError):
                pass
        return ("string", str_value)

    # --- getBytes() has data -> real setBytes() payload ---
    if length > 0:
        return ("bytes", raw.hex())

    # --- Probe numeric getters for non-default values ---
    # Note: MMKV numeric getters return concrete values (0 on miss),
    # never None, so we only need to check against the default.
    li = kv.getLongInt(key)
    if li != 0:
        return ("int64", li)

    i = kv.getInt(key)
    if i != 0:
        return ("int32", i)

    f = kv.getFloat(key)
    if f != 0.0:
        return ("float", f)

    if kv.getBool(key):
        return ("bool", True)

    # All numeric getters returned defaults -> report as empty string
    # (the caller already confirmed the key exists).
    if str_value is not None:
        return ("string", str_value)

    # Defensive fallback: getString raised and no numeric value found.
    return ("null", None)


def _read_as_type(kv: mmkv.MMKV, key: str, type_name: str) -> Any:
    """Read a key forced as a specific type."""
    getters = {
        "string": kv.getString,
        "bool": kv.getBool,
        "int32": kv.getInt,
        "uint32": kv.getUInt,
        "int64": kv.getLongInt,
        "uint64": kv.getLongUInt,
        "float": kv.getFloat,
        "bytes": kv.getBytes,
    }
    return getters[type_name](key)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _filter_keys(keys: list[str], pattern: str | None) -> list[str]:
    """Filter keys by regex pattern (case-insensitive).

    Always returns a fresh list, never a reference to the input.
    """
    if pattern is None:
        return keys[:]
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        print(f"Invalid regex pattern: {e}", file=sys.stderr)
        sys.exit(1)
    return [k for k in keys if regex.search(k)]


def cmd_instances(args: argparse.Namespace) -> int:
    """List MMKV instance IDs (and file sizes) found in --dir.

    Each MMKV instance produces two files on disk: <id> and <id>.crc.
    We scan for *.crc files to enumerate the instance IDs, then report
    the size of the corresponding main data file.
    """
    try:
        with os.scandir(args.dir) as it:
            # len(name) > 4 filters out a stray file literally named ".crc"
            # which would otherwise yield an empty instance ID.
            crc_names = [
                entry.name for entry in it
                if entry.is_file()
                and entry.name.endswith(".crc")
                and len(entry.name) > 4
            ]
    except OSError as e:
        print(f"Error reading directory: {e}", file=sys.stderr)
        return 1

    if not crc_names:
        print("(no MMKV instances found)")
        return 0

    instances: list[tuple[str, int]] = []
    for name in crc_names:
        iid = name[:-4]
        main_path = os.path.join(args.dir, iid)
        try:
            size = os.path.getsize(main_path)
        except OSError:
            size = -1
        instances.append((iid, size))
    instances.sort(key=lambda t: t[0].casefold())

    print(f"Total: {len(instances)} MMKV instance(s) in {args.dir}\n")
    max_id_len = max(len(iid) for iid, _ in instances)
    for iid, size in instances:
        size_str = _format_size(size) if size >= 0 else "(missing)"
        print(f"  {iid.ljust(max_id_len)}  {size_str}")
    return 0


def cmd_keys(kv: mmkv.MMKV, args: argparse.Namespace) -> int:
    """List all keys."""
    all_keys = sorted(kv.keys(), key=str.casefold)
    filtered = _filter_keys(all_keys, args.grep)
    if args.grep:
        print(f"Matched: {len(filtered)}/{len(all_keys)} keys\n")
    else:
        print(f"Total: {len(all_keys)} keys\n")
    for key in filtered:
        print(key)
    return 0


def cmd_get(kv: mmkv.MMKV, args: argparse.Namespace) -> int:
    """Get the value of a specific key."""
    key = args.key

    if not _key_exists(kv, key):
        print("(key not found)")
        return 1

    # --raw: show hex dump of getBytes() regardless of the actual type.
    if args.raw:
        raw = kv.getBytes(key)
        if raw is None or len(raw) == 0:
            print("(no raw bytes -- value stored as a native type, not via setBytes)")
        else:
            print(f"{len(raw)} bytes:")
            print(_hex_dump(raw))
        return 0

    # --type: force a specific type.
    if args.type:
        value = _read_as_type(kv, key, args.type)
        if args.type == "bytes":
            if value is None or len(value) == 0:
                print("(empty bytes)")
            else:
                print(value.hex())
        else:
            print(_format_value(args.type, value))
        return 0

    # Auto-infer.
    type_name, value = _infer_and_read(kv, key)
    if type_name == "json":
        print(f"({type_name})")
        _print_json(value, use_color=not args.no_color)
    elif type_name == "bytes":
        # Re-read the raw bytes so we can show a proper hex dump instead
        # of a single long line of hex characters.
        raw = kv.getBytes(key)
        print(f"({type_name}) {len(raw)} bytes:")
        print(_hex_dump(raw))
    elif type_name == "null":
        # Defensive branch -- containsKey() already passed so this is rare.
        print("(key exists but all getters returned defaults)")
    else:
        print(f"({type_name}) {_format_value(type_name, value)}")
    return 0


def cmd_dump(kv: mmkv.MMKV, args: argparse.Namespace) -> int:
    """Dump all key-value pairs."""
    all_keys = sorted(kv.keys(), key=str.casefold)
    filtered = _filter_keys(all_keys, args.grep)

    if args.format == "json":
        return _dump_json(kv, filtered, args)
    return _dump_text(kv, filtered, len(all_keys), args)


def _dump_text(
    kv: mmkv.MMKV,
    filtered: list[str],
    total_count: int,
    args: argparse.Namespace,
) -> int:
    if args.grep:
        print(f"Matched: {len(filtered)}/{total_count} keys\n")
    else:
        print(f"Total: {total_count} keys\n")

    for key in filtered:
        type_name, value = _infer_and_read(kv, key)
        if type_name == "json":
            if args.full:
                print(f"  {key}  ({type_name})")
                _print_json(value, use_color=not args.no_color)
            else:
                compact = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                print(f"  {key}  ({type_name}) {_truncate(compact)}")
        elif type_name == "bytes" and args.full:
            # Re-read the raw bytes for a readable multi-line hex dump
            # instead of one giant line of hex characters.
            raw = kv.getBytes(key)
            print(f"  {key}  ({type_name}) {len(raw)} bytes:")
            print(_hex_dump(raw, indent="    "))
        elif type_name == "null":
            print(f"  {key}  (null)")
        else:
            display = _format_value(type_name, value)
            if not args.full:
                display = _truncate(display)
            print(f"  {key}  ({type_name}) {display}")
    return 0


def _dump_json(
    kv: mmkv.MMKV,
    filtered: list[str],
    args: argparse.Namespace,
) -> int:
    """Dump as a JSON object: {key: {"type": ..., "value": ...}}.

    The --full flag is intentionally ignored in this mode; JSON output
    is always full. Grep metadata is also dropped to keep the output
    valid JSON that is suitable for piping into jq.
    """
    result: dict[str, dict[str, Any]] = {}
    for key in filtered:
        type_name, value = _infer_and_read(kv, key)
        result[key] = {"type": type_name, "value": value}
    _print_json(result, use_color=not args.no_color)
    return 0


def cmd_raw(kv: mmkv.MMKV, args: argparse.Namespace) -> int:
    """Show raw bytes and every possible type interpretation for a key."""
    key = args.key

    if not _key_exists(kv, key):
        print("(key not found)")
        return 1

    raw = kv.getBytes(key)
    if raw is None or len(raw) == 0:
        print("Raw bytes: (none -- value stored as a native type, not via setBytes)")
    else:
        print(f"Raw bytes ({len(raw)} bytes):")
        print(_hex_dump(raw))

    print("\nAll interpretations:")
    print("  (getters for non-matching types return their default:"
          " 0 / false / empty)")
    try:
        s = kv.getString(key)
        if s is not None:
            display = s if len(s) <= _RAW_STRING_PREVIEW else s[:_RAW_STRING_ELLIPSIS] + "..."
            print(f"  String:  {display}")
        else:
            print(f"  String:  (None)")
    except UnicodeDecodeError as e:
        print(f"  String:  (decode error: {e})")

    print(f"  Bool:    {_format_value('bool', kv.getBool(key))}")
    i32 = kv.getInt(key)
    u32 = kv.getUInt(key)
    i64 = kv.getLongInt(key)
    u64 = kv.getLongUInt(key)
    print(f"  Int32:   {i32}")
    print(f"  UInt32:  {u32}")
    print(f"  Int64:   {i64}")
    print(f"  UInt64:  {u64}")
    print(f"  Float:   {kv.getFloat(key)}")

    # Time interpretations: show any integer reads that land in the
    # plausible Unix-epoch window (2001-2200). Int32/UInt32 are only
    # probed as seconds (ms/us/ns are out of range for 32-bit values);
    # Int64/UInt64 get the full sweep of four units.
    timestamp_candidates: list[tuple[str, int, str]] = [
        ("Int32 as seconds", i32, "s"),
        ("UInt32 as seconds", u32, "s"),
        ("Int64 as seconds", i64, "s"),
        ("UInt64 as seconds", u64, "s"),
        ("Int64 as milliseconds", i64, "ms"),
        ("UInt64 as milliseconds", u64, "ms"),
        ("Int64 as microseconds", i64, "us"),
        ("UInt64 as microseconds", u64, "us"),
        ("Int64 as nanoseconds", i64, "ns"),
        ("UInt64 as nanoseconds", u64, "ns"),
    ]
    time_rows: list[tuple[str, str]] = []
    for label, val, unit in timestamp_candidates:
        formatted = _format_timestamp(val, unit)
        if formatted is not None:
            time_rows.append((label, formatted))

    if time_rows:
        print("\nTime interpretations (if the integer is a Unix epoch):")
        width = max(len(label) for label, _ in time_rows)
        for label, formatted in time_rows:
            print(f"  {label:<{width}}  {formatted}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fish_quote(s: str) -> str:
    """Escape a string for inclusion inside fish single quotes.

    Fish only interprets ``\\\\`` and ``\\'`` inside single quotes; everything
    else is literal, so these two substitutions are sufficient.
    """
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _zsh_quote(s: str) -> str:
    """Escape a string for inclusion inside zsh single quotes.

    Unlike fish, zsh single quotes do NOT interpret escape sequences --
    a single quote simply cannot appear inside ``'...'``. The POSIX idiom
    is to close the string, inject an escaped quote, and reopen:
    ``'foo'\\''bar'`` which zsh concatenates into ``foo'bar``.
    """
    return "'" + s.replace("'", "'\\''") + "'"


def _iter_parser_spec(parser: argparse.ArgumentParser) -> dict:
    """Walk an ``ArgumentParser`` and return a shell-neutral spec.

    Centralizes all the argparse private-API coupling (``_actions``,
    ``_SubParsersAction._choices_actions``, ``_HelpAction``) in one place
    so individual completion generators (fish, bash, zsh) consume a
    stable dict shape instead of each one walking the parser itself.

    Keys of the returned dict:

    * ``prog`` -- the canonical command name (``parser.prog``).
    * ``globals`` -- list of top-level flag descriptors.
    * ``subcommands`` -- list of ``{name, help, flags, positionals}``
      dicts, one per sub-parser, in insertion order.
    * ``required_longs`` -- long-option names (without the ``--`` prefix)
      of every top-level ``required=True`` flag. Completion generators
      gate the subcommand list on all of these being present.

    Each flag descriptor is
    ``{option_strings, takes_value, choices, help, required, is_help}``.
    ``is_help`` exists so generators can special-case ``-h/--help``
    (which is duplicated across every sub-parser and should only be
    emitted once unconditioned at the top level).

    Each positional descriptor is ``{name, help}``. Only the zsh
    generator currently consumes these (to declare the positional in
    ``_arguments`` so zsh knows the token shape); fish and bash ignore
    the field. It's still carried here so the walker stays the single
    source of truth.
    """
    def describe(a: argparse.Action) -> dict:
        return {
            "option_strings": list(a.option_strings),
            "takes_value": a.nargs != 0,
            "choices": [str(c) for c in a.choices] if a.choices else None,
            "help": a.help or "",
            "required": bool(a.required),
            "is_help": isinstance(a, argparse._HelpAction),
        }

    def flags_of(p: argparse.ArgumentParser) -> list[dict]:
        # Skip subparser pseudo-actions and positionals; return only the
        # flag-style (optional) actions so generators have a uniform view.
        return [
            describe(a) for a in p._actions
            if not isinstance(a, argparse._SubParsersAction)
            and a.option_strings
        ]

    def positionals_of(p: argparse.ArgumentParser) -> list[dict]:
        # Return positional args in declaration order, skipping the
        # subparser pseudo-action. Used by zsh's _arguments spec.
        return [
            {"name": a.dest, "help": a.help or ""}
            for a in p._actions
            if not isinstance(a, argparse._SubParsersAction)
            and not a.option_strings
        ]

    sub_action = next(
        (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)),
        None,
    )

    subcommands: list[dict] = []
    if sub_action is not None:
        help_by_name = {
            ca.dest: (ca.help or "") for ca in sub_action._choices_actions
        }
        for name, sp in sub_action.choices.items():
            subcommands.append({
                "name": name,
                "help": help_by_name.get(name, ""),
                "flags": flags_of(sp),
                "positionals": positionals_of(sp),
            })

    globals_ = flags_of(parser)
    required_longs = list(dict.fromkeys(
        o[2:]
        for g in globals_ if g["required"]
        for o in g["option_strings"]
        if o.startswith("--")
    ))

    return {
        "prog": parser.prog,
        "globals": globals_,
        "subcommands": subcommands,
        "required_longs": required_longs,
    }


def _completion_fish(parser: argparse.ArgumentParser) -> str:
    """Generate a fish shell completion script from parser metadata.

    Consumes the neutral spec from ``_iter_parser_spec`` and emits fish
    ``complete`` directives. All argparse private-API coupling lives in
    the walker; this function only knows about fish syntax.
    """
    spec = _iter_parser_spec(parser)
    prog = spec["prog"]
    has_opt_fn = f"__{prog}_has_opt"
    out: list[str] = [
        f"# fish completion for {prog} -- generated by `{prog} --completion fish`",
        f"# Do not edit by hand; regenerate after upgrading {prog}.",
        "",
        # Fish accumulates `complete` declarations across re-sources rather
        # than replacing them, so a freshly regenerated file would stack on
        # top of the previous revision and show the union of both. Erase
        # any prior state for this command first to make re-sourcing safe.
        f"complete -c {prog} -e",
        f"complete -c {prog} -f",  # disable file completion globally
        "",
        # Tiny helper: checks whether any of the given long-option names
        # is present on the command line, in EITHER the space-separated
        # form (``--dir /tmp``) or the inline form (``--dir=/tmp``).
        # Wraps fish's ``__fish_contains_opt`` which only handles the
        # former. Namespaced with the program name to avoid clashing with
        # other completion files that might define similar helpers.
        f"function {has_opt_fn}",
        "    set -l tokens (commandline -cpx)",
        "    for opt in $argv",
        "        contains -- \"--$opt\" $tokens",
        "        and return 0",
        "        string match -q -- \"--$opt=*\" $tokens",
        "        and return 0",
        "    end",
        "    return 1",
        "end",
        "",
    ]
    # Per-option argument hints, keyed by long-option form. Each value is
    # the full fish spec for the argument -- it replaces the default ``-x``
    # (requires-arg, no file fallback) so flags that genuinely want paths
    # can opt back into file/directory completion.
    path_args = {
        "--dir": "-x -a '(__fish_complete_directories)'",
        "--crypt-key-file": "-r -F",
    }

    def emit(flag: dict, condition: str | None = None) -> None:
        # Every sub-parser has its own _HelpAction. Emit it once at the
        # global level (condition is None) and skip the duplicates that
        # would otherwise be produced inside each subcommand loop.
        if flag["is_help"] and condition is not None:
            return
        parts = [f"complete -c {prog}"]
        if condition:
            parts.append(f"-n {_fish_quote(condition)}")
        for opt in flag["option_strings"]:
            if opt.startswith("--"):
                parts.append(f"-l {opt[2:]}")
            elif opt.startswith("-"):
                parts.append(f"-s {opt[1:]}")
        if flag["takes_value"]:
            hint = next(
                (path_args[o] for o in flag["option_strings"] if o in path_args),
                None,
            )
            if hint is not None:
                parts.append(hint)
            else:
                # -x == -r -f : takes an arg, no file fallback. Choices (if
                # any) are the only suggestions fish will offer.
                parts.append("-x")
                if flag["choices"]:
                    parts.append(f"-a {_fish_quote(' '.join(flag['choices']))}")
        if flag["help"]:
            parts.append(f"-d {_fish_quote(flag['help'])}")
        out.append(" ".join(parts))

    # ``no_subcmd``: true when no subcommand has been picked yet. Used for
    # both global options and the subcommand list itself. We deliberately
    # avoid ``__fish_use_subcommand`` -- that helper treats ``/path/to/mmkv``
    # as a positional token (it doesn't know ``--dir`` takes a value) and
    # would suppress subcommand suggestions after any value-bearing global
    # flag. Negating ``__fish_seen_subcommand_from`` against the full
    # subcommand list is the robust pattern used by fish's own git/docker
    # completions.
    names = [s["name"] for s in spec["subcommands"]]
    no_subcmd = (
        f"not __fish_seen_subcommand_from {' '.join(names)}" if names else None
    )

    # Per-state subcommand guards (state machine driven by required globals).
    #
    # Three buckets drive the logic:
    #   * ``dir_guard`` -- every top-level ``required=True`` flag present.
    #     For this tool that's just ``--dir``.
    #   * ``selector_guard`` -- at least one of _INSTANCE_SELECTOR_LONGS
    #     present. Required for subcommands in _SUBCOMMANDS_NEEDING_INSTANCE
    #     because ``main()`` rejects them otherwise.
    #   * ``no_subcmd`` -- no subcommand has been picked yet.
    #
    # We use the emitted ``__mmkvdump_has_opt`` helper rather than fish's
    # built-in ``__fish_contains_opt`` so the check recognizes both the
    # ``--dir VAL`` and ``--dir=VAL`` spellings. ``__mmkvdump_has_opt a b``
    # returns true if EITHER ``--a`` or ``--b`` is on the command line.
    # Negation of a compound is done via ``not begin; ...; end``.
    dir_guard = "; and ".join(
        f"{has_opt_fn} {r}" for r in spec["required_longs"]
    ) or None
    selector_longs = list(_INSTANCE_SELECTOR_LONGS)
    selector_guard = (
        f"{has_opt_fn} {' '.join(selector_longs)}" if selector_longs else None
    )

    def _negate(g: str) -> str:
        return f"not begin; {g}; end"

    def _and(*parts: str | None) -> str | None:
        kept = [p for p in parts if p]
        return "; and ".join(kept) if kept else None

    def _find_global(long: str) -> dict | None:
        for g in spec["globals"]:
            if f"--{long}" in g["option_strings"]:
                return g
        return None

    # Global options: only valid before the subcommand (argparse rejects
    # ``mmkvdump dump --dir X``). --help is excepted because every
    # sub-parser accepts it too.
    out.append("# Global options (only before a subcommand; --help excepted)")
    for g in spec["globals"]:
        if g["is_help"]:
            emit(g)  # unconditioned: --help is valid at every level
        else:
            emit(g, condition=no_subcmd)
    out.append("")

    if spec["subcommands"]:
        out.append(
            "# Subcommand suggestions, narrowed by which required globals"
            " the user has supplied so far."
        )

        # State A: a top-level required flag is still missing. Offer the
        # missing flag(s) as a bare-TAB candidate so an empty `mmkvdump
        # <TAB>` guides the user to the next needed step.
        if dir_guard and no_subcmd:
            state_a = _and(_negate(dir_guard), no_subcmd)
            for long in spec["required_longs"]:
                g = _find_global(long)
                desc = g["help"] if g else ""
                tail = f" -d {_fish_quote(desc)}" if desc else ""
                out.append(
                    f"complete -c {prog} -n {_fish_quote(state_a)}"
                    f" -a '--{long}'{tail}"
                )

        always_avail = [
            s for s in spec["subcommands"]
            if s["name"] not in _SUBCOMMANDS_NEEDING_INSTANCE
        ]
        instance_scoped = [
            s for s in spec["subcommands"]
            if s["name"] in _SUBCOMMANDS_NEEDING_INSTANCE
        ]

        # Subcommands that only need ``--dir`` (typically just ``instances``).
        always_guard = _and(dir_guard, no_subcmd)
        for s in always_avail:
            desc = s["help"]
            tail = f" -d {_fish_quote(desc)}" if desc else ""
            guard_part = f" -n {_fish_quote(always_guard)}" if always_guard else ""
            out.append(
                f"complete -c {prog}{guard_part} -a {s['name']}{tail}"
            )

        # Subcommands that need ``--dir`` AND an instance selector.
        scoped_guard = _and(dir_guard, selector_guard, no_subcmd)
        for s in instance_scoped:
            desc = s["help"]
            tail = f" -d {_fish_quote(desc)}" if desc else ""
            guard_part = f" -n {_fish_quote(scoped_guard)}" if scoped_guard else ""
            out.append(
                f"complete -c {prog}{guard_part} -a {s['name']}{tail}"
            )

        # State B: ``--dir`` is present but no instance selector. Offer
        # the selector flags as bare-TAB candidates so an empty
        # `mmkvdump --dir X <TAB>` surfaces ``--id`` / ``--default``
        # alongside the one runnable subcommand (``instances``).
        if dir_guard and selector_guard and instance_scoped:
            state_b = _and(dir_guard, _negate(selector_guard), no_subcmd)
            for long in selector_longs:
                g = _find_global(long)
                desc = g["help"] if g else ""
                tail = f" -d {_fish_quote(desc)}" if desc else ""
                out.append(
                    f"complete -c {prog} -n {_fish_quote(state_b)}"
                    f" -a '--{long}'{tail}"
                )

        out.append("")

        out.append("# Subcommand-scoped options")
        for s in spec["subcommands"]:
            cond = f"__fish_seen_subcommand_from {s['name']}"
            for f in s["flags"]:
                emit(f, condition=cond)

    return "\n".join(out) + "\n"


def _completion_bash(parser: argparse.ArgumentParser) -> str:
    """Generate a bash shell completion script from parser metadata.

    Emits a single ``_{prog}_completion`` function registered with
    ``complete -F``. The function is a small state machine with four
    phases, each run on every TAB:

    1. If ``$prev`` is a value-taking flag, complete that flag's
       argument (path, enumerated choice, or nothing for opaque values).
    2. Walk the words after ``prog``, skipping flags and their values,
       to find the current subcommand if any.
    3. Inside a subcommand scope, suggest only that subcommand's flags.
    4. Before a subcommand, suggest globals when typing a ``-``-prefixed
       word, or the subcommand list when typing a bare word AND every
       top-level ``required=True`` flag is already on the command line.

    Unlike fish, bash has no ``__fish_contains_opt`` / ``__fish_seen_\
subcommand_from`` helpers, so the state detection is hand-rolled over
    ``COMP_WORDS``. The value-taking-flag list is emitted both in the
    ``case "$prev"`` dispatcher and in the subcommand-detection loop
    (as the ``skip_pattern``) so scanning correctly treats ``--dir
    /path`` as a flag-plus-value rather than a positional.
    """
    spec = _iter_parser_spec(parser)
    prog = spec["prog"]
    func = f"_{prog}_completion"

    # Path-completing flags: these are project-specific so the mapping
    # lives here (not in the walker) to keep the walker shell-neutral.
    path_compgen = {
        "--dir": "compgen -d",
        "--crypt-key-file": "compgen -f",
    }

    # Classify every value-taking flag into one of three categories:
    #   path_flags   -- completes via compgen -d / -f / ...
    #   enum_flags   -- completes from a fixed choice list
    #   opaque_flags -- takes a value but no useful completion (--id etc.)
    # Each flag is identified by its first long-option form.
    path_flags: dict[str, str] = {}
    enum_flags: dict[str, str] = {}
    opaque_flags: list[str] = []

    def long_name(flag: dict) -> str | None:
        return next(
            (o for o in flag["option_strings"] if o.startswith("--")),
            None,
        )

    def classify(flag: dict) -> None:
        if not flag["takes_value"]:
            return
        long = long_name(flag)
        if long is None:
            return
        if long in path_flags or long in enum_flags or long in opaque_flags:
            return
        if long in path_compgen:
            path_flags[long] = path_compgen[long]
        elif flag["choices"]:
            enum_flags[long] = " ".join(flag["choices"])
        else:
            opaque_flags.append(long)

    for g in spec["globals"]:
        classify(g)
    for s in spec["subcommands"]:
        for f in s["flags"]:
            classify(f)

    # --- case "$prev" in ... branches ---
    case_lines: list[str] = []
    for opt, expr in path_flags.items():
        case_lines.append(
            f'        {opt})\n'
            f'            COMPREPLY=( $({expr} -- "$cur") )\n'
            f'            return\n'
            f'            ;;'
        )
    for opt, choices in enum_flags.items():
        case_lines.append(
            f'        {opt})\n'
            f'            COMPREPLY=( $(compgen -W "{choices}" -- "$cur") )\n'
            f'            return\n'
            f'            ;;'
        )
    if opaque_flags:
        case_lines.append(
            f'        {"|".join(opaque_flags)})\n'
            f'            return\n'
            f'            ;;'
        )
    case_block = "\n".join(case_lines)

    # --- Flag-name lists ---
    def name_list(flags: list[dict]) -> str:
        """Flatten to a space-separated list of option strings, deduped."""
        names: list[str] = []
        for f in flags:
            for opt in f["option_strings"]:
                if opt not in names:
                    names.append(opt)
        return " ".join(names)

    globals_names = name_list(spec["globals"])

    subcmd_case_lines: list[str] = []
    for s in spec["subcommands"]:
        flags_str = name_list(s["flags"])
        if flags_str:
            subcmd_case_lines.append(
                f'                {s["name"]})\n'
                f'                    COMPREPLY=( $(compgen -W "{flags_str}" -- "$cur") )\n'
                f'                    ;;'
            )
    subcmd_case_block = "\n".join(subcmd_case_lines)

    # --- Subcommand-detection skip pattern (value-taking globals) ---
    skip_longs: list[str] = []
    for g in spec["globals"]:
        if g["takes_value"]:
            long = long_name(g)
            if long and long not in skip_longs:
                skip_longs.append(long)
    skip_pattern = "|".join(skip_longs) if skip_longs else "--__NONE__"

    subcmd_names = " ".join(s["name"] for s in spec["subcommands"])

    # --- Phase 4b state machine ---
    # Build a sequence of "steps" where each step is a required
    # precondition that must be met before certain subcommands become
    # runnable. For this tool the steps are:
    #
    #   1. `--dir` must be present (argparse-level required=True).
    #   2. An instance selector (`--id` or `--default`) must be present
    #      if any _SUBCOMMANDS_NEEDING_INSTANCE subcommand is to be
    #      offered. `instances` is always offered once step 1 is met.
    #
    # Each step's patterns are matched in one COMP_WORDS pass that
    # sets `has_<var>=1` when encountered. The if/elif/else chain then
    # suggests the next needed flag(s) -- so an empty `mmkvdump <TAB>`
    # teaches the user to type `--dir`, and `mmkvdump --dir X <TAB>`
    # teaches them to add `--id`/`--default` (alongside `instances`,
    # which is runnable in that state).

    def _patterns_for(long: str) -> list[str]:
        """Bash case patterns for a long option. Value-taking flags
        also match the `--long=*` inline form."""
        pats = [f"--{long}"]
        for g in spec["globals"]:
            if f"--{long}" in g["option_strings"]:
                if g["takes_value"]:
                    pats.append(f"--{long}=*")
                return pats
        pats.append(f"--{long}=*")  # unknown: be permissive
        return pats

    always_avail_names = [
        s["name"] for s in spec["subcommands"]
        if s["name"] not in _SUBCOMMANDS_NEEDING_INSTANCE
    ]
    has_instance_scoped = any(
        s["name"] in _SUBCOMMANDS_NEEDING_INSTANCE for s in spec["subcommands"]
    )
    selector_longs_list = list(_INSTANCE_SELECTOR_LONGS)

    steps: list[dict] = []
    for long in spec["required_longs"]:
        steps.append({
            "var": f"has_{long}",
            "patterns": _patterns_for(long),
            "unmet_candidates": [f"--{long}"],
        })
    if selector_longs_list and has_instance_scoped:
        sel_pats: list[str] = []
        for long in selector_longs_list:
            sel_pats.extend(_patterns_for(long))
        steps.append({
            "var": "has_selector",
            "patterns": sel_pats,
            "unmet_candidates": (
                always_avail_names + [f"--{long}" for long in selector_longs_list]
            ),
        })

    all_subcmd_names = " ".join(s["name"] for s in spec["subcommands"])

    if steps:
        scan_case_lines = [
            f'            {"|".join(s["patterns"])}) {s["var"]}=1 ;;'
            for s in steps
        ]
        phase4b_lines = [
            "    local " + " ".join(f"{s['var']}=0" for s in steps),
            '    for w in "${COMP_WORDS[@]:1}"; do',
            '        case "$w" in',
            *scan_case_lines,
            '        esac',
            '    done',
            "",
        ]
        for i, s in enumerate(steps):
            kw = "if" if i == 0 else "elif"
            cand = " ".join(s["unmet_candidates"])
            phase4b_lines.append(f'    {kw} [ ${s["var"]} -eq 0 ]; then')
            phase4b_lines.append(
                f'        COMPREPLY=( $(compgen -W "{cand}" -- "$cur") )'
            )
        phase4b_lines.append('    else')
        phase4b_lines.append(
            f'        COMPREPLY=( $(compgen -W "{all_subcmd_names}" -- "$cur") )'
        )
        phase4b_lines.append('    fi')
    else:
        phase4b_lines = [
            f'    COMPREPLY=( $(compgen -W "{all_subcmd_names}" -- "$cur") )'
        ]
    phase4b_block = "\n".join(phase4b_lines)

    return f"""# bash completion for {prog} -- generated by `{prog} --completion bash`
# Do not edit by hand; regenerate after upgrading {prog}.

{func}() {{
    local cur prev
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev="${{COMP_WORDS[COMP_CWORD-1]}}"
    COMPREPLY=()

    # Phase 1: if $prev is a value-taking flag, complete its argument.
    case "$prev" in
{case_block}
    esac

    # Phase 2: find the current subcommand, if any. Walk tokens after
    # `{prog}`, skipping known value-taking flags and their values.
    local subcmd="" i=1 skip=0
    while [ $i -lt $COMP_CWORD ]; do
        local w="${{COMP_WORDS[i]}}"
        if [ $skip -eq 1 ]; then
            skip=0
        else
            case "$w" in
                {skip_pattern})
                    skip=1
                    ;;
                -*)
                    ;;
                *)
                    subcmd="$w"
                    break
                    ;;
            esac
        fi
        i=$((i + 1))
    done

    # Phase 3: inside a subcommand scope. Only suggest that subcommand's
    # flags when the user is actually typing a `-`-prefixed word; bare
    # tokens are positionals (e.g. `get <key>`) that we can't enumerate,
    # so TAB returns nothing rather than polluting the menu with flags.
    if [ -n "$subcmd" ]; then
        if [[ "$cur" == -* ]]; then
            case "$subcmd" in
{subcmd_case_block}
            esac
        fi
        return
    fi

    # Phase 4a: pre-subcommand, typing a flag -> suggest globals.
    if [[ "$cur" == -* ]]; then
        COMPREPLY=( $(compgen -W "{globals_names}" -- "$cur") )
        return
    fi

    # Phase 4b: pre-subcommand, typing a bare word -> surface the next
    # runnable step. The state machine is assembled by the generator so
    # the available candidates depend on which required globals the user
    # has already supplied (see the ``steps`` list in _completion_bash).
{phase4b_block}
}}

complete -F {func} {prog}
"""


def _completion_zsh(parser: argparse.ArgumentParser) -> str:
    """Generate a zsh shell completion script from parser metadata.

    Emits a ``#compdef``-style file that defines ``_{prog}`` using
    zsh's declarative ``_arguments`` DSL. Globals are listed inline on
    the top-level ``_arguments`` call; the subcommand positional
    dispatches via the ``->args`` state so each sub-parser can then
    call ``_arguments`` again with its own flag set.

    The required-flag gate (subcommand list is only suggested when
    --dir is already present) lives inside ``_{prog}_commands``, which
    walks ``$words`` and returns empty when a required flag is
    missing. Same semantics as fish's ``__fish_contains_opt`` guard
    and bash's COMP_WORDS loop.
    """
    spec = _iter_parser_spec(parser)
    prog = spec["prog"]
    func = f"_{prog}"
    commands_func = f"_{prog}_commands"

    # Project-specific path completion hints.
    path_actions = {
        "--dir": "_directories",
        "--crypt-key-file": "_files",
    }

    def spec_entries(flag: dict) -> list[str]:
        """Build ``_arguments`` spec entries for one flag.

        Returns a list because multi-form flags (e.g. ``-h/--help``)
        emit one entry per form, each with an exclusion prefix so that
        having one form suppresses the other. Single-form flags return
        a one-element list.
        """
        opts = flag["option_strings"]
        # Escape `[` / `]` inside the description (unlikely for this
        # parser but keep the generator robust).
        safe_desc = flag["help"].replace("[", "\\[").replace("]", "\\]")
        takes_value = flag["takes_value"]

        if takes_value:
            long = next((o for o in opts if o.startswith("--")), opts[0])
            msg_name = long.lstrip("-")
            if long in path_actions:
                action = path_actions[long]
            elif flag["choices"]:
                action = "(" + " ".join(flag["choices"]) + ")"
            else:
                action = ""
            tail = f"[{safe_desc}]:{msg_name}:{action}"
        else:
            tail = f"[{safe_desc}]"

        def form(opt: str) -> str:
            # zsh marks value-taking long options with ``=`` and short
            # options with ``+`` so both ``--foo VAL`` and ``--foo=VAL``
            # forms are accepted.
            if not takes_value:
                return opt + tail
            return (opt + "=" + tail) if opt.startswith("--") else (opt + "+" + tail)

        if len(opts) == 1:
            return [_zsh_quote(form(opts[0]))]

        # Multi-form: ``(-h --help)-h[desc]`` per form so zsh suppresses
        # the other alias once one is already on the line.
        exclusion = "(" + " ".join(opts) + ")"
        return [_zsh_quote(exclusion + form(opt)) for opt in opts]

    # --- Global spec entries ---
    global_entries: list[str] = []
    for g in spec["globals"]:
        global_entries.extend(spec_entries(g))

    # --- Subcommand dispatch cases ---
    # Each case calls `_arguments` again with its own flag set AND any
    # positional args derived from the sub-parser (e.g. `get <key>`).
    subcmd_case_blocks: list[str] = []
    for s in spec["subcommands"]:
        inner_entries: list[str] = []
        for f in s["flags"]:
            inner_entries.extend(spec_entries(f))
        for idx, pos in enumerate(s["positionals"], start=1):
            # Positional spec: "N:msg:action". We use an empty action
            # because the only positionals in this tool are MMKV keys,
            # which we cannot enumerate without opening the database.
            inner_entries.append(_zsh_quote(f"{idx}:{pos['name']}:"))

        if inner_entries:
            args_body = " \\\n                        ".join(inner_entries)
            subcmd_case_blocks.append(
                f"                {s['name']})\n"
                f"                    _arguments \\\n"
                f"                        {args_body}\n"
                f"                    ;;"
            )
        else:
            # Subcommand with no flags or positionals (shouldn't happen
            # in practice because every sub-parser has at least -h/--help,
            # but keep the branch for future-proofness).
            subcmd_case_blocks.append(
                f"                {s['name']})\n"
                f"                    ;;"
            )
    subcmd_case_body = "\n".join(subcmd_case_blocks)

    # --- State-machine body for _{prog}_commands ---
    # The helper walks ``$words`` to detect which required globals are
    # on the command line, then branches the candidate list. The same
    # sequence of states applies across all three shells: missing
    # required flag -> offer that flag; required met but selector
    # missing -> offer ``instances`` plus the selector flags;
    # everything met -> offer every subcommand.

    def _find_global_zsh(long: str) -> dict | None:
        for g in spec["globals"]:
            if f"--{long}" in g["option_strings"]:
                return g
        return None

    def _entry(name: str, help_text: str) -> str:
        """Build a ``_describe`` array entry with ``:`` escaped."""
        safe = (help_text or "").replace(":", "\\:")
        return _zsh_quote(f"{name}:{safe}")

    def _zsh_patterns_for(long: str) -> list[str]:
        pats = [f"--{long}"]
        g = _find_global_zsh(long)
        if g is None or g["takes_value"]:
            pats.append(f"--{long}=*")
        return pats

    _always_avail = [
        s for s in spec["subcommands"]
        if s["name"] not in _SUBCOMMANDS_NEEDING_INSTANCE
    ]
    _has_instance_scoped = len(_always_avail) < len(spec["subcommands"])
    _selector_longs = list(_INSTANCE_SELECTOR_LONGS)

    zsh_steps: list[tuple[str, list[str], list[str]]] = []
    for long in spec["required_longs"]:
        g = _find_global_zsh(long)
        zsh_steps.append((
            f"has_{long}",
            _zsh_patterns_for(long),
            [_entry(f"--{long}", g["help"] if g else "")],
        ))
    if _selector_longs and _has_instance_scoped:
        sel_pats: list[str] = []
        for long in _selector_longs:
            sel_pats.extend(_zsh_patterns_for(long))
        unmet_entries = [
            _entry(s["name"], s["help"]) for s in _always_avail
        ]
        for long in _selector_longs:
            g = _find_global_zsh(long)
            unmet_entries.append(_entry(f"--{long}", g["help"] if g else ""))
        zsh_steps.append(("has_selector", sel_pats, unmet_entries))

    _all_entries = [_entry(s["name"], s["help"]) for s in spec["subcommands"]]

    if zsh_steps:
        var_decl = " ".join(f"{v}=0" for v, _, _ in zsh_steps)
        scan_clauses = [
            f'            {"|".join(pats)}) {var}=1 ;;'
            for var, pats, _ in zsh_steps
        ]
        func_lines = [
            f"    local i {var_decl}",
            '    for (( i=2; i<=${#words}; i++ )); do',
            '        case "${words[i]}" in',
            *scan_clauses,
            '        esac',
            '    done',
            "",
            "    local -a commands",
        ]
        for i, (var, _, unmet) in enumerate(zsh_steps):
            kw = "if" if i == 0 else "elif"
            entries_block = "\n            ".join(unmet)
            func_lines.append(f'    {kw} [[ ${var} -eq 0 ]]; then')
            func_lines.append('        commands=(')
            func_lines.append(f'            {entries_block}')
            func_lines.append('        )')
        func_lines.append('    else')
        all_block = "\n            ".join(_all_entries)
        func_lines.append('        commands=(')
        func_lines.append(f'            {all_block}')
        func_lines.append('        )')
        func_lines.append('    fi')
        func_lines.append("")
        func_lines.append(
            f"    _describe -t commands '{prog} next step' commands"
        )
    else:
        # No required/selector gating; just offer every subcommand.
        func_lines = ["    local -a commands", "    commands=("]
        for e in _all_entries:
            func_lines.append(f"        {e}")
        func_lines.append("    )")
        func_lines.append(
            f"    _describe -t commands '{prog} command' commands"
        )
    commands_func_body = "\n".join(func_lines)

    global_args_body = (
        " \\\n        ".join(global_entries)
        + " \\\n        '1:command:" + commands_func + "'"
        + " \\\n        '*::arg:->args'"
    )

    return f"""#compdef {prog}
# zsh completion for {prog} -- generated by `{prog} --completion zsh`
# Do not edit by hand; regenerate after upgrading {prog}.

{func}() {{
    local state line curcontext="$curcontext"

    _arguments -C \\
        {global_args_body}

    case $state in
        args)
            case "$line[1]" in
{subcmd_case_body}
            esac
            ;;
    esac
}}

{commands_func}() {{
    # State machine: inspect $words to detect which required globals
    # are present, then branch the candidate list. Identical semantics
    # to fish's per-state __mmkvdump_has_opt guards and bash's Phase 4b.
{commands_func_body}
}}

{func} "$@"
"""


class _CompletionAction(argparse.Action):
    """Emit a shell completion script and exit.

    Mirrors ``--version``'s short-circuit semantics: runs during argparse's
    argument processing, before ``--dir`` (or any other ``required=True``
    flag) is validated, so ``mmkvdump --completion fish`` works standalone.
    """

    def __init__(
        self,
        option_strings: list[str],
        dest: str = argparse.SUPPRESS,
        default: Any = argparse.SUPPRESS,
        choices: Any = None,
        help: str | None = None,
    ) -> None:
        super().__init__(
            option_strings=option_strings,
            dest=dest,
            default=default,
            nargs=None,  # exactly one value, constrained by choices
            choices=choices,
            help=help,
        )

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        if values == "bash":
            sys.stdout.write(_completion_bash(parser))
        elif values == "fish":
            sys.stdout.write(_completion_fish(parser))
        elif values == "zsh":
            sys.stdout.write(_completion_zsh(parser))
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mmkvdump",
        description="MMKV Dump -- browse and inspect MMKV databases",
        epilog=_USAGE_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--completion", action=_CompletionAction, choices=("bash", "fish", "zsh"),
        help="Print a shell completion script for the given shell and exit",
    )

    # Global options
    parser.add_argument("--dir", required=True, help="Path to the MMKV root directory")

    # --id / --default are required for instance-scoped commands but not for
    # `instances`. We validate this manually in main() so `instances` can be
    # run without them.
    id_group = parser.add_mutually_exclusive_group()
    id_group.add_argument("--id", help="MMKV instance ID")
    id_group.add_argument("--default", action="store_true", help="Use the default MMKV instance")

    key_group = parser.add_mutually_exclusive_group()
    key_group.add_argument("--crypt-key", default=None, help="Encryption key (visible in `ps` output)")
    key_group.add_argument(
        "--crypt-key-file", default=None,
        help="Read the encryption key from a file (preferred over --crypt-key for security)",
    )

    parser.add_argument(
        "--single-process", action="store_true",
        help="Open in single-process mode (default: multi-process, safer when the app is running)",
    )
    parser.add_argument("--no-color", action="store_true", help="Disable syntax highlighting")
    parser.add_argument(
        "--log-level", choices=tuple(_LOG_LEVELS.keys()), default="none",
        help="MMKV log level (default: none)",
    )

    sub = parser.add_subparsers(dest="command", required=True, help="Command to execute")

    sub.add_parser("instances", help="List MMKV instance IDs found in --dir")

    p_keys = sub.add_parser("keys", help="List all keys")
    p_keys.add_argument("--grep", default=None, help="Filter keys by regex pattern (case-insensitive)")

    p_get = sub.add_parser("get", help="Get value of a key")
    p_get.add_argument("key", help="The key to read")
    get_mode_group = p_get.add_mutually_exclusive_group()
    get_mode_group.add_argument(
        "--type", choices=_TYPE_CHOICES, default=None,
        help="Force reading as this type (default: auto-infer)",
    )
    get_mode_group.add_argument(
        "--raw", action="store_true",
        help="Show raw bytes (hex dump) without type inference",
    )

    p_dump = sub.add_parser("dump", help="Dump all key-value pairs")
    p_dump.add_argument("--grep", default=None, help="Filter keys by regex pattern (case-insensitive)")
    p_dump.add_argument(
        "--full", action="store_true",
        help="Show full values without truncation (ignored with --format json)",
    )
    p_dump.add_argument(
        "--format", choices=("text", "json"), default="text",
        help="Output format (default: text)",
    )

    p_raw = sub.add_parser("raw", help="Show raw bytes and all type interpretations")
    p_raw.add_argument("key", help="The key to inspect")

    return parser


def _load_crypt_key(args: argparse.Namespace) -> int:
    """Resolve --crypt-key-file into args.crypt_key. Returns exit code."""
    if args.crypt_key_file is None:
        return 0
    try:
        with open(args.crypt_key_file, "rb") as f:
            data = f.read()
    except OSError as e:
        print(f"Error reading crypt key file: {e}", file=sys.stderr)
        return 1
    # Strict decode: silently replacing invalid bytes would corrupt the
    # key and leave the user wondering why MMKV can't decrypt.
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError as e:
        print(
            f"Error: crypt key file {args.crypt_key_file} is not valid UTF-8: {e}",
            file=sys.stderr,
        )
        return 1
    # Strip trailing whitespace/newlines introduced by text editors.
    args.crypt_key = decoded.strip()
    if not args.crypt_key:
        print(f"Error: crypt key file {args.crypt_key_file} is empty", file=sys.stderr)
        return 1
    return 0


def _resolve_mode(args: argparse.Namespace) -> mmkv.MMKVMode:
    """Compute the MMKVMode flag based on CLI flags.

    Attempts to apply ReadOnly (since this tool never writes) when the
    binding's MMKVMode is an IntFlag that supports bitwise composition.
    Silently falls back to the plain process mode otherwise -- for
    example when MMKVMode is a regular IntEnum, `A | B` returns a bare
    int that pybind11's strict type checking will refuse.
    """
    mode: mmkv.MMKVMode = (
        mmkv.MMKVMode.SingleProcess if args.single_process
        else mmkv.MMKVMode.MultiProcess
    )
    read_only = getattr(mmkv.MMKVMode, "ReadOnly", None)
    if read_only is not None:
        try:
            combined = mode | read_only
        except TypeError:
            return mode
        # IntFlag OR returns a flag instance; IntEnum OR returns a plain
        # int. Only keep the composed value if it is still an MMKVMode.
        if isinstance(combined, mmkv.MMKVMode):
            mode = combined
    return mode


def _open_mmkv(args: argparse.Namespace) -> mmkv.MMKV:
    """Open the MMKV instance selected by the CLI flags."""
    mode = _resolve_mode(args)
    if args.default:
        if args.crypt_key:
            return mmkv.MMKV.defaultMMKV(mode, args.crypt_key)
        return mmkv.MMKV.defaultMMKV(mode)
    if args.crypt_key:
        return mmkv.MMKV(args.id, mode, args.crypt_key)
    return mmkv.MMKV(args.id, mode)


def _install_sigpipe_handler() -> None:
    """Restore default SIGPIPE so piping to `head`/`less` doesn't leave
    an ugly BrokenPipeError traceback.

    No-op on platforms without SIGPIPE (Windows) or when called from a
    non-main thread.
    """
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass


def main() -> int:
    _install_sigpipe_handler()

    parser = build_parser()
    args = parser.parse_args()

    # Expand `~` in user-supplied paths so quoted paths like "~/foo"
    # (which the shell would not expand) still work.
    args.dir = os.path.expanduser(args.dir)
    if args.crypt_key_file:
        args.crypt_key_file = os.path.expanduser(args.crypt_key_file)

    # Validate --dir once, up front.
    if not os.path.isdir(args.dir):
        print(f"Error: directory does not exist: {args.dir}", file=sys.stderr)
        return 1

    # `instances` only needs --dir; short-circuit before any MMKV init.
    if args.command == "instances":
        return cmd_instances(args)

    # All other commands (listed in _SUBCOMMANDS_NEEDING_INSTANCE at the
    # top of the file) operate on a specific instance and require one of
    # _INSTANCE_SELECTOR_LONGS. The constants are the single source of
    # truth; the completion generators read them to narrow tab-completion
    # so both the runtime check and the tab-suggestions stay consistent.
    if not args.default and (args.id is None or args.id.strip() == ""):
        parser.error("one of --id or --default is required for this command")

    # Reject an empty --crypt-key before it silently falls back to
    # non-encrypted mode and produces confusing "cannot decrypt" errors.
    if args.crypt_key == "":
        parser.error("--crypt-key cannot be empty")

    rc = _load_crypt_key(args)
    if rc != 0:
        return rc

    log_level = _LOG_LEVELS[args.log_level]
    if args.log_level != "none":
        mmkv.MMKV.initializeMMKV(args.dir, log_level, _mmkv_logger)
    else:
        mmkv.MMKV.initializeMMKV(args.dir, log_level)

    mmkv.MMKV.registerErrorHandler(_error_handler)
    mmkv.MMKV.registerContentChangeHandler(_content_change_handler)

    try:
        kv = _open_mmkv(args)
        commands = {
            "keys": cmd_keys,
            "get": cmd_get,
            "dump": cmd_dump,
            "raw": cmd_raw,
        }
        return commands[args.command](kv, args)
    finally:
        mmkv.MMKV.onExit()


if __name__ == "__main__":
    sys.exit(main())
