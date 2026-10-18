# Secrets: the team key vault

Picky's API keys (Claude, Gemini, Perceptron, Stripe test, Photon, ...) live in a small **key vault** (`utils/secrets.py`)
instead of being pasted into `.env` files, chat or PRs.

- **Each machine** keeps its keys in the OS keychain (the macOS Keychain on our Macs). On headless Linux (the GPU host
  "vhagar") it uses a private file, `~/.config/picky/secrets.env` (mode 600, dir 700), automatically.
- **The team** shares one encrypted bundle, `secrets/team.age`, committed to the repo. It is encrypted with
  [age](https://age-encryption.org) to every public key listed in `secrets/recipients.txt`, so only teammates (and
  machines) on that list can decrypt it. A leaked or public repo leaks nothing usable.
- **The code** doesn't change: `utils.config.settings.anthropic_api_key` (and the other fields) resolve from the vault.

## Quick start (about 2 minutes per person)

**Lead, once: store the keys and start the bundle.**

```bash
make secret NAME=PERCEPTRON_ROBOTICS_API_KEY   # paste at the hidden prompt, Enter (repeat for each key you hold)
make secrets                                   # SET=yes  SOURCE=keychain  VALUE=••••last4
make secrets-identity                          # prints your age1... public key
#   add it to secrets/recipients.txt as:  age1...  # lead, macbook
make secrets-push                              # writes secrets/team.age (lists who can read it)
git switch -c chore/secrets-bundle && git add secrets/ && git commit -m "chore: team secrets bundle" \
  && git push -u origin HEAD && gh pr create --fill
```

**Teammate (or vhagar), once.**

```bash
git pull
make secrets-identity     # prints your age1... public key; send it to the lead (public keys are safe in chat)
```

The lead adds your line to `secrets/recipients.txt`, runs `make secrets-pull && make secrets-push`, and pushes the
change to a `chore/secrets-<you>` branch. You don't have to wait for the PR to merge:

```bash
git fetch origin && git show origin/chore/secrets-<you>:secrets/team.age | make secrets-pull ARGS="--bundle -"
make secrets              # every key: SET=yes, SOURCE=keychain (vhagar: file)
```

After the merge, `git pull && make secrets-pull` is all you need.

## Adding the Perceptron key (lead)

The Perceptron **"Isaac SO100 participant API"** (`https://robotics.perceptron.inc`, OpenAPI docs at
`https://robotics.perceptron.inc/docs`) takes **one team-scoped bearer key** that Perceptron's operators hand out at the
event (their sponsor booth or channel). Only the lead requests it. In the vault it is `PERCEPTRON_ROBOTICS_API_KEY`.
The hosted vision key for the verifier (`api.perceptron.inc`) is a different name: `PERCEPTRON_API_KEY`.

1. Store it on your Mac: `make secret NAME=PERCEPTRON_ROBOTICS_API_KEY`. Paste the key at the hidden prompt and press
   Enter. Nothing is echoed, and nothing lands in your shell history.
2. Check it: `make secrets` shows `PERCEPTRON_ROBOTICS_API_KEY  yes  keychain  ••••xxxx` (the last 4 characters only).
3. Verify it against the API without ever showing it. The key travels through a pipe, not the screen or `argv`. That
   matters on the shared vhagar, where other users can read `ps`. The `( ... )` subshell keeps the variable out of
   your shell afterwards.

   ```bash
   ( eval "$(uv run python -m utils.secrets env PERCEPTRON_ROBOTICS_API_KEY)"
     : "${PERCEPTRON_ROBOTICS_API_KEY:?not set: make secret NAME=PERCEPTRON_ROBOTICS_API_KEY}"
     printf 'Authorization: Bearer %s\n' "$PERCEPTRON_ROBOTICS_API_KEY" |
       curl -sS -H @- -w '\nHTTP %{http_code}\n' https://robotics.perceptron.inc/v1/capabilities )
   ```

   `HTTP 200` plus JSON (qualified modes, quotas, budgets) means the key works. `HTTP 401
   {"error":{"code":"unauthorized",...}}` means the key is wrong, expired or revoked: ask Perceptron. The
   `warning: printing secret values...` line on stderr comes from `env`. It is expected here because the output goes
   into `eval`, not the screen.

   To check the path the code uses (vault → `utils.config`), also no echo:

   ```bash
   uv run python -c "import httpx; from utils.config import settings as s; r = httpx.get('https://robotics.perceptron.inc/v1/capabilities', headers={'Authorization': 'Bearer ' + s.perceptron_robotics_api_key}, timeout=15); print(r.status_code, r.text[:300])"
   ```
4. Share it: `make secrets-push` (Quick start above), then commit `secrets/team.age` (and `secrets/recipients.txt`) in
   a PR. Teammates and vhagar run `make secrets-pull`.
5. In code: `from utils.config import settings` → `settings.perceptron_robotics_api_key`, sent as
   `Authorization: Bearer <key>`. Tools that only read env vars: `eval "$(uv run python -m utils.secrets env
   PERCEPTRON_ROBOTICS_API_KEY)"` first.

Need it on vhagar before the bundle is set up? Send it over SSH, not chat. The value goes through stdin and never
reaches the screen, `argv` or shell history:
`pbpaste | ssh vhagar 'cd ~/picky && uv run python -m utils.secrets set PERCEPTRON_ROBOTICS_API_KEY --from-stdin'`.

## Resolution order

For every known name, the first hit wins:

```
process env  >  OS keychain (service "picky")  >  ~/.config/picky/secrets.env  >  repo .env (legacy)
```

- Env first, so `ANTHROPIC_API_KEY=... make demo` still overrides everything, and CI keeps working.
- The vault beats `.env`, so the blank `KEY=` lines from `cp .env.example .env` don't hide your real keys.
- An **exported empty** variable (`export ANTHROPIC_API_KEY=`) still wins and blanks the key (handy to force a fake for
  one run: `ANTHROPIC_API_KEY= make demo`). `make secrets` shows it as `blank` / `env`.
- The dev defaults `PICKY_SIM_TOKEN=dev-sim-token` / `PICKY_SIGNING_KEY=dev-signing-key` (from `.env.example`) show as
  `dev`, never count as set for `check --env prod`, and are never pushed to the team.
- `Settings(_env_file=None)` (hermetic tests) skips both `.env` and the vault. Pytest switches the vault off for the
  whole run (`conftest.py`), so tests never read your keychain.
- A broken vault (unreadable file, locked keychain) never stops the gateway. Those keys fall through to env / `.env`,
  and a warning names the key, never the value.
- Process-env knob (not read from `.env`): `PICKY_SECRETS_BACKEND=auto|keyring|file|off`. `auto` (default) = the
  keychain when a usable one exists and a desktop session is present, otherwise the file. `file` forces the file,
  `keyring` forces the keychain (Linux desktop), `off` disables the vault (env + `.env` only). A plaintext keyring
  (`keyrings.alt`) is never used, even if installed.

## Known secrets

The registry is fixed: the CLI refuses names that aren't on this list. To add one, change `utils/secrets.py`
`REGISTRY`, `Settings` and `.env.example` in one `utils/` PR. `make secrets ARGS=-v` prints the same table with
"where to get it".

| Name | What | Where to get it | `check` requires it for |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Claude: agent, robot brains, chat, vision `observe` | platform.claude.com → API keys | real `agent`, `perception` |
| `GEMINI_API_KEY` | Gemini planner (vla-loop) | aistudio.google.com/apikey | — |
| `PERCEPTRON_ROBOTICS_API_KEY` | Perceptron **Isaac SO100 participant API** team bearer key (robotics.perceptron.inc) | Perceptron at the event, one per team (the lead has it) | — |
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
| `make secret NAME=X` | `set X` (or `set X --from-stdin`) | store one key from a hidden prompt. Strips whitespace. Refuses unknown names, empty or multi-line values, live Stripe keys and a value typed on the command line. Warns on an unexpected prefix |
| `make secrets` | `list [-v]` | table: name, set?, source, `••••last4`, description, plus team bundle status |
| — | `delete X` | remove from the keychain and the file |
| — | `check [--arms a,b] [--env prod]` | exit 1 if a key a real arm in `PICKY_REAL_ARMS` needs is missing |
| — | `env [X ...]` | print `export X=...` lines for `eval`. The only command that prints values, and it refuses a terminal |
| `make secrets-identity` | `identity` | create/show your age key `~/.config/picky/age.key` (600) and print its public key |
| `make secrets-push` | `push [X ...] [--force] [--replace]` | encrypt to everyone in `secrets/recipients.txt` → `secrets/team.age` |
| `make secrets-pull` | `pull [--force] [--bundle FILE\|-]` | decrypt `secrets/team.age` (or another copy) into your local vault |

The bundle commands need the `secrets` extra (`pyrage`, the age bindings). The make targets add it for you with
`uv run --extra secrets`. Everything else runs on base deps. No CLI output, error or log line contains a value: a key
pasted where a name or argument goes is not echoed back.

## Per-machine notes

### Mac

Keys go to the login keychain under service `picky` (Keychain Access → search "picky"). If macOS asks whether
"python" may access the keychain, click **Always Allow**: it's the repo's Python reading its own item. Already had
keys in `.env`? They keep working at the lowest priority. Move each one with `make secret NAME=...`, then blank the
line in `.env`. Long keys (over ~1000 characters) can get cut off when pasted at a prompt on macOS. Pipe those in
instead: `pbpaste | uv run python -m utils.secrets set NAME --from-stdin`.

### vhagar (headless Linux GPU host, no sudo, shared)

There's nothing to install system-wide. With no desktop session, the vault uses `~/.config/picky/secrets.env`
automatically. Don't run `uv sync` or `make setup` just for this: they drop extras, and `uv run` installs what the
vault needs by itself.

```bash
cd ~/picky && git pull
make secrets-identity                          # vhagar gets its own age key; send the age1... line to the lead
make secrets-pull                              # after the lead's push -> ~/.config/picky/secrets.env (600)
make secrets                                   # SOURCE=file
```

The Isaac worker (`/data/$USER/isaacsim/python.sh -m arms.sim.app`) reads the same file through `utils.config`, even
without `keyring` in Isaac's Python. If an SSH session forwards a desktop (`DISPLAY` set) and a keyring prompt hangs,
force the file with `export PICKY_SECRETS_BACKEND=file` (e.g. in `~/.bashrc`). For a process that doesn't use
`utils.config` (a shell script, a CLI, an SDK that only reads env vars):

```bash
eval "$(uv run python -m utils.secrets env PERCEPTRON_ROBOTICS_API_KEY HF_TOKEN)"
some-tool ...                                  # sees them as normal env vars
```

Never put a key in a command line on vhagar (`curl -H "Authorization: Bearer $KEY"`, `--api-key ...`). Other users
can see it in `ps`. Pipe it in, as the Perceptron check above does.

## How push and pull behave

- **push shares only what you stored on purpose:** your local vault (keychain / file), plus keys still sitting in the
  repo `.env` (labelled `(from .env)`). It never shares your **shell environment**, such as an `ANTHROPIC_API_KEY`
  exported in `~/.zshrc`; it lists those as "not shared". It also never shares dev defaults or invalid values like a
  live Stripe key.
- **push merges.** It decrypts the current bundle with your identity, adds keys you have that the bundle lacks, and
  keeps the rest. If one of your values differs from the bundle's, it **refuses**, so a stale laptop can't revert a
  rotation. To fix that, run `make secrets-pull ARGS=--force` (take the team's). To overwrite one key on purpose, run
  `make secrets-push NAME=X`; `ARGS=--force` overwrites all of them with yours. `--replace` rebuilds the bundle from
  only your keys and drops everyone else's. Use it only as a last resort, when nobody can decrypt the old bundle.
- **push shows who can read it:** every recipient's label from `recipients.txt` (`<- you` marks yours). Read that list
  before committing. Anyone who can merge a line into `recipients.txt` gets every key at the next push.
- **pull never clobbers.** Missing keys are stored. If your local value differs, pull keeps it and tells you;
  `make secrets-pull ARGS=--force` takes the team's. `--bundle FILE` (or `-` for stdin) decrypts a copy from a branch
  or AirDrop. That's safe, because the file is ciphertext.
- The bundle shows no plaintext names or values; GitHub only sees an `-----BEGIN AGE ENCRYPTED FILE-----` block.
  `.gitattributes` marks it binary, so git never writes conflict markers into it.
- **Two people pushed at once** (merge conflict on `secrets/team.age`):
  `git checkout --theirs secrets/team.age && make secrets-pull && make secrets-push && git add secrets/team.age`.
  This takes main's bundle, merges your keys back in, and re-encrypts.

## Rotation (after the hackathon, or when someone leaves)

age can't un-share what someone already decrypted. Old bundles also stay in git history, so anyone who was ever a
recipient can still read old values. Removing a recipient therefore means **rotating the keys**:

1. Remove their line from `secrets/recipients.txt`.
2. Revoke and regenerate each key at its provider (Anthropic, Gemini, Perceptron, Stripe test, Photon, Deepgram, HF).
   Generate new `PICKY_SIM_TOKEN` / `PICKY_SIGNING_KEY`.
3. Store each new value with `make secret NAME=...`, then run `make secrets-push ARGS=--force`. Any key you didn't
   rotate keeps its old, exposed value: rotate it too, or rebuild with `ARGS=--replace`. Commit in a PR, then everyone
   runs `make secrets-pull ARGS=--force`.

After DubHacks (the repo goes public before submission), rotate or revoke every hackathon key above. The ciphertext
can stay public. What protects it is the private age keys on our machines.

## Don't

- Don't commit `.env`, `~/.config/picky/*`, `age.key` or any decrypted bundle. `secrets/` ignores everything except
  `*.age` and `recipients.txt`.
- Don't paste keys into chat, PRs, issues, commit messages, screenshots or LLM prompts; share them through the bundle.
  Public `age1...` keys are fine to paste anywhere.
- Don't type a key as a command-line argument (`set NAME sk-...`). The CLI refuses it without echoing it, but your
  shell history already has it: delete that history line.
- Don't print keys in logs or the web app. `make secrets` shows `••••last4` only, and `repr(settings)` shows `••••`.
  `env` output is for `eval`, never for copy/paste.
- Don't use live payment keys: the vault refuses `sk_live_` / `rk_live_` for Stripe (test/sandbox only, CLAUDE.md §8).
- Don't put an `AGE-SECRET-KEY-...` anywhere except `~/.config/picky/age.key`. `push` refuses a `recipients.txt` that
  contains one.
- The web app's own keys (`arms/frontend/.env.local`) are outside this vault for now.

## Troubleshooting

- `make secrets` says `file ... (no usable OS keychain here)` on a Mac: the keychain backend failed to load. Check it
  with `uv run python -c "import keyring; print(keyring.get_keyring())"`.
- "keychain write failed ... using ~/.config/picky/secrets.env": the keychain is locked (e.g. over SSH), so the key
  went to the file instead. If the keychain already held an older value, that old value wins again once the keychain
  is unlocked: log in on the Mac and `make secret` again.
- `pull`: "isn't a recipient yet": the bundle wasn't encrypted to your key yet. Send your `age1...` to the lead.
- `pull`: "not a valid age bundle" or "merge-conflict markers": see "Two people pushed at once" above.
- `push` refused because values differ: run `make secrets-pull ARGS=--force` (or `make secrets-push NAME=X` if yours
  is the new one).
- `make secrets` lists a key as `blank` / `env`: an exported empty variable is hiding the vault. Run `unset NAME`.
