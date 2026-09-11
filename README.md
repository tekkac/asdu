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

`asdu` shows where Codex, Claude, and OMP sessions use disk space.

It works like a file browser. Start in a folder, inspect its sessions, and move
up or down the directory tree. Everything stays local.

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

Run `asdu` inside any project:

```sh
asdu
```

Start somewhere else:

```sh
asdu --project ~/
```

The initial view shows folders. Press `g` for one flat list, then sources
of sessions in the current folder. Press `t` to show session trees; they open
folded.

```text
 asdu  ~/Code/demo                                                         143.9 MiB  12 sessions  size↓
    74.2 MiB  ████████████      5  /api
    38.1 MiB  ██████▏░░░░░      3  /docs

›   23.5 MiB  ████████████  codex   main    now       ▸ Fix flaky tests (+2)
     8.1 MiB  ████▏░░░░░░░  claude  main    12m ago   Review the release notes

 codex 019bcb82-bef5-7503 | 2026-09-09 11:52 | ~/.codex/sessions/…jsonl
 Enter open  Backspace back  a action  t tree  / find  g group  f source  F type  s sort   ? help  q quit
```

- Arrow keys select rows. In trees, `↑↓` select siblings and `←→` move between
  parents and children.
- `Enter` opens a folder or session brief.
- `Backspace` goes back or moves to the parent folder.
- `/` or `Ctrl-F` searches every scanned session by title, folder, or ID.
- `g` switches between folder, source, and all-session views.
- `f` filters by source.
- `F` filters by session type: main, child, review, or `?` when unknown.
- `s` changes the sort order.
- `t` toggles trees. `Space` folds one branch and `z` folds or opens all.
- `a` shows the actions supported by the selected source.
- `r` rescans.
- `q` quits.

Press `?` for help. Run `asdu --help` for command-line options.

## Session briefs

Press `Enter` on a session to see its folder, size, ID, activity, recent request,
last reply, and resume command.

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
Resume: claude --resume demo-css-001
```

## Session actions

Browsing never changes a session. Press `a` to review an action before it runs.

Codex archive and restore use Codex itself. Codex delete is permanent. Claude
sessions can move to the system Trash. Tree actions include folded descendants.
Active Claude sessions are refused.

Successful actions append a content-free record to
`$XDG_STATE_HOME/asdu/actions.jsonl`, normally
`~/.local/state/asdu/actions.jsonl`.

## Contributing

Bug reports, UI improvements, and new session sources are welcome. Keep changes
small and use fictional session data in tests.

```sh
uv run pytest
uv run ruff check .
```

New sources start read-only. Add file-changing actions only when their lifecycle
is understood and tested.

## License

[MIT](LICENSE)
