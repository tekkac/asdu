# asdu

`asdu` is an ncdu-style browser for local coding-agent session storage.
It answers a practical question: which projects, topics, and conversations are
using disk before you archive or remove them.

It reads Codex and Claude sessions locally. It makes no network or model calls.
Other local agent formats are welcome as small, self-contained source adapters.

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

For testing from the private repository, first authenticate GitHub over SSH:

```sh
uv tool install 'git+ssh://git@github.com/tekkac/asdu.git'
asdu --version
asdu
```

Update with `uv tool upgrade asdu`. If the command is not on your PATH, run
`uv tool update-shell` and open a new terminal. Remove it with `uv tool uninstall asdu`.

Alternatively, clone and install locally:

```sh
gh repo clone tekkac/asdu
cd asdu
uv tool install .
```

You can also run the script directly from a checkout:

```sh
# Browse sessions associated with the current directory.
uv run asdu.py

# Browse a project or every known project.
uv run asdu.py --project ~/Code/example
uv run asdu.py --all

# Search user-authored session text for configured keywords.
uv run asdu.py --content-keywords
```

Use the arrow keys to navigate, `Enter` to open, `Backspace` to return, `r` to
rescan, `?` for help, and `q` to quit. In a session group: `t` shows the native Codex
conversation tree and `a` opens Archive/Trash actions.

Use `/text` to find a title in the current list or text in a brief. `n` and `N`
move to the next and previous match, wrapping at the ends. Home/End jump to
the beginning/end. Search reads only the titles or brief already displayed.
Action and rescan errors appear in the bottom status line.

## What it shows

- Transcript file sizes, relative update time, source, and session provenance.
- CWD, tag, source, and origin groupings.
- Native Codex parent/child trees. Cross-tag parents appear as structural
  context. Claude sidechains are labelled `side`: Claude records do not expose
  a parent session ID for them.
- A local session brief with first/latest request, last reply, activity, and
  native provenance, plus a copyable native resume command for Codex or Claude.

The built-in starter tags are `development`, `research`, `operations`,
`security`, `tooling`, `data`, and `documentation`. Sessions that do not fit
remain `untagged`; recurring local title/path patterns can supply a small set
of inferred tags.

Sizes count transcript bytes associated with each working directory, not the
project's files or filesystem allocated blocks. Tag groups use the first
matching (primary) tag; the brief lists all matching tags. Each transcript
appears in one tag group, and `all sessions` is a separate view of the total.

## Commands

```sh
# Scriptable overview or list.
uv run asdu.py summary --group source
uv run asdu.py sessions --tag research

# One structural brief; use an ID shown by the browser.
uv run asdu.py digest --session 019f9a3a

# Select one or more sources.
uv run asdu.py --source codex --source claude --all

# Replace starter tag rules with durable local rules.
uv run asdu.py --config asdu.toml.example
```

`--content-keywords` scans only recognized user-message text. Its cache lives
under `$XDG_CACHE_HOME/asdu/` or `~/.cache/asdu/`; changing tag keywords causes
one cache rebuild. Normal browsing and briefs do not cache transcript data.

## Safety and storage

Browsing is read-only. `a` always opens an action chooser:

- **Archive** writes a gzip copy under `$XDG_DATA_HOME/asdu/archive/` (or
  `~/.local/share/asdu/archive/`) before removing the live transcript.
  Each archive has a unique filename; the action log records its destination.
- **Trash** uses the operating system’s recoverable Trash and checks again
  that the transcript has not changed since scanning.

Successful Archive and Trash actions append a content-free JSONL record to
`$XDG_STATE_HOME/asdu/actions.jsonl` (or `~/.local/state/asdu/actions.jsonl`).

asdu needs no account, API key, or hosted service. Its only runtime dependency
is `send2trash`, installed automatically by uv for recoverable Trash support.

## Supported sources

| Source | Default local root | Notes |
| --- | --- | --- |
| Codex | `~/.codex/sessions` | Native parent-thread trees |
| Claude | `~/.claude/projects` | Sidechains, no parent session link |

Pass `--root` or `--claude-root` to use another location. The curses interface
is intended for POSIX terminals. Additional source support is welcome; a source
adapter only needs a local reader and synthetic fixtures.

## Contributing

Keep changes small, local, and dependency-free where possible. Add a synthetic
fixture for each new or changed source-record shape—never a real transcript.

```sh
python3 -m unittest tests/test_asdu.py
```

The implementation deliberately keeps one runnable script: source inspection,
shared scanning, classification, storage actions, UI, and CLI are separated by
small functions rather than a framework or service layer.

## License

[MIT](LICENSE)
