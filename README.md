# CairnKeeper

Keeps [Phantom Abyss](https://store.steampowered.com/app/989440/) playable after
its servers shut down.

*A cairn is a stack of stones left by people who walked a path before you, so
that whoever comes next can find it. That is what a phantom is, and this keeps
them.*

Phantom Abyss is an asynchronous multiplayer game that puts players into
procedurally-generated temples and tasks them with retrieving relics, unlocking
whips, and completing challenges.

## The problem

This game is online-only, and is no longer being updated. Its time is limited,
and when the servers go down, the game will stop working. This is not something
that I think is fair to the players who bought the game, and this project aims
to resolve that issue.

## Purpose of the project

My goal is to provide players with the ability to continue playing the game
they paid for by having the game look at a local server instead of a remote
one, which may stop working at any time. This was accomplished by listening to
packets sent and received while the game was running and reverse engineering
the protocol. The result is a server that can be run locally from a simple
client, or even hosted in a Docker container for multiple friends to play and
connect to.

With the application, you will be able to listen and capture temples from the
still active servers, capture phantoms of players you see around you, and
export your archive to a central server for others to continue enjoying.

## What cannot be replaced

The generator that builds temples lives in the game itself, so temples can go on
being made forever. Phantoms cannot. Every one is a recording of a real person's
run, and **a phantom that nobody saved is gone** — there is no way to regenerate
somebody else's run.

That is the whole of the deadline. Everything else can wait.

---

## Do this before the servers close

Run the app in **Listen** mode and play normally, at least once, ideally more.

Listen puts a server between your game and `wiby.net`. Everything still works
exactly as it always has — you are playing online, on the real service — but
every temple you enter, every phantom you run against, and **your own account's
progression** are written to disk as you go.

That last part matters more than people expect. Your whips, upgrades, relics and
reputation live on the service, not in your save file. Play one session in
Listen mode and your progression is captured; skip it, and when the servers go
your offline account starts from nothing.

One session takes minutes. It is the single most useful thing you can do.

---

## After the servers close: how to help

Nothing stops when the service does.

**Keep playing, and keep your recordings.** Every run you make offline is stored
as a phantom, so a temple you play stays populated for whoever you share it
with. The archive only grows.

**Pool what you have.** No single player will ever have run enough temples to
matter on their own. A few hundred players between them might, and each of them
gathered theirs simply by playing. The app makes a folder you can hand to
somebody — zip it, put it anywhere, no server and no accounts needed — and takes
one in the same way.

**Report temples that cannot be finished.** The generator sometimes builds a
temple nobody can beat: a door whose key never spawned, an exit sealed off. Once
the servers are gone there is nobody left to fix the generator, so the app routes
around them instead — but only if players say which ones.

**Host for your friends.** A server is a Docker container and an archive
directory. Anybody who can run a container can run one.

---

## Getting it

Download `CairnKeeper.exe` from the releases page. There is no installer
and nothing to set up: it is one file, it uses only what Python already ships
with, and it keeps everything it writes in one folder you choose.

**Windows will warn you about it.** An unsigned executable nobody has seen
before gets flagged, and that is a reasonable thing for a computer to do about a
program that rewrites part of a game's executable. Signing is the only thing
that changes that, and this is not signed.

What you can check instead is where it came from. Releases are built by GitHub
Actions from a tagged commit — the workflow is in `.github/workflows/`, the
build log is public, and each binary carries an attestation tying it to the
commit that produced it:

```bash
gh attestation verify CairnKeeper.exe --repo <owner>/<repo>
```

That is a stronger answer than a published hash. A hash only helps if you build
it yourself and compare, and PyInstaller output is not byte-reproducible — your
build would differ from this one in ways that mean nothing. The attestation
says the file came from the source you can read, which is the actual question.

You need your own copy of Phantom Abyss, installed through Steam.

---

## Using it

Open the application. There are three options.

**Listen** — play on the real servers and keep everything you touch. Only useful
while the service is still up, and the only thing here with a deadline.

**Host** — play offline from what you have kept. No connection at all.

**Join** — play on a server somebody else hosts, against everything they have
gathered. There is a box for the address; a host name is enough.

Press **Play**. It prepares your game, starts the local server, launches Phantom
Abyss, and puts your game back exactly as it was when you quit. Your game is
only ever modified while a session is actually running — if one is interrupted,
opening the app again repairs it.

**Profiles** let you keep more than one save. A new profile starts the game from
the beginning, without touching the one you have, and you can switch back
whenever you like. Players have asked for a way to start over since release;
this is that, without buying the game again.

**Progress** shows what each of your saves has earned.

**Share** hands your temples and recordings to another player, or takes theirs
in.

After every session the app reads back what it kept and tells you whether it is
intact. A truncated file looks archived right up until you need it, and by then
there is nothing left to re-download it from.

---

## Building it yourself

You need Python 3.11 or newer. Nothing else — the client is deliberately pure
standard library.

```bash
python -m phantom_offline window     # the same window, from source
python build.py                      # freeze it into one executable
python -m pytest tests -q            # the test suite
```

`build.py --check` reports what it would do and why, including a check that
nothing on the play path is third-party.

---

## Privacy

This project moves other people's data around. That is the whole point of it,
and it is why the handling is deliberate rather than assumed.

**Your Steam account is never sent to any server.** The game signs in with a
SteamID64 and a live Steam authentication ticket. A preservation server needs
neither: it needs something stable to file a save under, and nothing more. So
the client swaps them — before a request leaves your machine, your platform id
becomes a handle minted locally, and the ticket is not forwarded at all. A
server you do not own is never told who you are. Not because it promises to
discard it, but because it never receives it.

It is a minted handle rather than a hash of your Steam id, because hashing an
enumerable input is not anonymity: a SteamID64 is `7656119` followed by ten
digits, and anybody holding the hashes can hash every candidate and recover the
list.

**What a shared folder contains.** Temples, the recordings of runs through them,
and the names the game already prints above every phantom you run past. What it
does not contain: your account, your progress, your login, or anybody's Steam
id, profile link or friend code. Recordings carry the submitter's SteamID64 and
Steam ticket inside them — those are stripped before a recording travels, and a
recording arriving with either still in it is refused at the door. A name that
is *itself* a platform id, which some players use, is replaced too.

**What your own archive contains.** Everything, faithfully, including other
players' usernames and your own credentials. It is yours and it stays on your
machine. `.gitignore` excludes it and the app never uploads it. Use the sharing
folder to give content to people; it carries what should travel and leaves the
rest behind, which is a promise a raw copy cannot make.

**If you host a server**, what you end up holding about your players is a save
each, filed under a handle their own client minted. No Steam ids, no tickets, no
friend lists.

---

## Sharing what you have

```bash
python -m phantom_offline pool export --out ./my-contribution
python -m phantom_offline pool import --folder ./someone-elses
python -m phantom_offline pool status
```

Or from the window: **Share** does both directions.

A contribution is a directory of blobs plus a manifest naming them by content
hash. Deliberately a folder rather than a service — it can be zipped and handed
to somebody today, which works with no server, no accounts and no hosting bill.

Taking one in has two options. **Add to my archive** merges it into yours.
**Keep it separate** leaves it read-only as a bundle you can switch off again,
so your own captures are never touched. Some people want their offline copy to
be exactly what they played; others want everything anybody saved. Neither is
more correct, so it asks.

Only temples the real service minted are ever contributed. A temple generated
offline is worth nothing to anybody — they can make their own — and its id would
collide with a real one.

Use `--no-names` to leave the runners' names out. Every phantom then appears as
"Explorer", which costs an archive most of what makes it feel like the game.

---

## Hosting a server

```bash
mkdir -p archive
docker compose up -d --build
```

Then give people your address; they put it in the **Join** box.

`--build` matters on every update — `docker compose up` on its own reuses the
image it already has. Make `archive/` yourself before the first run: a bind
mount whose host directory does not exist is created by Docker owned by root,
and then nothing running as you can write to it.

It runs as uid 1000, which on most Linux systems is you, so the files it writes
belong to you. If your uid is different, say so once:

```bash
echo PHANTOM_UID=$(id -u) > .env
echo PHANTOM_GID=$(id -g) >> .env
```

**An empty archive serves nothing.** Put content in it — somebody's contribution
folder, imported once:

```bash
docker compose exec cairnkeeper \
    python -m phantom_offline --state /archive pool import --folder /archive/incoming
```

Without Docker, `python -m phantom_offline serve --host 0.0.0.0` is the same
thing. Everywhere else the default is loopback: hosting is something you choose,
not something you get by accident.

---

## The command line

Everything the window does, and a good deal it does not. `--help` on any of
them.

| | |
|---|---|
| `window` | the graphical version; what the packaged build opens |
| `play` | a whole session: patch, serve, launch, clean up |
| `serve` | the server alone (`--capture` to forward and archive, `--host` to listen wider) |
| `launch` | start the game against it |
| `patch` / `unpatch` | point the game at localhost, or undo it |
| `pool` | share temples and phantoms, or take somebody's in |
| `bundles` | switch read-only content from other people on and off |
| `daily` | capture today's daily temple, or list what has been captured |
| `harvest` | archive temples from the live service without playing them |
| `archive` | download blobs referenced by captured responses |
| `verify` | check every blob and layout is intact (`--quick` for what changed) |
| `status` | what is archived and playable |
| `progress` | a player's relics, whips and challenge levels |
| `report` | flag a temple that cannot be finished, and share the list |
| `shares` / `shareharvest` | look up share codes, archive what they point at |
| `resync` | refresh a profile from the newest captured login |
| `pin` | force every fresh run onto one temple |
| `fidelity` | replay captured requests and diff against the real responses |
| `scrub` | strip credentials from older captures |
| `restore` | undo `Engine.ini` changes |

### Where the archive lives

Hundreds of gigabytes is realistic, so it usually belongs off your system drive.
Resolution order:

1. `--state` on the command line
2. `$PHANTOM_OFFLINE_STATE`
3. a path written in `.archive_path` beside the project
4. `./run`

---

## Temples that cannot be finished

Report one from the window after playing it, or:

```bash
python -m phantom_offline report last --reason missing-key --note "exit door"
```

A **generated** temple is replaced by rerolling its seed, so the slot refills
and no content is lost. An **archived** temple is skipped instead — its layout
is fixed bytes from the service, so there is no seed to reroll.

Reports travel with the content they are about, and are trusted according to who
made them. Your own report acts at once on your own machine. Somebody else's
withholds nothing until a second player who ran the same temple agrees, and a
reporter who condemns an implausible number of temples stops counting toward
that agreement. One player must not be able to blacklist an archive for
everybody.

---

## How it works

The client already contains the generator (Dungeon Architect). The server's job
is to hand out a seed, remember the layout the client builds, and store
phantoms — so this reimplements the *service*, not the game.

```
GetDungeon   -> seed + layout URL + up to ~50 phantoms
                (no layout URL means "client, build it yourself")
SubmitDungeonLayout <- the temple the client built
SubmitRun           <- the player's own recording
VerifyUserID -> the whole profile: currency, upgrades, route history
```

**Where progression actually lives.** Two places, which is not obvious. The
service holds reputation, victory routes and purchases. `UserProgress.sav` on
your disk holds whips, upgrades, relics and currency, and the game reads it at
startup without asking anybody. A profile has to carry both halves or it resets
nothing you can see.

**Relics, unlocked whips and challenge completion are not stored as lists.** The
client derives all three from `victoryRoutes`, so preserving route history
preserves all of them.

**A run is recorded once per floor.** A recording count is not a count of
ghosts: most players on floor two are the same people who survived floor one.
Both numbers are true and the app reports both.

**The daily is chosen by the date.** There is no seed to derive it from — every
captured daily response carries none, and the service simply decided which
temple it was. So a day nobody captured is a day nobody can play again, and the
days that were captured are replayed in an order every archive agrees on.

Responses are shape-identical to the real service's, checked by
`tools/compare_fidelity.py`, which replays captured requests through the offline
backend and diffs the result against what `wiby.net` actually returned.

The protocol work, and how each fact was established, is in
[docs/PROTOCOL.md](docs/PROTOCOL.md).

---

## Layout

```
phantom_offline/   the server, the window, the client patcher, the archiving
tools/             analysis: string dumps, fixture summaries, fidelity diffing
docs/PROTOCOL.md   the reconstructed protocol, and how each fact was established
tests/             ~460 tests, mostly regressions for bugs found the hard way
Dockerfile         hosting a server for other players
docker-compose.yml the same, with the archive mounted and the port published
```
---

## Scope, and what this repository contains

This talks to a game you own, on your own machine, to keep it working after the
service it depends on is switched off.

**It does not circumvent DRM, and it is not a way to play without buying the
game.** It replaces a network service, nothing else. You need your own copy,
installed and licensed the ordinary way, and it will not work without one. This
project does not condone piracy and offers no help with it.

**No game content is in this repository.** No game assets, no game code, no
temple layouts, no phantom recordings, no player records — only source code and
documentation. Phantom Abyss belongs to its rights holders, and nothing here is
affiliated with or endorsed by them.

The archive a player builds is their own capture of a service they were
themselves using, and it stays on their machine. Sharing exists so players can
give content to each other directly; nothing routes through this project.

---

## License

MIT, see [LICENSE](LICENSE). The license covers the code in this repository and
nothing else.
