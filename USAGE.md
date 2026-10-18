# Using Kilnmark

Every command is one shot: it reads the state file, does its work, prints, and
exits. Nothing sleeps, nothing blocks, nothing needs a network.

Results go to **stdout**; errors and hints go to **stderr**. Exit codes are
`0` success, `2` invalid input, `3` unreadable state.

---

## 1. Firing schedules

### Add a schedule

```sh
python3 kilnmark.py schedule add bisque --start 20 \
    --segment rate=100,target=220,soak=10,cone=06 \
    --segment rate=200,target=620,soak=20,cone=6
```

```
saved schedule bisque with 2 segments
  total firing time: 4h 30m (270 min)
  next: kilnmark schedule show bisque
```

- `--start` is the temperature the kiln is at when the firing begins
  (default `20` C).
- `--segment` is repeatable and the order you type them in is the order they are
  stored and shown.
- `rate` is C/hr, `target` is the temperature the segment climbs to, `soak` is
  minutes held at the target, `cone` is an optional label.

The total is derived, never stored:

```
total_minutes = Σ over segments of ((target − previous_target) / rate) × 60 + soak
previous_target for segment 1 = --start
```

So the example is `(220−20)/100×60 + 10 = 130` plus `(620−220)/200×60 + 20 = 140`
— **270 minutes**.

### List every schedule

```sh
python3 kilnmark.py schedule list
```

```
schedules (2)

  name    segments  total firing time
  ──────  ────────  ──────────────────
  bisque         2  4h 30m (270 min)
  glaze          2  19h 10m (1150 min)

  active: glaze
```

### Show one schedule

```sh
python3 kilnmark.py schedule show bisque
python3 kilnmark.py schedule            # bare: shows the active schedule
```

The active schedule is the most recently saved one. `show` and the bare command
print the identical view.

### Invalid input

```sh
python3 kilnmark.py schedule show nope
# error: no schedule named nope
# hint: known schedules: bisque, glaze        (exit 2)

python3 kilnmark.py schedule add bad --segment rate=0,target=220,soak=10
# error: segment 1: rate must be greater than 0 (C/hr)   (exit 2)

python3 kilnmark.py schedule add bad --segment rate=fast,target=220,soak=10
# error: segment 1: rate must be a whole number (got 'fast')   (exit 2)

python3 kilnmark.py schedule add bad --start 20 \
    --segment rate=200,target=620,soak=20 \
    --segment rate=200,target=500,soak=10
# error: segment 2: target 500 C is not above the previous target 620 C   (exit 2)
```

A rejected schedule is never written: the state file still holds only the
schedules that were valid. Schedules are add-only — no editing or deleting after
save.

---

## 2. Cone deformation log

```sh
python3 kilnmark.py cone add --cone 6 --at 180 --note "tip curls"
```

```
logged cone 6 at 180 min (tip curls)
  cone 6: 1 entry, first deform 180 min
  next: kilnmark cone list
```

`--at` is whole minutes elapsed since the firing start; `--note` is optional
(120 characters max). Logging `cone 6` and `06` keeps them as two distinct
labels, because they are two different cones.

```sh
python3 kilnmark.py cone list
```

```
cone log (3 entries)

  elapsed  cone  note
  ───────  ────  ────────────────
  180 min  6     tip curls
  200 min  5     -
  240 min  6     second cone down

  per cone (earliest deform)
    cone 6: 2 entries, first deform 180 min
    cone 5: 1 entry, first deform 200 min
```

Entries print chronologically; the summary reports the **earliest** deform per
cone, soonest first.

### A damaged entry

Cone entries live one per line in `state.json`. If one is malformed (say its
`at_min` was removed by hand), it is named by its line number and skipped while
every readable entry still prints — exit code stays `0`:

```
skipping malformed cone entry at line 12
cone log (2 entries)
...
```

---

## 3. Stoke interval timer

### Start a cadence

```sh
python3 kilnmark.py timer run --interval-min 5 --repeat 3 --now 1700000000
```

```
stoke plan: every 5 min, 3 repeats
  stoke       at  clock
  ─────  ───────  ─────
  1       +5 min  05:00
  2      +10 min  10:00
  3      +15 min  15:00
started at 2023-11-14T22:13:20Z
next stoke in 05:00 (stoke 1 of 3)
```

`--now <epoch seconds>` is an injectable clock: give it and every countdown is
deterministic. Omit it to use the system clock. `--schedule <name>` records the
schedule this firing is running.

### Count down to the next stoke

```sh
python3 kilnmark.py timer --tick --now 1700000120
```

```
next stoke in 03:00 (stoke 1 of 3)
  started 2023-11-14T22:13:20Z · elapsed 2 min · interval 5 min
```

Once the repeats are spent:

```
all 3 stokes complete (interval 5 min)
```

### Read the stored defaults back

```sh
python3 kilnmark.py timer --last
python3 kilnmark.py timer             # bare: same view
```

```
last run: every 5 min, 3 repeats, started 2023-11-14T22:13:20Z
  last tick at 2023-11-14T22:15:20Z
  reuse: kilnmark timer run --interval-min 5 --repeat 3
```

With no prior run at all:

```
no timer run yet
  run one:    kilnmark timer run --interval-min 5 --repeat 3
  inspect:    kilnmark timer --last
  countdown:  kilnmark timer --tick --now 1700000120
  default interval: 10 min, 3 repeats
```

`timer run` with no flags reuses the stored interval and repeat, which is what
makes the previous run the next run's defaults.

### Rejected input

```sh
python3 kilnmark.py timer run --interval-min 0
# error: interval must be at least 1 minute          (exit 2, stored run untouched)
```

---

## 4. State file and recovery

```sh
python3 kilnmark.py --reset
# state reset: /home/you/.kilnmark/state.json
```

Recreates the seeded state (`version`, empty schedules, empty cone entries, no
timer) at that path and exits `0`. If the state file cannot be parsed:

```sh
python3 kilnmark.py schedule list
# error: state file could not be read (invalid JSON): /home/you/.kilnmark/state.json
# hint: run `kilnmark --reset` to recreate it                 (exit 3)
```

Every write goes to a temp file in the same directory and is `os.replace`d onto
`state.json`, so an interrupted write can never truncate the file: the previous
state stays readable.

---

## 5. Scripting

Add `--json` (before or after the subcommand) for stable machine-readable
output:

```sh
python3 kilnmark.py schedule list --json
python3 kilnmark.py schedule show bisque --json
python3 kilnmark.py cone list --json
python3 kilnmark.py timer --last --json
python3 kilnmark.py timer --tick --now 1700000120 --json
```

Colour is applied only to a TTY and is disabled by `--no-color` or `NO_COLOR`.
