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

__version__ = "1.1"

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

  # Generate fish shell completion (one-time install)
  mmkvdump --completion fish > ~/.config/fish/completions/mmkvdump.fish
"""

# Maximum column width for a truncated value in `dump` text output.
_DUMP_TRUNCATE_AT = 120
_DUMP_ELLIPSIS_AT = _DUMP_TRUNCATE_AT - 3  # leave room for "..."

# Preview length for string values shown by the `raw` subcommand.
_RAW_STRING_PREVIEW = 200
_RAW_STRING_ELLIPSIS = _RAW_STRING_PREVIEW - 3

_TYPE_CHOICES = (
    "string", "bool", "int32", "uint32", "int64", "uint64", "float", "bytes",
)

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
    print(f"  Int32:   {kv.getInt(key)}")
    print(f"  UInt32:  {kv.getUInt(key)}")
    print(f"  Int64:   {kv.getLongInt(key)}")
    print(f"  UInt64:  {kv.getLongUInt(key)}")
    print(f"  Float:   {kv.getFloat(key)}")
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


def _completion_fish(parser: argparse.ArgumentParser) -> str:
    """Generate a fish shell completion script from argparse metadata.

    Walks ``parser._actions`` and the ``_SubParsersAction`` map to extract
    flags, subcommands, argument choices, and help strings. ``_actions`` is
    private, but stable across modern Python releases -- worth the coupling
    to keep the parser as the single source of truth for completion output.
    """
    prog = parser.prog
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
    ]
    # Per-option argument hints, keyed by long-option form. Each value is
    # the full fish spec for the argument -- it replaces the default ``-x``
    # (requires-arg, no file fallback) so flags that genuinely want paths
    # can opt back into file/directory completion.
    path_args = {
        "--dir": "-x -a '(__fish_complete_directories)'",
        "--crypt-key-file": "-r -F",
    }

    def emit(action: argparse.Action, condition: str | None = None) -> None:
        if isinstance(action, argparse._SubParsersAction):
            return
        # Every sub-parser has its own _HelpAction. Emit it once at the
        # global level (condition is None) and skip the duplicates that
        # would otherwise be produced inside each subcommand loop.
        if isinstance(action, argparse._HelpAction) and condition is not None:
            return
        if not action.option_strings:  # skip positionals
            return
        parts = [f"complete -c {prog}"]
        if condition:
            parts.append(f"-n {_fish_quote(condition)}")
        for opt in action.option_strings:
            if opt.startswith("--"):
                parts.append(f"-l {opt[2:]}")
            elif opt.startswith("-"):
                parts.append(f"-s {opt[1:]}")
        if action.nargs != 0:  # the option takes a value
            hint = next(
                (path_args[o] for o in action.option_strings if o in path_args),
                None,
            )
            if hint is not None:
                parts.append(hint)
            else:
                # -x == -r -f : takes an arg, no file fallback. Choices (if
                # any) are the only suggestions fish will offer.
                parts.append("-x")
                if action.choices:
                    parts.append(f"-a {_fish_quote(' '.join(map(str, action.choices)))}")
        if action.help:
            parts.append(f"-d {_fish_quote(action.help)}")
        out.append(" ".join(parts))

    sub_action = next(
        (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)),
        None,
    )
    names = list(sub_action.choices) if sub_action is not None else []

    # ``no_subcmd``: true when no subcommand has been picked yet. Used for
    # both global options and the subcommand list itself. We deliberately
    # avoid ``__fish_use_subcommand`` -- that helper treats ``/path/to/mmkv``
    # as a positional token (it doesn't know ``--dir`` takes a value) and
    # would suppress subcommand suggestions after any value-bearing global
    # flag. Negating ``__fish_seen_subcommand_from`` against the full
    # subcommand list is the robust pattern used by fish's own git/docker
    # completions.
    no_subcmd = (
        f"not __fish_seen_subcommand_from {' '.join(names)}" if names else None
    )

    # Additional prerequisite for the subcommand list: every top-level
    # ``required=True`` flag must already be on the command line. Without
    # this the user can tab-complete ``mmkvdump <TAB>`` into ``dump``,
    # hit enter, and be rejected by argparse for missing --dir. We gate
    # ONLY the subcommand list on this (not the global options, which
    # include --dir itself -- gating --dir on --dir-being-present would
    # make --dir unreachable). ``__fish_contains_opt`` matches the
    # ``--dir VAL`` form but not the inline ``--dir=VAL`` form; the
    # latter is an unlikely corner case for this tool's examples.
    required_longs = list(dict.fromkeys(
        o[2:]
        for a in parser._actions
        if (a.option_strings and a.required
            and not isinstance(a, argparse._SubParsersAction))
        for o in a.option_strings
        if o.startswith("--")
    ))
    subcmd_guard_parts = [f"__fish_contains_opt {o}" for o in required_longs]
    if no_subcmd:
        subcmd_guard_parts.append(no_subcmd)
    subcmd_guard = "; and ".join(subcmd_guard_parts) if subcmd_guard_parts else None

    # Global options: only valid before the subcommand (argparse rejects
    # ``mmkvdump dump --dir X``). --help is excepted because every
    # sub-parser accepts it too.
    out.append("# Global options (only before a subcommand; --help excepted)")
    for a in parser._actions:
        if a is sub_action:
            continue
        if isinstance(a, argparse._HelpAction):
            emit(a)  # unconditioned: --help is valid at every level
        else:
            emit(a, condition=no_subcmd)
    out.append("")

    if sub_action is not None:
        out.append(
            "# Subcommands (only after all required globals have been supplied"
            " and before one is picked)"
        )
        sub_help = {ca.dest: (ca.help or "") for ca in sub_action._choices_actions}
        for name in names:
            desc = sub_help.get(name, "")
            tail = f" -d {_fish_quote(desc)}" if desc else ""
            out.append(
                f"complete -c {prog} -n {_fish_quote(subcmd_guard)} -a {name}{tail}"
            )
        out.append("")

        out.append("# Subcommand-scoped options")
        for name, sp in sub_action.choices.items():
            cond = f"__fish_seen_subcommand_from {name}"
            for a in sp._actions:
                emit(a, condition=cond)

    return "\n".join(out) + "\n"


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
        if values == "fish":
            sys.stdout.write(_completion_fish(parser))
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
        "--completion", action=_CompletionAction, choices=("fish",),
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

    # All other commands require a concrete MMKV instance selection.
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
