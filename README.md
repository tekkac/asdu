# asdu

`asdu` is a terminal browser for Codex and Claude sessions, inspired by ncdu.
Find large conversations, see what they were about, and archive or trash them.
Everything runs locally.

## A look inside

```text
        █████╗ ███████╗██████╗ ██╗   ██╗
       ██╔══██╗██╔════╝██╔══██╗██║   ██║
       ███████║███████╗██║  ██║██║   ██║
       ██╔══██║╚════██║██║  ██║██║   ██║
       ██║  ██║███████║██████╔╝╚██████╔╝
       ╚═╝  ╚═╝╚══════╝╚═════╝  ╚═════╝

            agent session disk usage

╭──────────────────────────────────────────────╮
│ Indexing Codex                               │
│ ███████████████████░░░░░░░░░░░░░░░░░░░   50% │
│ 342 sessions                         1.2 GiB │
╰──────────────────────────────────────────────╯
```

Preview with fictional sessions:

```text
 asdu  demo                                                  size↓

›    1.0 GiB  ████████████     42  /make-the-tests-green
   512.0 MiB  ██████░░░░░░     17  /one-small-css-change
   256.0 MiB  ███░░░░░░░░░      8  /rewrite-it-in-rust
   128.0 MiB  █▌░░░░░░░░░░      5  /why-is-it-dns
    64.0 MiB  ▊░░░░░░░░░░░      3  /final-final-v2

Transcripts: 1.9 GiB  75 sessions
 Enter open  g group  f filter  s sort  ? help
```

Open a conversation with `Enter`:

```text
 Center a div without changing the laws of physics

18.2 MiB  claude main  2h ago
Folder: /demo/one-small-css-change
12 turns across 384 events; 2 compactions.

╭ Latest request
│ Why did the login page disappear?
│
├ Last reply
│ The button is centered. I have restored the login page
│ and removed the unnecessary Kubernetes deployment.
│
├ First request
│ Move the button two pixels to the left.
╰

ID: demo-css-001
Tags: development
Resume: claude --resume demo-css-001
```

## Install

Requires [uv](https://docs.astral.sh/uv/), Python 3.11+, and a POSIX terminal
(Linux or macOS). This private repository requires GitHub SSH access.

```sh
uv tool install 'git+ssh://git@github.com/tekkac/asdu.git'
asdu
```

Run `asdu` from a project folder to browse its sessions. To browse across
your home directory and use conversation text for tagging:

```sh
asdu --project ~/ --content-keywords
```

- `--project ~/` includes sessions associated with folders under your home
  directory. Replace `~/` with any project folder.
- `--content-keywords` also checks user messages for tag keywords.
  The initial scan takes longer and shows progress.

Use `asdu --help` (or `-h`) for all options, and `?` inside the app for keys.
Re-run the install command with `--force` to update. Check your version with
`asdu --version`.

## Browse and review

Use the arrow keys to navigate, `Enter` to open, and `Backspace` to go back.
The `›` marker and highlighted row show your selection.

- Sort by size, name, or when the session file was updated with `s`.
- Explore folders, tags, or session types with `g` to group.
- Show only Codex or Claude with `f` to filter.
- Search titles or the open brief with `/`; use `n` and `N` for matches.
- Open a session brief with `Enter` to read request/reply excerpts and
  find a command you can copy to resume the conversation.
- Explore related conversations with `t` for tree view. Use `Space` to
  fold a branch and `z` to expand or collapse all branches.

Tree rows include their descendants' sizes; `(+N)` counts those descendants.
The footer counts each session once. Relationships come from recorded metadata,
so children without a known parent can appear on their own.

Briefs show recent excerpts first, wrapped at 120 columns. Previews are labeled
until the full scan finishes; exact activity counts then appear automatically.
You can scroll while loading. `Enter` or `Backspace` returns to the list and
cancels any remaining scan.

Background stripes adapt to the terminal theme at startup. If detection is
unavailable, use `--theme dark` or `--theme light`; `--ascii` uses plain symbols.

Press `r` to rescan, `?` for help, or `q` to quit. `Ctrl-C` exits and clears the screen.
Sizes refer to saved conversations, not your project files.
The footer reports skipped files and invalid records encountered during discovery.
This is a scan warning, not a full transcript integrity check.

## Clean up

Browsing changes nothing. Select a session and press `a` to choose:

- **Archive** replaces the saved conversation with a compressed copy.
  Archives are saved in `~/.local/share/asdu/archive/`
  (or your XDG data directory). Decompress and restore the file to its original
  location before resuming it; asdu has no archive browser.
- **Trash** moves it to your system Trash, where you can restore it.

asdu keeps a local log of these actions.
Close sessions in their original app before archiving or trashing them.

## Contributing

Bug reports, UI improvements, and support for other agents are welcome.
Keep changes small and use fictional sessions in tests.
Keep normal browsing cache-free; justify additional caching with measurements.

From a checkout, run `uv run asdu.py` to try the app. Run the tests with:

```sh
python3 -m unittest discover -s tests
```

Test a fresh wheel on Debian with Python 3.11:

```sh
docker build -f tests/Dockerfile -t asdu-smoke .
docker run --rm --network none asdu-smoke
```

This runs the unit tests and checks installation, discovery, briefs, trees,
and archive/Trash recovery using disposable fixtures. No host folders are mounted.
Touchpad scrolling and visual rendering still need a real-terminal check.

## License

[MIT](LICENSE)
