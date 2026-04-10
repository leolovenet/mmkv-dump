# mmkvdump

A command-line tool for browsing and inspecting [MMKV](https://github.com/Tencent/MMKV) databases.

MMKV is Tencent's key-value storage framework used widely in Android and iOS apps.
`mmkvdump` lets you look inside those databases from the terminal: list instances,
list keys, read individual values with automatic type inference, or dump every
key-value pair in either human-readable or JSON format.

## Features

- **Instance discovery** — scan a directory to list every MMKV instance
- **Key listing and filtering** — plain or regex-filtered (case-insensitive)
- **Automatic type inference** — guesses string / JSON / int / float / bool / bytes
- **Forced typing** — read as a specific type when auto-inference is wrong
- **Raw hex view** — `get --raw` or the dedicated `raw` subcommand
- **JSON dump** — pipe straight into `jq`
- **Syntax-highlighted JSON** — when pygments is installed and stdout is a TTY
- **Encryption keys** — accepted inline (`--crypt-key`) or from a file (`--crypt-key-file`)
- **Safe defaults** — read-only mode when the binding supports it, multi-process mode otherwise

## Prerequisites

### Required

- **Python 3.10+**
- **[MMKV for Python](https://github.com/Tencent/MMKV/wiki/python_setup)** — install this first,
  the script will refuse to start otherwise.

### Optional

- **[pygments](https://pypi.org/project/Pygments/)** — enables syntax highlighting
  for JSON values. Install with `python3 -m pip install pygments`.

## Installation

### Step 1 — Install the `mmkv` Python binding

`mmkv` is not on PyPI. Build it from source following the upstream
instructions at <https://github.com/Tencent/MMKV/wiki/python_setup>,
under whichever Python interpreter you plan to run `mmkvdump` with —
that's the interpreter that will be able to `import mmkv` afterwards.

Optional: `python3 -m pip install pygments` (under the same interpreter)
for JSON syntax highlighting.

Verify the install succeeded **under the same interpreter** you plan to use:

```bash
python3   -c "import mmkv; print(mmkv)"   # or python3.11, etc.
```

### Step 2 — Install the script

Pick the pattern that matches your Python setup.

#### Pattern A — Simple symlink

If `python3 -c "import mmkv"` works, the script's shebang
(`#!/usr/bin/env python3`) will do the right thing. Just symlink the
script onto your `$PATH`:

```bash
ln -s "$(pwd)/mmkvdump.py" /usr/local/bin/mmkvdump
```

#### Pattern B — Wrapper script

If your default `python3` does **not** have `mmkv` installed — common when
you have multiple Python versions (Homebrew `python@3.10`, `python@3.11`,
etc.), use `pyenv`, or keep `mmkv` inside a virtualenv — create a small
wrapper that pins the right interpreter. Save the following as
`~/bin/mmkvdump` (or anywhere on your `$PATH`):

```bash
#!/bin/bash
exec python3.11 /path/to/mmkv-dump/mmkvdump.py "$@"
```

Then make it executable:

```bash
chmod +x ~/bin/mmkvdump
```

Tune to taste:

- Replace `python3.11` with the interpreter where `mmkv` actually lives
  (e.g. `~/.venvs/mmkv/bin/python`, `/opt/homebrew/bin/python3.10`, …).
- Replace `/path/to/mmkv-dump/mmkvdump.py` with the absolute path to the
  script in this repository.

Two details in the wrapper matter:

- **`exec`** replaces the bash process with Python, avoiding a dangling
  shell and making signal handling (Ctrl-C, SIGPIPE) behave correctly.
- **`"$@"`** — **quote it!** Without the quotes, arguments containing
  spaces or shell metacharacters get re-split by the shell, so flags like
  `--grep 'session .*'` silently break.

### Step 3 — Verify

```bash
mmkvdump --version
mmkvdump --help
```

## Shell completion

`mmkvdump` can emit a completion script for fish or bash, derived directly
from the argparse metadata so the completions stay in sync with the tool.
zsh support is planned.

Tab-completion covers subcommands, global flags, subcommand-specific
flags, enumerated choices (`--type`, `--format`, `--log-level`), and path
arguments (`--dir` offers directories, `--crypt-key-file` offers files).
The subcommand list is gated on `--dir` being present so tab won't fill
in a subcommand argparse would then reject.

### Fish

```bash
mmkvdump --completion fish > ~/.config/fish/completions/mmkvdump.fish
exec fish   # reload the current session
```

### Bash

Install into the XDG per-user directory scanned by the `bash-completion`
package (the common case on modern distributions and on macOS via
Homebrew's `bash-completion@2`):

```bash
mkdir -p ~/.local/share/bash-completion/completions
mmkvdump --completion bash > ~/.local/share/bash-completion/completions/mmkvdump
```

If you don't have `bash-completion` installed, source the script
directly from your `~/.bashrc` instead:

```bash
mmkvdump --completion bash > ~/.mmkvdump-completion.bash
echo 'source ~/.mmkvdump-completion.bash' >> ~/.bashrc
```

Start a new bash session (or `source` the file) for the completions to
take effect.

## Usage

```
mmkvdump --dir <mmkv-directory> [options] <subcommand>
```

### Subcommands

| Command       | Description                                                 |
|---------------|-------------------------------------------------------------|
| `instances`   | List MMKV instance IDs found in `--dir`                     |
| `keys`        | List all keys (optionally filtered by `--grep <regex>`)     |
| `get <key>`   | Read a single value                                         |
| `dump`        | Dump every key-value pair                                   |
| `raw <key>`   | Show raw bytes plus every type interpretation for debugging |

### Global options

| Flag                     | Description                                                    |
|--------------------------|----------------------------------------------------------------|
| `--dir <path>`           | **Required.** Path to the MMKV root directory                  |
| `--id <name>`            | MMKV instance ID                                               |
| `--default`              | Use the default MMKV instance (mutually exclusive with `--id`) |
| `--crypt-key <str>`      | Encryption key (visible in `ps` output)                        |
| `--crypt-key-file <f>`   | Read the encryption key from a file (preferred)                |
| `--single-process`       | Open in single-process mode (default: multi-process)           |
| `--no-color`             | Disable syntax highlighting                                    |
| `--log-level <level>`    | MMKV log verbosity: `none`/`debug`/`info`/`warning`/`error`    |
| `--completion <shell>`   | Print a shell completion script (currently: `fish`) and exit   |
| `--version`              | Print version and exit                                         |

## Examples

```bash
# Discover all MMKV instances in an Android app's data directory
mmkvdump --dir ~/app-data/com.example/mmkv instances

# List every key in one instance
mmkvdump --dir ~/app-data/com.example/mmkv --id user_prefs keys

# Filter keys by regex
mmkvdump --dir ~/app-data/com.example/mmkv --id user_prefs keys --grep '^session_'

# Read a single value (auto-infer the type)
mmkvdump --dir ~/app-data/com.example/mmkv --id user_prefs get last_login_time

# Force a specific type when auto-inference is wrong
mmkvdump --dir ~/app-data/com.example/mmkv --id user_prefs get some_key --type int64

# Dump everything, compact text format
mmkvdump --dir ~/app-data/com.example/mmkv --id user_prefs dump

# Dump as JSON and pipe to jq
mmkvdump --dir ~/app-data/com.example/mmkv --id user_prefs dump --format json | jq .

# Decrypt using a key file (key not leaked to `ps`)
mmkvdump --dir ~/app-data/com.example/mmkv --id user_prefs \
  --crypt-key-file ~/.mmkv-keys/example.key dump

# Inspect raw bytes of a single key
mmkvdump --dir ~/app-data/com.example/mmkv --id user_prefs raw mystery_field
```

Run `mmkvdump --help` for the full list of options and a built-in examples block.

## How type inference works

MMKV does not store type metadata on disk, so `mmkvdump` has to guess. The
strategy, in order:

1. Try `getString` — if the result is non-empty, printable UTF-8, classify
   as `string` (or `json` when it parses as a JSON object/array).
2. If `getBytes` returned non-empty data, classify as `bytes` (these values
   were stored via `setBytes()`).
3. Probe numeric getters (`getLongInt` → `getInt` → `getFloat` → `getBool`)
   and return the first non-default value.
4. Fall back to an empty string.

When auto-inference picks the wrong type, use `get --type <name>` to force a
specific reader, or use `raw <key>` to see the value through every possible
getter at once.

## License

MIT — see [LICENSE](LICENSE).
