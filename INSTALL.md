# Installing Kilnmark

Kilnmark is a single-file Python CLI with no runtime dependencies. "Install"
here means: make sure Python is present, then run it from the checkout.

## 1. Prerequisites (installed by you, never by this project)

- **Python 3.9 or newer.** Check with `python3 --version`.
- Nothing else. There is no compiler step, no database, no service, no network
  access and no system package to add.

## 2. Test dependency (the only project-level dependency)

`requirements.txt` pins the test runner. Install it with the stack's own tool:

```sh
bash install.sh
```

`install.sh`

1. `cd`s to this directory,
2. installs `requirements.txt` with `python3 -m pip` (upgrading `pip`,
   `setuptools` and `wheel` first) — or skips that step when pytest is already
   importable, so it also works on a machine with no index access,
3. verifies `import kilnmark` and `python3 kilnmark.py --version`,
4. **exits** — it never starts a long-running process.

It is idempotent: running it twice is safe.

Manual equivalent:

```sh
python3 -m pip install --upgrade pip setuptools wheel
python3 -m pip install -r requirements.txt
```

## 3. Seed the state file

There is no separate seed step. The first command that touches the store creates
`~/.kilnmark/state.json` (mode `0600`) with the seeded empty state:

```sh
python3 kilnmark.py schedule list
# schedules (0)
#
# no schedules saved yet
#   start one:
#     kilnmark schedule add bisque --start 20 \
#       --segment rate=100,target=220,soak=10,cone=06
```

Want sample data instead of an empty file? `sh demo.sh` fills a scratch state
directory with real schedules, cone entries and a stored timer run, prints the
whole tour, then deletes the scratch directory.

## 4. Run it

```sh
python3 kilnmark.py --help          # command surface and examples
sh demo.sh                          # recorded end-to-end demonstration
```

Kilnmark is not a server: every invocation does its work and exits. There is no
port, no bind address and no daemon.

## 5. Verify the installation

```sh
python3 kilnmark.py --version
python3 -m pytest                   # 26 tests, no network needed
```

## Where data lives

| Path | Contents |
| ---- | -------- |
| `~/.kilnmark/state.json` | schedules, cone entries, timer run (mode `0600`) |
| `$KILNMARK_HOME/state.json` | the same document when `KILNMARK_HOME` is set |

Uninstall by deleting `~/.kilnmark` and the checkout.
