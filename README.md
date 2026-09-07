# asdu

```text
        █████╗ ███████╗██████╗ ██╗   ██╗
       ██╔══██╗██╔════╝██╔══██╗██║   ██║
       ███████║███████╗██║  ██║██║   ██║
       ██╔══██║╚════██║██║  ██║██║   ██║
       ██║  ██║███████║██████╔╝╚██████╔╝
       ╚═╝  ╚═╝╚══════╝╚═════╝  ╚═════╝

            agent session disk usage
```

`asdu` is a terminal browser for Codex, Claude, and OMP sessions, inspired by ncdu.
OMP support is read-only: browse, tags, trees, and conversation briefs.
Find large conversations, see what they were about, and archive or trash them.
Everything runs locally.

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

- `--project ~/` includes sessions under your home directory. Replace it with
  any project folder.
- `--content-keywords` also checks user messages for tag keywords.
  The initial scan takes longer and shows progress.

Re-run the install command with `--force` to update.

## Browse and review

Use the arrow keys to navigate, `Enter` to open, and `Backspace` to go back.

Tags are the opening screen. They use fixed keyword and folder rules, so adding
unrelated sessions won't change a conversation's tags. Unmatched sessions stay
untagged. The first matching tag determines its group; the brief shows all matches.
Use [custom rules](asdu.toml.example) with `--config` to replace the defaults.

- `g` groups by folder, tag, source, or session type; `f` filters by source.
- `s` sorts; `/` searches, with `n` and `N` for matches.
- `t` shows conversation trees; `Space` folds a branch.
- `r` rescans; `q` quits. `Ctrl-C` exits and clears the screen.

Use `?` for all keys and `asdu --help` for command-line options.
Sizes refer to saved conversations, not your project files.

Fictional sessions, shown without terminal colors:

```text
 asdu  .

     1.0 GiB  ████████████      8  /make-the-tests-green
   512.0 MiB  ██████░░░░░░      4  /one-small-css-change

›   64.0 MiB  codex   main      1d ago  ▾ Fix one flaky test (+2)
    16.0 MiB  codex   child     1d ago    ├─ Find the race condition
     8.0 MiB  codex   child     1d ago    └─ Remove the lucky sleep
    18.2 MiB  claude  main      2h ago  Center a div without changing physics
     9.7 KiB  omp     main     29d ago  hello?

 Transcripts: 1.6 GiB  16 sessions
 Enter open  g group  f filter  s sort  t tree  a action  ? help
```

Open a conversation with `Enter`:

```text
 Center a div without changing the laws of physics

18.2 MiB  claude main  2h ago
Folder: /demo/one-small-css-change
24 messages across 384 events; 0 compactions.

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

Briefs show recent excerpts first while full counts load in the background.
OMP briefs also identify the provider and latest model used. Resume commands are
shown when the source supports them.

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

Source readers live in `asdu_sources/`. Shared scanning and file actions live in
`asdu_sessions.py`, grouping and trees in `asdu_browser.py`, and the terminal UI
in `asdu.py`. Add readers to the explicit registry; new sources are read-only
unless their storage actions have been reviewed.

From a checkout, run `uv run asdu.py` to try the app. Run the tests with:

```sh
python3 -m unittest discover -s tests
```

Test a fresh wheel on Debian with Python 3.11:

```sh
docker build -f tests/Dockerfile -t asdu-smoke .
docker run --rm --network none asdu-smoke
```

This runs unit and PTY tests and checks installation, discovery, briefs, trees,
and archive/Trash recovery using disposable fixtures. No host folders are mounted.
The PTY tests cover startup, resize, Ctrl-C cleanup, and delayed terminal replies.
Touchpad scrolling and visual rendering still need a real-terminal check.

## License

[MIT](LICENSE)
