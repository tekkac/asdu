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

`asdu` shows how much disk space your Codex, Claude, and OMP sessions use.
Browse by tag, folder, source, or session type. Open a brief to review a
conversation. See how to resume it. Archive or remove supported sessions.

`asdu` reads local session files. It does not send their contents anywhere.

## Install

Install with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install 'git+https://github.com/tekkac/asdu.git'
asdu
```

Update an existing installation:

```sh
uv tool install --force 'git+https://github.com/tekkac/asdu.git'
```

## Use

Run `asdu` inside a project to see its sessions.

```sh
asdu
```

Show sessions stored under your home directory and use message text for tags:

```sh
asdu --project ~/ --content-keywords
```

`--project` limits results to sessions whose working directory is inside that
folder. `--content-keywords` also checks user messages when assigning tags.
The first scan can take longer.

Use the arrow keys to move. Press `Enter` to open and `Backspace` to return.

- `g` changes the grouping.
- `f` filters by source.
- `s` changes the sort order.
- `Ctrl-F` or `/` finds text. Use `n` and `N` for the next or previous match.
- `t` shows session trees. Use `Space` to fold a branch.
- `a` opens session actions.
- `r` rescans.
- `q` quits.

Press `?` for every key. Run `asdu --help` for command-line options.

## Session list

```text
 asdu  .                                                               size↓

     1.0 GiB  ████████████      8  /research
   512.0 MiB  ██████░░░░░░      4  /web-app

›   64.0 MiB  codex   main      1d ago  ▾ Fix one flaky test (+2)
    16.0 MiB  codex   child     1d ago    ├─ Find the race condition
     8.0 MiB  codex   child     1d ago    └─ Remove the lucky sleep
    18.2 MiB  claude  main      2h ago  Center the login form
     9.7 KiB  omp     main     29d ago  Check the build

 Transcripts: 1.6 GiB  16 sessions
 Enter open  g group  f filter  s sort  Ctrl-F find  t tree  a action  ? help
```

Sizes refer to saved conversations, not project files.

## Session brief

Press `Enter` on a session:

```text
 Center the login form

18.2 MiB  claude main  2h ago
Folder: /demo/web-app
24 messages across 384 events; 0 compactions.

╭ Latest request
│ Why did the login page disappear?
│
├ Last reply
│ The login page is back and the form is centered.
│
├ First request
│ Move the form two pixels to the left.
╰

ID: demo-css-001
Tags: development
Resume: claude --resume demo-css-001
```

The brief shows the folder, tags, session ID, recent messages, and a resume
command when available. It may also show the model or live-session commands.
`asdu` displays these commands but does not run them.

## Session actions

Browsing does not change any files. Select a session and press `a` to see its
available actions.

- Codex sessions can be archived, restored, or deleted through Codex.
- Claude sessions can be compressed into an archive or moved to Trash.
- `asdu` does not modify OMP sessions.

Deleting a Codex session is permanent and always requires confirmation. Close
a Claude session before archiving it or moving it to Trash.

## Contributing

Bug reports, UI improvements, and new session sources are welcome. Keep changes
small. Use fictional session data in tests.

```sh
uv run asdu.py
python3 -m unittest discover -s tests
```

New sources should start without file-changing actions. Add those actions only
after the source provides a safe way to perform them.

## License

[MIT](LICENSE)
