# Secrets: the team key vault

Picky's API keys (Claude, Gemini, Perceptron, Stripe test, Photon, ...) live in a small **key vault** (`utils/secrets.py`)
instead of being pasted into `.env` files, chat or PRs.

- **Each machine** keeps its keys in the OS keychain (the macOS Keychain on our Macs). On headless Linux (the GPU host
  "vhagar") it uses a private file, `~/.config/picky/secrets.env` (mode 600, dir 700), automatically.
- **The team** shares one encrypted bundle, `secrets/team.age`, committed to the repo. It is encrypted with
  [age](https://age-encryption.org) to every public key listed in `secrets/recipients.txt`, so only teammates (and
  machines) on that list can decrypt it. A leaked repo leaks nothing usable.
- **The code** doesn't change: `utils.config.settings.anthropic_api_key` (and the other fields) resolve from the vault.

TL;DR, the Perceptron team key:

```bash
make secret NAME=PERCEPTRON_ROBOTICS_API_KEY     # paste at the hidden prompt, press Enter
make secrets                                     # check: SET=yes, SOURCE=keychain, VALUE=••••last4
```

## Resolution order

For every known name, the first hit wins:

```
process env  >  OS keychain (service "picky")  >  ~/.config/picky/secrets.env  >  repo .env (legacy)
```

- Env first, so `ANTHROPIC_API_KEY=... make demo` still overrides everything (and CI keeps working).
- The vault beats `.env`, so the blank `KEY=` lines from `cp .env.example .env` don't hide your real keys.
- An **exported empty** variable (`export ANTHROPIC_API_KEY=`) still wins and blanks the key; `make secrets` shows it
  as `blank` / `env`.
- `Settings(_env_file=None)` (hermetic tests) skips both `.env` and the vault. Pytest switches the vault off for the
  whole run (`conftest.py`), so tests never read your keychain.
- Process-env knob (not read from `.env`): `PICKY_SECRETS_BACKEND=auto|keyring|file|off`. `auto` (default) = the
  keychain when a usable one exists and a desktop session is present, otherwise the file. `file` forces the file,
  `keyring` forces the keychain (Linux desktop), `off` disables the vault (env + `.env` only).

## Known secrets

Fixed registry: the CLI refuses names that aren't on this list (add new ones in `utils/secrets.py` `REGISTRY` +
`Settings` + `.env.example` in one `utils/` PR). `make secrets ARGS=-v` prints the same with "where to get it".

| Name | What | Where to get it | `check` requires it for |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Claude: agent, robot brains, chat, vision `observe` | platform.claude.com → API keys | real `agent`, `perception` |
| `GEMINI_API_KEY` | Gemini planner (vla-loop) | aistudio.google.com/apikey | — |
| `PERCEPTRON_ROBOTICS_API_KEY` | Perceptron **Isaac SO100 participant API** team key (robotics.perceptron.inc) | Perceptron, one per team (the lead has it) | — |
| `PERCEPTRON_API_KEY` | Perceptron hosted vision (verifier grounding / OCR), optional | api.perceptron.inc | — |
| `STRIPE_SECRET_KEY` | Stripe **test** key `sk_test_...` (live keys are refused) | dashboard.stripe.com/test/apikeys | real `trust` |
| `VISA_API_KEY`, `VISA_SHARED_SECRET` | Visa sandbox, optional | developer.visa.com | — |
| `PHOTON_API_KEY` | Photon 2.0 messaging | app.beta.photon.codes → project → API key | real `messaging` |
| `DEEPGRAM_API_KEY` | speech-to-text, optional | console.deepgram.com | — |
| `PICKY_SIM_TOKEN` | token the Isaac worker sends on `/ws/sim` (same value on gateway and vhagar) | generate one | when `PICKY_ENV` isn't dev |
| `PICKY_SIGNING_KEY` | HMAC key for mandates / receipts | generate one | when `PICKY_ENV` isn't dev |
| `HF_TOKEN` | Hugging Face, gated model downloads | huggingface.co/settings/tokens | — |

Non-secrets (model ids, URLs, `PHOTON_PROJECT_ID`, ...) stay in `.env`.

## Commands

| Make | CLI (`uv run python -m utils.secrets ...`) | Does |
|---|---|---|
| `make secret NAME=X` | `set X` (or `set X --from-stdin`) | store one key; hidden prompt; strips whitespace; refuses unknown names, empty or multi-line values and live Stripe keys; warns on an unexpected prefix |
| `make secrets` | `list [-v]` | table: name, set?, source, `••••last4`, description (+ team bundle status) |
| — | `delete X` | remove from the keychain and the file |
| — | `check [--arms a,b] [--env prod]` | exit 1 if a key a real arm in `PICKY_REAL_ARMS` needs is missing |
| — | `env [X ...]` | print `export X=...` lines for `eval` (the only command that prints values; refuses a terminal) |
| `make secrets-identity` | `identity` | create/show your age key `~/.config/picky/age.key` (600) and print its public key |
| `make secrets-push` | `push [X ...] [--force] [--replace]` | encrypt to everyone in `secrets/recipients.txt` → `secrets/team.age` |
| `make secrets-pull` | `pull [--force]` | decrypt `secrets/team.age` into your local vault |

The bundle commands need the `secrets` extra (`pyrage`, the age bindings); the make targets add it for you with
`uv run --extra secrets`. Everything else runs on base deps.

## Per-machine setup

### Mac (teammates)

```bash
git pull && make setup
make secret NAME=ANTHROPIC_API_KEY        # or: pbpaste | uv run python -m utils.secrets set ANTHROPIC_API_KEY --from-stdin
make secrets                              # SOURCE=keychain
```

Keys go to the login keychain under service `picky` (Keychain Access → search "picky"). If macOS asks whether
"python" may access the keychain, click **Always Allow** (it's the repo's Python reading its own item). Already had
keys in `.env`? They keep working (lowest priority); move each one with `make secret NAME=...` and then blank the line
in `.env`.

### vhagar (headless Linux GPU host, no sudo)

Nothing to install system-wide: with no desktop session the vault uses `~/.config/picky/secrets.env` automatically.

```bash
cd ~/picky && git pull && uv sync              # keyring is a base dep; it just finds no backend here
make secret NAME=PERCEPTRON_ROBOTICS_API_KEY   # -> ~/.config/picky/secrets.env (600)
make secrets                                   # SOURCE=file
```

The Isaac worker (`/data/$USER/isaacsim/python.sh -m arms.sim.app`) reads the same file through `utils.config`, even
without `keyring` in Isaac's Python. If an SSH session forwards a desktop (`DISPLAY` set) and a keyring prompt hangs,
force the file: `export PICKY_SECRETS_BACKEND=file` (e.g. in `~/.bashrc`). For a process that doesn't use
`utils.config` (a shell script, a CLI, an SDK that only reads env vars):

```bash
eval "$(uv run python -m utils.secrets env PERCEPTRON_ROBOTICS_API_KEY HF_TOKEN)"
some-tool ...                                  # sees them as normal env vars
```

## Team bundle (share keys once, not in chat)

One-time, per person **and per machine** (vhagar gets its own key):

```bash
make secrets-identity          # creates ~/.config/picky/age.key, prints "age1..."
```

Add the printed public key to `secrets/recipients.txt` (`age1...  # alice, macbook`), commit and push it in a PR. Public
keys are safe to commit. Then someone who can already decrypt (the lead, at first) re-encrypts for the new list:

```bash
git pull && make secrets-pull  # take the team's latest first
make secrets-push              # re-encrypts to every recipient; commit secrets/team.age
```

and the newcomer runs `git pull && make secrets-pull`. Done: their keychain (or vhagar's file) now has every key.

How push/pull behave:

- **push merges.** It decrypts the current bundle with your identity, adds keys you have locally that the bundle
  lacks, and keeps the rest. If your local value differs from the bundle's it **refuses** (so a stale laptop can't
  revert a rotation): run `make secrets-pull` first, or `make secrets-push NAME=X` to overwrite just that key on
  purpose (rotation), or `ARGS=--force` to overwrite all with yours. `--replace` rebuilds the bundle from only your
  local keys (when you can't decrypt the old one).
- **pull never clobbers.** Missing keys are stored; a different local value is kept and reported; `make secrets-pull
  ARGS=--force` takes the team's.
- `push` reads keys from the full resolution order (env, keychain, file, `.env`), so keys still sitting in an old
  `.env` get shared too; it prints only names. Every push writes a new ciphertext: commit `secrets/team.age`.
- The bundle has no plaintext names or values; GitHub only sees an `-----BEGIN AGE ENCRYPTED FILE-----` block.

## Adding the Perceptron key (lead)

The Perceptron "Isaac SO100 participant API" key for `https://robotics.perceptron.inc` is one key per team; keep it in
the vault as `PERCEPTRON_ROBOTICS_API_KEY`. (The hosted vision key for the verifier is a different name,
`PERCEPTRON_API_KEY`.)

1. On your Mac: `make secret NAME=PERCEPTRON_ROBOTICS_API_KEY`, paste the key at the hidden prompt, Enter.
2. Check: `make secrets` shows `PERCEPTRON_ROBOTICS_API_KEY  yes  keychain  ••••xxxx`.
3. Share it: `make secrets-identity`, put your `age1...` line (and teammates' / vhagar's lines once they send them) in
   `secrets/recipients.txt`, then `make secrets-push` and commit `secrets/recipients.txt` + `secrets/team.age`.
4. Teammates and vhagar: `git pull && make secrets-pull`.
5. Code reads it as `settings.perceptron_robotics_api_key` (`from utils.config import settings`); non-Python tools via
   `eval "$(uv run python -m utils.secrets env PERCEPTRON_ROBOTICS_API_KEY)"`.

Can't wait for the bundle? Send it to vhagar without chat: `pbpaste | ssh vhagar 'cd ~/picky && uv run python -m
utils.secrets set PERCEPTRON_ROBOTICS_API_KEY --from-stdin'`.

## Rotation (after the hackathon, or when someone leaves)

age can't un-share what someone already decrypted, so removing a recipient means **rotating the keys**:

1. Remove their line from `secrets/recipients.txt`.
2. Revoke/regenerate each key at its provider (Anthropic, Gemini, Perceptron, Stripe test, Photon, Deepgram, HF) and
   generate new `PICKY_SIM_TOKEN` / `PICKY_SIGNING_KEY`.
3. `make secret NAME=...` for each new value, then `make secrets-push ARGS=--force`; commit; everyone `make secrets-pull
   ARGS=--force`.

After DubHacks (the repo goes public before submission): rotate every key above, or at least revoke the hackathon
keys. The bundle is safe to publish only while nobody outside the team holds an identity in `recipients.txt`.

## Don't

- Don't commit `.env`, `~/.config/picky/*`, `age.key` or any decrypted bundle (`secrets/` ignores everything but
  `*.age` and `recipients.txt`).
- Don't paste keys into chat, PRs, issues, commit messages, screenshots or LLM prompts; share them through the bundle.
- Don't print keys in logs or the web app. `make secrets` shows `••••last4` only; `env` is for `eval`, never for
  copy/paste.
- Don't use live payment keys: the vault refuses `sk_live_` / `rk_live_` for Stripe (test/sandbox only, CLAUDE.md §8).
- Don't put an `AGE-SECRET-KEY-...` anywhere but `~/.config/picky/age.key`.
- The web app's own keys (`arms/frontend/.env.local`) are outside this vault for now.

## Troubleshooting

- `make secrets` says `file ... (no usable OS keychain here)` on a Mac: the keychain backend failed to load; check
  `uv run python -c "import keyring; print(keyring.get_keyring())"`.
- "keychain write failed ... using ~/.config/picky/secrets.env": the keychain is locked (e.g. over SSH). The key was
  saved to the file instead; it still resolves.
- `pull`: "can't decrypt ... isn't a recipient yet": your public key isn't in the bundle yet; add it to
  `recipients.txt` and ask a teammate to `make secrets-push`.
- `push` refused, values differ: `make secrets-pull` first, then push again (or `make secrets-push NAME=X`).
