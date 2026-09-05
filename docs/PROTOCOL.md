# Phantom Abyss — Backend Protocol (reconstructed)

All facts below were recovered statically from
`PhantomAbyss/Binaries/Win64/PhantomAbyss-Win64-Shipping.exe`
(UE 4.27.1, game build `0.9.1`, Steam appid `989440`).

Nothing here came from live traffic — the official servers were already
returning `503` when this work started (see [Server status](#server-status)).

---

## 1. Headline finding: level generation is client-side

The shipping binary contains the full procedural dungeon generator. The game
embeds **Dungeon Architect** (a UE marketplace plugin — `DungeonArchitectRuntime`)
with game-specific subclasses:

- `UWIBYSnapDungeonBuilder` / `UWIBYSnapDungeonConfig` / `UWIBYSnapDungeonModel`
- `UWIBYGridCustomDungeonBuilder` / `UWIBYGridDungeonConfig`
- `UWIBYDungeonMarkerEmitter`

Proof that generation runs on the client, not the server:

```
Could not extend into room %s exiting through door %i of room %s, no attachment
configuration matched. This is a critical failure for an Explicit Rooms dungeon
generation
```

That is a *generator* error message living in the client. Combined with the
`dungeonSeed` field in the server response and the existence of a
`/SubmitDungeonLayout` endpoint (the client **uploads** the layout it produced),
the model is:

1. Server hands the client a **seed** plus dungeon settings indices.
2. Client builds the temple deterministically with Dungeon Architect.
3. Client uploads the resulting layout so that *other* players who roll the same
   temple get a byte-identical one, and so recorded phantoms line up.

**Consequence for this project: we do not need to reimplement level generation.**
We need to reimplement the *server* that hands out seeds and stores phantoms.
That is a far smaller and far more tractable problem.

---

## 1a. Confirmed from live capture

Captured from the running client on 2026-08-26. Unlike everything else in this
document, these are observed facts rather than reconstruction.

**The client's verification request** (sent to `/VerifyUserID`):

```json
{
  "userId": 0,
  "playerId": "<steamid64>",
  "platform": "STEAM",
  "verificationToken": "<steam auth session ticket>",
  "platformVerificationId": "",
  "clientDungeonVersion": 128,
  "clientProtocolVersion": 7
}
```

This confirms the reconstructed `VerifiableRequest` fields
(`verificationToken`, `clientDungeonVersion`, `clientProtocolVersion`) and adds
`userId`, `playerId`, `platform`, `platformVerificationId`.

> `verificationToken` is a **live Steam authentication ticket** and `playerId` is
> the player's SteamID64. Both are credentials/PII. `phantom_offline/redact.py`
> strips them before anything is written to disk — do not disable that if you
> intend to share captures.

**The live server's maintenance response** (HTTP 503, verbatim):

```json
{
  "mode": 1,
  "newGamesLockedOut": false,
  "maintenanceTimeUTC": "2026-08-26T17:04:09.711Z",
  "serverVersion": 128,
  "serverProtocolVersion": 7
}
```

Note `serverVersion` == `clientDungeonVersion` == 128 and
`serverProtocolVersion` == `clientProtocolVersion` == 7. A replacement server
must report these, or the client is likely to treat it as out of date.
`mode: 1` accompanied a 503, so `0` is the presumed healthy value — unconfirmed.

**Unknown routes** return AWS API Gateway's `{"message": "Missing
Authentication Token"}` with HTTP 403. Seeing that in a capture means the URL
was wrong, not that authentication failed.

## 1b. The third domain: `dungeons.wiby.net`

**This is the part that actually matters for preservation.**

Temple layouts and phantom recordings do not travel in the JSON. `/GetDungeon`
returns URLs on a separate CDN, and the game fetches them *directly* — so they
never pass through a proxy pointed at the game service:

```
"layoutDownloadURL": "https://dungeons.wiby.net/version_128/2024-02-23/
                      dungeon-701203-floor-2-layout-ygck77l0"
"ghostRuns": [{"downloadURL": ".../dungeon-701203-floor-2-runinfo-lttxkd70", ...}]
```

The URL structure is `/<version>/<date>/dungeon-<id>-floor-<n>-<kind>-<slug>`
where `<kind>` is `layout` or `runinfo`.

### Layout blob format

Base64-encoded JSON. Decoded, one floor of a real temple:

```json
{
  "numWings": 2,
  "randomCurrent": -352918470,
  "chosenGuardian": "/Game/Player/Abilities/AbilityDescriptions/GuardianGrenadesDetails...",
  "roomsHash": 2073586747,
  "difficulty": 0,
  "connections": [ {"moduleAInstanceId", "moduleBInstanceId", "doorAName", "doorBName"} ],
  "roomInfos":   [ {"moduleClassPath", "nodeId", "roomIndex", "wingId",
                    "roomDistToStart", "roomDistToEnd", "transform",
                    "traps", "visualPack"} ]
}
```

That is the entire temple: the room graph, every room's placement and traps, the
guardian, and the RNG state. **A seed alone is not enough to reproduce a temple**
— the layout blob is authoritative, which is why archiving these is the priority.

`phantom_offline/assets.py` harvests every download URL from captured responses
and stores the blobs; `phantom_offline/library.py` indexes them by
`(dungeon id, floor)` and serves them back.

## 1c. Real `/GetDungeon` exchange

Request (gzipped when large — see the 415 note below):

```json
{"gameMode": "DGM_ADVENTURE", "dungeonSettingsIndex": 0, "routeId": 205602,
 "routeStage": 0, "dungeonFloorNumber": 2, "shareCode": "", "knownDungeons": [],
 "whipWagered": "Bamboo", "dungeonId": 701203, "isRetry": false,
 "difficultyRating": 0, "curseLevel": 530, "networkFilter": false,
 "platformFriends": ["<steamid>", ...], "userId": 62842, "playerId": "<steamid>",
 "platform": "STEAM", "verificationToken": "...", "platformVerificationId": "",
 "clientDungeonVersion": 128, "clientProtocolVersion": 7}
```

Response:

```json
{"serverStatus": {"mode": 1, "newGamesLockedOut": false, "maintenanceTimeUTC": "...",
                  "serverVersion": 128, "serverProtocolVersion": 7},
 "userID": 62842, "routeID": 205602, "playerCount": 1214, "deathCount": 166,
 "routeStage": 0, "dungeonID": 701203, "dungeonSeed": -951959203, "areaID": 1,
 "gameMode": 1, "difficultyRating": 0, "dungeonSettingsIndex": 0,
 "dungeonFloorNumber": 2, "dungeonLayoutType": 2, "dungeonFloorTotalCount": 3,
 "dungeonFloorType": 3, "dungeonVersion": 128, "totalDungeonAttemptsAllFloors": 0,
 "temporaryPowerIndex": -1, "whipWagered": null, "relicIDs": null,
 "relicCollectorIDs": null, "relicCollectorNames": null,
 "health": {"baseHealth": 3, "baseDailyHealth": 3},
 "precalculatedRooms": null, "layoutDownloadURL": "...",
 "persistentChanges": null, "ghostRuns": [...]}
```

Corrections to the earlier reconstruction, all confirmed by capture:

- **`serverStatus` is an object, not an integer.** It is the same struct as
  `maintenanceInfo`.
- **`mode: 1` is healthy.** It appears on successful 200 responses. (It also
  appears on 503s, so `mode` is not the health signal at all — the HTTP status
  is.)
- **`dungeonSeed` is a signed int32** (`-951959203` observed).
- Field casing is `routeId` / `dungeonId` on the request but `routeID` /
  `dungeonID` on the response.
- `gameMode` is a string in the request (`"DGM_ADVENTURE"`) and an int in the
  response (`1`).

One temple, per floor: floor 0 seed `1500488846` (layout type 3, floor type 1),
floor 1 seed `138833038` (3, 4), floor 2 seed `-951959203` (2, 3).

`ghostRuns[]` entries carry `runData` (empty), `userID`, `userName`, `success`,
`lifetime`, `endLocation`, `runID`, `downloadURL`, `reputation`, `platform`,
`platformUserID`.

### Gzipped requests

Larger request bodies (`/GetDungeon`, `/GetFriends`) are sent with
`Content-Encoding: gzip`; smaller ones (`/WakeUp`, `/VerifyUserID`) are not. A
proxy that decompresses the body **must drop the `Content-Encoding` header**
before forwarding, or the server answers `415 Unsupported Media Type`.

## 1d. `/GetDungeon` will not re-serve a finished route

Tested directly, and it rules out bulk archiving of play history.

Replaying the client's own known-good `/GetDungeon` request — same route
(`205602`), same dungeon (`701203`), same floor, same `curseLevel`,
`whipWagered` and populated `platformFriends`, with live captured credentials —
returns **500 Internal Server Error**. The identical request from the game
succeeded an hour earlier, while that route was still in progress.

The pattern across attempts:

| Request | Result |
|---|---|
| Floor 0 of a completed route | `400 Bad Request` |
| Floors 1+ of a completed route | `500 Internal Server Error` |
| Any request with empty `platformVerificationId` | `400 Bad Request` |

So the endpoint is bound to **run state**, not just identity: it serves a temple
for a route you are currently attempting, and refuses one you have already
finished. `victoryRoutes` is a record of what you played, not a menu you can
replay.

Two consequences:

1. **Temples must be archived as they are played**, while the route is live.
   There is no retroactive bulk pull.
2 `platformVerificationId` is mandatory. The client sends it empty on `/WakeUp`
   and only populates it once fully logged in, so credentials scraped from an
   early request are unusable — see `phantom_offline/session.py`.

A related dead end: most of a long-lived account's history is on old content
versions (this account: 53 of 55 routes on versions 34–87, only 2 on the current
128). Even if run state allowed it, those temples are long gone from the CDN.

The untested avenue is **share codes**. `/GetDungeon` accepts a `shareCode`, and
`/VerifyShareCode` and `/GetDungeonShareList` exist, which suggests a shared
temple can be requested by code without having an active route for it.

## 1e. Real `/SubmitRun` exchange

The phantom recording travels **inline**, not via the CDN — `runData` is a
~244 KB string in the request body. That means a replacement server can keep the
player's own ghosts with no extra fetching.

Request (confirmed by capture):

```json
{"dungeonId": 747351, "dungeonFloorNumber": 0, "routeId": 222645,
 "routeAttemptId": 0, "userId": 62842, "playerUniqueId": 62842,
 "playerId": "<steamid>", "platform": "STEAM",
 "gameMode": "DGM_CLASSIC", "leaderboardType": "DLT_SCORE",
 "lifetime": 99.46453094482422, "success": 1, "isSandbag": 0,
 "endLocation": "[146.591370,50962.441406,424.577484]",
 "collectedRelicId": "", "permanentSettingsData": "", "reputation": 0,
 "networkFilter": false,
 "currency": {"dungeonKeys": ..., "essence": ...},
 "dailyScoreSubmission": {"m_score", "m_totalTime", "m_whip", "m_floorScoreData"},
 "playerStats": { ...60 counters... },
 "runData": "<~244 KB recording>",
 "verificationToken": "...", "platformVerificationId": "...",
 "clientDungeonVersion": 128, "clientProtocolVersion": 7}
```

Response:

```json
{"userID": 62842, "routeID": 222645, "dungeonID": 747351,
 "dungeonFloorNumber": 0, "attemptID": 0, "success": false,
 "isSandbag": false, "routeTotalEssence": 0, "updatedReputation": 0,
 "temporaryPowerIndex": -1, "currency": null, "health": null,
 "lastRunRouteInfo": null, "activeClassicRoutes": [],
 "dailySubmissionResponse": {"routeID", "leaderboardType", "leaderboard",
                             "clearanceRate", "expiryTime"},
 "maintenanceInfo": { ...as elsewhere... }}
```

`attemptID` on the receipt is the route attempt, the same value as
`lastRunRouteInfo.routeAttemptID`: 0 in 102 of 103 captured receipts and 1 in
the one whose route block also said 1. It is not a count of submissions. A
receipt whose `attemptID` does not match the attempt the client is on is not
treated as the receipt for the run it just submitted, and the relic and whip a
completed route earns then arrive at the next login (from `victoryRoutes`)
instead of at the hub. `dailySubmissionResponse.leaderboardType` is the
request's `leaderboardType` as an enum value (`DLT_SCORE` 0, `DLT_TIME` 1).

Note the request uses `dungeonId`/`routeId`/`playerUniqueId` while the response
uses `dungeonID`/`routeID`/`userID` — the same casing split seen on
`/GetDungeon`. `playerStats` is a flat object of ~60 lifetime counters, sent
in full on every submission.

`phantom_offline/backend.py` stores `runData` as a phantom blob for that temple
and registers it, so an offline player's own ghosts appear on later visits.

## 1f. Harvesting, route mechanics and content variety

Confirmed by tracing a full play session and probing the live server.

### The route loop

```
GetDungeon(routeId=0, dungeonId=0, floor=0)   -> mints a NEW route + temple
SubmitRun(..., success=1)                     -> that floor was finished
GetDungeon(routeId=R, dungeonId=D, floor=1)   -> next floor of that temple
GetDungeon(routeId=R, dungeonId=0, floor=0, routeStage=N+1) -> next temple
```

Two facts make bulk archiving possible without fabricating anything:

1. `routeId=0, dungeonId=0` mints a brand new route and temple **every time**.
2. Later floors can be fetched **without submitting a run first**.

So `phantom_offline/harvest.py` never calls `/SubmitRun`. Faking completions
would publish phantoms other players race against and corrupt the account's
statistics; walking the floors directly archives the same data and claims
nothing untrue.

### What cannot be harvested

* **Finished routes.** Re-requesting a completed route returns 400 (floor 0) or
  500 (later floors), even replaying the client's own successful request.
* **Deeper stages.** Asking for `routeStage=N` on a fresh route returns an empty
  placeholder (`areaID=0`, `dungeonFloorTotalCount=0`, no ghosts). Real areas 2+
  are reachable only by genuinely progressing a route.
* **Old content versions.** Most of a long-lived account's history sits on
  retired versions whose layouts are gone from the CDN.

Neither `dungeonSettingsIndex` (0-3) nor `curseLevel` changes what a fresh route
returns -- always `areaID=1, dungeonFloorType=1, dungeonLayoutType=3`.

### Guardians vary by mode, not by account

Three exist: `GuardianChaseDetails`, `GuardianGrenadesDetails`,
`GuardianShootDetails`. Measured across an archive of 100+ layouts:

| mode | guardian |
|---|---|
| `DGM_ADVENTURE` | Grenades (91/91) |
| `DGM_CLASSIC` | Grenades (8/8) |
| `DGM_DAILY` | Chase (1/1) |

`chosenGuardian` lives in the layout blob, which is server-side per dungeon, so
it is not a client setting. Variety comes from playing different modes, not from
harvesting more of one.

### `UWIBYDebugSettings`

A block of developer settings exists in the shipping binary:

```
UseDebugLevel              DebugEntryGameMode        DisableGuardians
DoLevelIntro               AdventureModeAreaOverride m_guardianOverride
RecordGhosts               AdventureModeRelicIndexOverride
RecordTempleLayout         DungeonTestSeed           DebugDifficultyOverride
OfflinePowerShrines        DebugRoomList             DebugVisualsPack
TestDungeonGeneration      DebugDifficultySettings   DebugDLCs
```

`AdventureModeAreaOverride` and `DungeonTestSeed` would be the clean way to
reach areas 2+ and specific seeds. Whether they are config-backed
(`[/Script/PhantomAbyss.WIBYDebugSettings]` in `Game.ini`) is **untested**.

## 1g. The client builds temples; the server only stores them

The single most important finding for long-term preservation.

`UDungeonBuilder` and `UWIBYGameInstance::SendDungeonLayout` ->
`/SubmitDungeonLayout` show the client generates a temple locally and uploads
it. `/SubmitDungeonLayout` **never appears in captured traffic**, because every
temple the live server served already had a `layoutDownloadURL`.

The implication: a replacement server does not need an archived layout for every
temple. Hand the client a `dungeonSeed` with an empty `layoutDownloadURL`, and
it builds the temple itself and posts the result back. Store that under the
CDN's naming convention and it is indexed and replayable exactly like a
downloaded one.

So an offline server can keep producing **new** content indefinitely, rather
than only replaying what was archived before shutdown.

### Confirmed against the real client (2026-08-27)

An offline server with an **empty archive** served a seed and an empty
`layoutDownloadURL`. The client built the temple itself and uploaded it:

```json
{"dungeonId": 543832, "areaId": "Ruins", "gameMode": "DGM_ADVENTURE",
 "dungeonFloorNumber": 0, "dungeonFloorCount": 3, "dungeonFloorType": 1,
 "dungeonLayoutType": 3, "dungeonSettingsIndex": 0, "dungeonVersion": 0,
 "savedLayoutData": "<101,540 chars>",
 "permanentSettingsData": "eyJwZXJtYW5lbnRzQXJyYXkiOltdfQ=="}
```

Decoded: `numWings 1`, **16 roomInfos, 15 connections** -- indistinguishable
from a CDN layout (real ones are 100-260 KB with 15-16 rooms). The temple
loaded and played normally.

Note `areaId` is a **string** here (`"Ruins"`), unlike the numeric `areaID`
every other endpoint uses.

### The floor-shape constraint (load-bearing)

`(dungeonLayoutType, dungeonFloorType)` must match a combination that actually
occurs at that depth. Serving floor 0 as `layoutType 2 / floorType 3` -- valid
only on floor 2 -- made the generator produce `numWings 0` with no rooms and no
connections, and the game hung on a black screen. The client still used the
seed (`randomCurrent` matched exactly); it simply could not build from an
impossible shape.

Measured across 600+ real temples:

| floor | layoutType | floorType |
|---|---|---|
| 0 | 3 | 1 |
| 1 | 3 | 4 |
| 2 | 2 | 3 |

Encoded as `FLOOR_SHAPES` in `backend.py`.

### Consequence

Temples are **not** a finite resource offline. Only phantoms are: every
recording is a real person's run, capped near 50 per floor, unrefreshable once
captured. Harvesting is about preserving phantoms; the temples that carry them
can be regenerated at will.

## 1h. Game modes: what the player sees vs what the protocol calls it

The in-game menu offers three modes. The protocol names do not match the UI
names, which is worth stating plainly because `DGM_CLASSIC` is **not** a
"Classic mode" the player ever sees:

| Menu | Protocol | Structure (from captured traffic) |
|---|---|---|
| Adventure Mode | `DGM_ADVENTURE` | Chains of 3-floor temples. `routeStage` is the position in the chain; the UI shows `RUINS 1/6`. Chains are per-area and unlock in order ("Break Ruins chain to unlock"). |
| **Abyss Mode** | `DGM_CLASSIC` | One 8-floor descent: Ruins → Caverns → Inferno → Rift, as four 2-floor temples. `routeStage` 0-3 is the area. Collect 4 relics, one per area. Has difficulty tiers. |
| Daily Mode | `DGM_DAILY` | A single 2-3 floor temple, one per day, unlimited retries, scored on a global leaderboard with a clearance rate. |

Measured across the archive:

```
DGM_ADVENTURE  areaID [1]        floors/temple [3]  routeStage [0..3]
DGM_CLASSIC    areaID [1,2,3,4]  floors/temple [2]  routeStage [0..3]
DGM_DAILY      areaID [1]        floors/temple [2]  routeStage [0]
```

`DGM_PRACTISE` and `DGM_ERRORMODE` exist in the enum but are not player modes.

### Abyss difficulty

`difficultyRating` on `/GetDungeon` is the Abyss difficulty tier. The binary has
`AbyssDifficulty`, `eAbyssDifficulty`, `eDifficultyTier`,
`abyssDifficultySettings` and `IsAbyssDifficultyUnlocked`, with names `master`
and `insane`; the menu shows Master → Insane → Nightmare → (a fourth), each
unlocked by beating the previous.

**Every capture so far has `difficultyRating: 0`**, because only Master was
unlocked on the accounts used. The higher tiers are unobserved.

### Challenge Level is `curseLevel`

The number the UI calls "Challenge Level" is `curseLevel` in the protocol -- a
run displayed as Challenge Level 250 was recorded as `curseLevel: 250`.
Observed range across modes: 0-930 in normal play, 1,000 on Abyss Master.

Reward tiers, read off the challenge screen: base completion awards the relic
and the whip; 400 awards a whip skin; 600 awards Gold Shine. Progress is tracked
in `playerStats.HighestAdventureHeatCompletedPerWhip`, a map of whip name to the
highest challenge level beaten with it.

### Two kinds of relic

Adventure challenge relics (one per challenge temple, e.g. `SoldierDart`) are
distinct from the four Abyss relics collected one per area. Both land in the
same place -- `victoryRoutes[].dungeons[].relicIDs`.

## 1i. Abyss route structure, and what a harvester can reach

Abyss (`DGM_CLASSIC`) is one descent through all four areas, but it is **not
one dungeon**. A route spans four dungeons -- one per area -- and
`dungeonFloorTotalCount` is the floor count of the *current dungeon*, not the
route. In-game totals:

| Tier | `difficultyRating` | Floors in the route |
|---|---|---|
| Master | 0 | 8 |
| Insane | 1 (inferred) | 10 |
| Nightmare | 2 (confirmed) | 12 |
| Unchained | 3 (inferred) | 13 |

Master is 2 floors in each of Ruins/Caverns/Inferno/Rift; the harder tiers
split the extra floors between areas unevenly. Nightmare's first dungeon has 3
floors. Unchained advertises Challenge Level 2,000.

### How the client walks a route

```
GetDungeon(routeId=0, dungeonId=0, floor=0)  -> route R, dungeon A (area 1)
GetDungeon(routeId=R, dungeonId=A, floor=1)  -> next floor of A
SubmitRun(...)
GetDungeon(routeId=R, dungeonId=0, floor=0)  -> dungeon B (area 2), stage 1
```

`dungeonId=0` on an **existing** route advances to the next area.

### The limit

Requesting the next stage without a `SubmitRun` in between returns **400**.
Verified on an untouched Nightmare route: stage 0 archived cleanly (dungeon
707869, 3 floors, 44/50/51 phantoms), stage 1 refused.

So a harvester reaches **the first dungeon of an Abyss route only** -- 3 of 12
floors on Nightmare, 2 of 8 on Master. Areas 2-4 require actually playing, or
fabricating run submissions, which this project does not do.

### Difficulty tiers are separate temple pools

Draining Master left it minting empty sequential temples for everyone,
including the game client -- while Nightmare still returned a populated temple
(64 players) on its first request. Each tier holds its own supply of unbeaten
temples, and Abyss retires a temple once anybody beats it, so populated ones
are a slowly-replenishing resource. Harvest Abyss in small, spaced batches.

## 1j. Advancing an Abyss route without playing it (UNUSED -- last resort)

Documented because the option may matter if a shutdown date is announced, when
polluting records costs nothing because they are about to be lost anyway. It
has **not** been used to harvest, and the harvester will not do it.

### The problem

An Abyss route spans four dungeons and only the first is reachable read-only
(see 1i). Asking for the next stage returns 400 until the current dungeon's
last floor has been submitted. So areas 2-4 need a `/SubmitRun`.

### What was measured

| Submission | Result |
|---|---|
| `success=1`, empty `runData` | **400** -- twice, including a fully well-formed request |
| `success=0`, empty `runData` | **200** -- three real client submissions did this |
| `success=1`, real `runData` | **200** -- accepted |

So the server requires a claimed floor *clear* to carry an actual recording. A
death may be submitted with none, but a death ends the route rather than
advancing it.

The accepted `success=1` returned `currency: null`, meaning it was treated as a
mid-route floor clear exactly like a genuine one.

### What is still unproven

Whether an accepted submission actually unlocks the next area. The single test
ran against a **degenerate temple** -- Master's pool was drained, so the route
returned `players 0, ghosts 0, floors 1` where a real Master temple has 2.
Stage 2 still returned 400, but that temple was not properly instantiated, so
the result is inconclusive rather than negative.

A conclusive test needs a populated temple, i.e. a tier whose pool is not
drained.

### The procedure, if it is ever needed

1. Mint a route: `GetDungeon(routeId=0, dungeonId=0, floor=0)`
2. Archive every floor of the returned dungeon
3. `SubmitRun` the last floor with `success=1` and a real `runData` blob
4. `GetDungeon(routeId=R, dungeonId=0, floor=0)` -> next area
5. Repeat for four areas, and **never submit the final floor**, so the temple
   is not beaten and stays available to real players

Copy the request shape from a captured `success=1` submission: it needs a
populated `playerStats` (60 counters) and a `dailyScoreSubmission` object, not
nulls.

### Why it is not used

`runData` is movement data. Replayed in a temple it was not recorded in, the
phantom walks through walls and floats through geometry, so anyone who rolls
that temple downloads a visibly broken ghost. Every other technique in this
project is read-only or touches only the operator's own account; this one
degrades other players' sessions while the servers are still live.

The one test submission went to dungeon `747712` -- an empty temple with zero
other players -- specifically to keep that exposure near zero.

## 2. Transport

- Plain HTTP/1.1 with JSON bodies (`Content-Type: application/json`).
- `Accept-Encoding: gzip`, `User-Agent: X-UnrealEngine-Agent`.
- Requests are `POST` (the literal `POST` appears next to the endpoint strings).
- Timestamps use `%Y%m%dT%H%M%S%sZ`.

### Environment selection

`EServerTarget` enum values, in order:

| Value | Name              | Redirector base                        | Game service base                        |
|------:|-------------------|----------------------------------------|------------------------------------------|
| 0     | `ST_None`         | —                                      | —                                        |
| 1     | `ST_Development`  | `https://redirector.dev.wiby.net`      | `https://gameservice.dev.wiby.net`       |
| 2     | `ST_Test`         | `https://redirector.test.wiby.net`     | `https://gameservice.test.wiby.net`      |
| 3     | `ST_Beta`         | `https://redirector.beta.wiby.net`     | `https://gameservice.beta.wiby.net`      |
| 4     | `ST_Production`   | `https://redirector.wiby.net`          | `https://gameservice.wiby.net`           |
| 5     | `ST_Local`        | `http://localhost:54908`               | `http://localhost:54908`                 |

There is also `https://gameservice.xboxsandbox.wiby.net` and a `LocalServer`
string. **The shipping build already knows how to talk to a local server on
port 54908 over plain HTTP.**

### Boot sequence

```
GET/POST  <redirector>/redirectV3.json   ->  InitialServerResponse
POST      <gameservice>/WakeUp           ->  WakeUpResponse
POST      <gameservice>/VerifyUserID     ->  VerifyUserResponse  (WIBYUserResponse)
POST      <gameservice>/GetDungeon       ->  WIBYDungeonResponse
```

`InitialServerResponse.targetEndpoint` is what redirects the client onto the real
game service — so whoever controls the redirector controls where everything else
goes.

---

## 3. Endpoints

Recovered endpoint paths:

| Endpoint | Purpose |
|---|---|
| `/redirectV3.json` | Redirector; returns `InitialServerResponse` |
| `/WakeUp` | Wake/health handshake |
| `/GetMaintenanceStatus` | `ServerMaintenanceStatusResponse` |
| `/VerifyUserID` | Login / identity; returns `WIBYUserResponse` |
| `/RefreshVerification` | Re-auth |
| `/GetDungeon` | **Seed + dungeon parameters** |
| `/SubmitDungeonLayout` | Client uploads generated layout |
| `/SubmitRun` | Upload run result + phantom |
| `/GetDailyDungeonInfo` | Daily challenge info |
| `/GetDungeonShareList` | Shared temples by a user |
| `/VerifyShareCode` | Resolve a share code |
| `/PurchaseUpgrade` | Permanent upgrade purchase |
| `/PurchaseTemporaryPower` | In-run power purchase |
| `/ConvertKeys` | Key tier exchange |
| `/SpendCurrency` | Generic spend |
| `/CompleteTutorialDungeon` | Tutorial completion |
| `/AddFriend`, `/GetFriends` | Social |
| `/RequestDataReset` | Wipe account |
| `/DebugGiveKeys`, `/DebugGiveEssence` | Debug grants |

---

## 4. Struct schemas

Field names are exact — UE 4.27 stores reflected property names as ASCII in the
struct registration tables, laid out in declaration order immediately *before*
the struct's own name. Types below are inferred from usage and are the part most
likely to need correction against a live client.

### Base / shared

```
VerifiableRequest
  verificationToken        string
  clientDungeonVersion     int
  clientProtocolVersion    int

WakeUpServerRequest
  clientVersion            string

InitialServerResponse            # redirectV3.json
  globalValues             ServerGlobalValues
  maintenanceInfo          <maintenance struct>
  targetEndpoint           string    # where the client goes next

ServerGlobalValues
  relicConversionRewards   RelicConversionRewards
  keyConversionUpRates     int[]
  keyConversionDownRates   int[]

RelicConversionRewards
  rewards                  ...
  rewardsByArea            map
  rewardsByArea_Key        ...
```

### Dungeon (the important one)

```
WIBYDungeonRequest
  knownDungeons            [] # dungeons the client already has cached
  platformFriends          []
  (+ VerifiableRequest fields)

WIBYDungeonResponse
  serverStatus                    int
  playerCount                     int
  deathCount                      int
  routeStage                      int
  dungeonSeed                     int64   # <-- drives generation
  dungeonFloorTotalCount          int
  difficultyRating                float
  terminatedOldRuns               bool
  totalDungeonAttemptsAllFloors   int
  temporaryPowerIndex             int
  whipWagered                     int
  relicCollectionFlags            int
  relicCollectorIDs               string[]
  relicCollectorNames             string[]
  health                          int
  lockedRoutes                    []
  precalculatedRooms              []      # inline pre-baked room list
  layoutDownloadURL               string  # or fetched out-of-band
  persistentChanges               []
  ghostRuns                       []      # phantom descriptors

WIBYDungeonLayoutSubmission
  dungeonFloorNumber       int
  dungeonFloorCount        int
  dungeonLayoutType        EDungeonFloorLayout
  dungeonFloorType         EDungeonFloorType
  dungeonVersion           int
  dungeonSettingsIndex     int
  areaID                   EAreaID
  savedLayoutData          <blob>
  permanentSettingsData    <blob>
```

Note `layoutDownloadURL` / `downloadURL`: bulk layout and phantom data are
fetched from a separate URL rather than inlined, so a replacement server must
also host those blobs (it can point them at itself).

### User / progression

```
WIBYUserRequest
  requestedRouteID         int64
  requestedRouteVersion    int

WIBYUserResponse
  currentUsername            string
  lastRouteInfo              RouteInfo
  victoryRoutes              []
  permanentPurchases         []
  dailyDungeonInfo           WIBYDailyDungeonInfo
  dailyDungeonInfoYesterday  WIBYDailyDungeonInfo
  playerStatistics           ...

RouteInfo
  completedByID            string
  completedByName          string
  routeAttemptCount        int
  sandbag                  bool
  wageredWhipID            int
  storedCurrency           int

WIBYPlatformVerificationResponse
  platformVerificationID   string
```

### Runs / phantoms

```
WIBYDungeonRunSubmission
  playerUniqueID           string
  lifetime                 float
  isSandbag                bool
  collectedRelicID         int
  currency                 ...
  runData                  <blob>
  dailyScoreSubmission     ...
  routeAttemptID           int64
  playerStats              ...

WIBYRunUploadReceipt
  routeTotalEssence        int
  updatedReputation        int
  lastRunRouteInfo         RouteInfo
  activeClassicRoutes      []
  attemptID                int64
  dailySubmissionResponse  ...

WIBYDungeonRunDownload
  userName                 string
  runID                    string
  platformUserID           string
  downloadURL              string

WIBYDungeonFloorGhostNames
  ghostNames               string[]
  ghostIDs                 string[]
  ghostPlatforms           string[]
  stageIndex               int
  floorIndex               int
```

Ghost blob naming scheme (printf formats found in the binary):

```
Ghosts_route_%u_stage_%u_dungeon_%u_floor_%i_instance_%i
Ghosts_adventureArea_%u_challengeIndex_%u_dungeon_%u_floor_%i_instance_%i
Ghosts_daily_dungeon_%u_floor_%i_instance_%i
PermanentChanges_mode_%c_route_%u_stage_%u_dungeon_%u_floor_%u
```

### Daily / leaderboard

```
WIBYDailyDungeonInfoRequest
  leaderboardType          EWIBYDailyLeaderboardType
  networkFilter            int

WIBYDailyDungeonInfoResponse
  today                    WIBYDailyDungeonInfo
  yesterday                WIBYDailyDungeonInfo

WIBYDailyDungeonInfo
  expiryTime               string
  leaderboard              LeaderboardEntry[]
  clearanceRate            float

LeaderboardEntry
  whip                     int
  score                    int
  totalTime                float
  placement                int
  completed                bool
```

### Economy

```
ConvertKeysRequest       requestedAmount, convertUp, purchaseID
ConvertKeysResponse      sourceKeyType, amountExchanged, destinationKeyType, amountReceived
PurchaseUpgradeRequest   upgradeID, toLevel
PurchaseUpgradeResponse  upgrades
PurchaseTemporaryPowerResponse  powerIndex
SpendCurrencyRequest     essences, keys
SpendCurrencyResponse    purchaseItemID, spentEssence, spentKeys, result
HealthQueryResponse      lockedDungeons
```

`EPurchaseResult`: `PR_Success`, `PR_InsufficientFunds`, `PR_Failure`,
`PR_AlreadyPurchased`.

### Social / sharing

```
FriendDetails            userID, platformID, platformProvider, avatar, isOnline,
                         isPlaying, userShareCode, shareCodes
WIBYFriendList           friends
WIBYAddFriendRequest     userToAdd
WIBYFriendListRequest    friendPlatformIDs
ShareCodeVerificationRequest   dungeonShareCode
ShareCodeVerificationResponse  isValid, alreadyAttempted, sharedBy, sharerID, routeInfo
BasicDungeonInfo         numFloors, settingsIndex, numGhosts, relicCollectorUserIDs
DungeonListBySharerResponse    userExists, sharerUserID, username, sharedRoutes
```

---

## 5. Relevant enums

```
EServerTarget          ST_None, ST_Development, ST_Test, ST_Beta, ST_Production, ST_Local
EDungeonFloorLayout    DFL_UNSET, DFL_LINEAR, DFL_HUB_SPOKES, DFL_EXPLICIT
ELayoutDifficulty      LD_NO_DIFFICULTY, LD_EASY, LD_MEDIUM, LD_HARD
ELayoutStacking        LS_Floor, LS_MidSecLow, LS_MidSec, LS_MidSecHigh, LS_Wall, LS_Ceiling
EWIBYDifficulty        D_UNSET, D_NORMAL, D_HARD, D_VERY_HARD, D_SUPER_HARD, D_HARDEST
EGhostFriendStatus     GFS_Unknown, GFS_NotFriend, GFS_Friend, GFS_FollowTarget
EPurchaseResult        PR_Success, PR_InsufficientFunds, PR_Failure, PR_AlreadyPurchased
EPlayerUpgradeType     PU_NONE, PU_HEALTH, PU_COINS, PU_WHIP_LENGTH, PU_WHIP_SPEED,
                       PU_DODGE, PU_1_RUN_POWER_OPTIONS, PU_DASH_SPEED, PU_DASH_COOLDOWN,
                       PU_KEY_EXCHANGE_OPTIONS, PU_POWERS_TIER, PU_CURSES_TIER,
                       PU_*_TRAPS (pendulum/dart/blade/spike/mine/boulder/crusher/defender/gas),
                       PU_DEVOURING_RAGE, PU_EYE_OF_AGONY, PU_MASKED_DEFILER,
                       PU_PIT_DAMAGE, PU_LANDING_DAMAGE
EAreaID, EWIBYRoomType, EDungeonFloorType, EDungeonGameMode
```

---

## 6. `UWIBYDebugSettings`

A developer settings object on the game instance (`m_debugSettings`). Its
properties, in declaration order:

```
UseDebugTempleSettings, InfiniteStamina, InfiniteHealth, SkipSplashScreens,
ForceTutorial, SkipTutorial, ForceRelicSequence, OwnAllRelics, OwnAllWhips,
AllWhipsGoldenAura, AllUpgradesPurchased, CorporealGhosts, RouteID,
DungeonSeed, OverrideFloorType, OverrideLayoutType, ServerTarget, UserProgress
```

A second cluster of debug flags (same subsystem):

```
UseDebugLevel, DoLevelIntro, requestedUserName, DisableDamageDodgeUpgrades,
IgnoreInstantDeathEvents, RecordGhosts, RecordTempleLayout, NoMenusRetry,
RecordTutorialGhost, ShowPrototypeSettings, ForceEssenceSequence, ShowAllNPCs,
DebugHub, ShowFloorRemovalVolumes, TestDungeonGeneration, PrintDangerChanges,
OfflinePowerShrines, CapMaxFPSTo5, DebugEntryGameMode, AdventureModeAreaOverride,
AdventureModeRelicIndexOverride, DungeonTestSeed, DebugDailyTime, OverrideLevel,
DebugDifficultySettings, DebugRoomList, DebugDifficultyOverride, DebugVisualsPack,
DisableGuardians, DebugDLCs, DebugSpecialEvent
```

`ServerTarget`, `DungeonSeed` and `TestDungeonGeneration` are the interesting
ones. These live in Blueprint defaults inside the `.pak`, so changing them means
repacking — which is why the interception approach below is preferred.

---

## 7. Interception levers (no binary patching required)

The shipping build honours these, all reachable from the **user-writable**
`%LOCALAPPDATA%\PhantomAbyss\Saved\Config\WindowsNoEditor\Engine.ini`:

- `[HTTP.Curl] bVerifyPeer=False` — disables `CURLOPT_SSL_VERIFYPEER`.
- `n.VerifyPeer` — console variable for the same thing.
- `HttpProxyAddress` / `httpproxy=` command line — route all HTTP through a proxy.
- CA bundle is loaded from `Certificates/cacert.pem`.

So a replacement server can be installed by pointing `redirector.wiby.net` at
localhost (hosts file) and disabling peer verification, or by driving the client
to `ST_Local` / port 54908.

---

## 8. Server status

Checked at the start of this project:

```
redirector.wiby.net   -> 3.169.202.x   (AWS)   GET /redirectV3.json  -> 403 (S3 AccessDenied)
gameservice.wiby.net  -> 3.169.202.x   (AWS)   POST /WakeUp          -> 503
```

Edge infrastructure still answers; the origin is gone. Live traffic capture is
therefore not possible — everything above is static reconstruction.

---

## 1k. `dungeonSettingsIndex` selects the area's settings collection

**In Abyss, `dungeonSettingsIndex` equals `routeStage`.** It is not an echo of
whatever the client sent.

Measured across 380 real Abyss dungeons — 5 from our own captures, the rest from
routes other players shared — with no exceptions:

| routeStage | areaID | area    | dungeonSettingsIndex | n   |
|-----------:|-------:|---------|---------------------:|----:|
| 0          | 1      | Ruins   | 0                    | 136 |
| 1          | 2      | Caverns | 1                    | 104 |
| 2          | 3      | Inferno | 2                    | 104 |
| 3          | 4      | Rift    | 3                    | 77  |

Adventure is unaffected: it is a single area and the index stays 0.

### Why it matters

The index picks a dungeon settings collection. The shipping binary reaches these
through `GetDungeonSettingsCollection`, and the collection carries at minimum:

- `TempleKeyChances` / `UTempleKeyDataAsset` — where and whether keys spawn
- `GetGuardianStartingLevel`
- `weightedCurseOptions`

So the index is not cosmetic. It is part of the generator's input.

### The bug it caused

We originally read the index from the request and echoed it back, which meant
every Abyss area was served with **index 0** — the Ruins settings.

The visible symptom was a temple that generated and rendered normally but could
not be finished: on the last Rift floor the exit door stayed shut, and pausing
near it surfaced an "Insert Key" prompt that did nothing. The door wanted a key
the generator had never been configured to place.

This is worth calling out because Phantom Abyss has a *genuine, developer-
acknowledged* bug that produces impossible temples, and the symptom is similar
enough to misattribute. The tell was that the broken temple had been generated
locally seconds earlier and carried zero phantoms — the real bugged temples are
notable precisely because they accumulate phantoms from everyone who fails them.
A freshly minted empty temple could not be one of those.

Regression tests: `tests/test_abyss_routes.py::test_settings_index_tracks_the_area`
and `::test_adventure_settings_index_is_untouched`.

### Caveat

That the settings collection governs key placement is inferred from the symbols
above, not from decompiled generator code. What is *measured* is the mapping in
the table — that part is certain.


---

## 1l. Unplayable temples, and how an offline server routes around them

Phantom Abyss can generate a temple that cannot be finished. This is not
speculation -- the developers have said so, and the failure mode is visible in
the archive: a temple with an unusually large number of phantoms and no
completions is a temple everybody failed.

Once the service is gone there is no patch coming, so the offline server treats
it as a serving problem rather than a generation problem.

### Two kinds, needing different remedies

| | generated | archived |
|---|---|---|
| where the layout lives | built by the client from a seed | fixed blob from the CDN |
| what identifies it | the generation fingerprint | dungeon id + floor |
| remedy | reroll the seed | skip it in the rotation |
| content lost | none -- the slot refills | that one temple |

### Why the fingerprint is not the seed

The client builds a temple from the seed *and* the shape it is asked for. The
same seed at a different area, floor shape or settings index produces a
completely different temple -- §1k is the direct evidence, where changing only
`dungeonSettingsIndex` changed whether a door had a key.

So the blacklist key covers every generation input:

```
gameMode, areaID, dungeonSettingsIndex, dungeonLayoutType,
dungeonFloorType, dungeonFloorTotalCount, difficultyRating, dungeonSeed
```

sha256 of those, truncated to 16 hex. Keying on the seed alone would withhold
temples that are perfectly playable, which for a preservation project is the
worse error: it destroys content to avoid a problem that is not there.

### Rerolling

The seed derives from a stable key, so a reroll appends a counter:
`stable_seed(key)`, then `stable_seed(key, 1)`, `stable_seed(key, 2)`, and so
on until the fingerprint is clean. The counter is persisted with the temple
record -- without that, the replacement temple would change under a player
between sessions.

The floor shape is computed *before* the reroll and left alone by it. A reroll
that also changed the shape would risk the empty-temple black screen from §1e,
trading a dead temple for a worse one.

### Trust

A report is a claim. Some fraction of "impossible" temples are players who have
not found the route yet, and a blacklist that believed every report would
quietly delete good content for everyone downstream.

* **Local** -- blocked immediately. That player hit it; re-serving it to them
  helps nobody, and the cost of being wrong is one rerolled temple.
* **Imported** -- needs `IMPORT_THRESHOLD` (2) independent reporters, or an
  explicit `confirmed`.
* **Cleared** -- never blocks, and no import can un-clear it.

### Privacy

Reports are meant to be pooled, and a SteamID64 identifies a real account.
Reporters are stored as a salted hash, the salt is per install, and `export`
drops the reporter list entirely, keeping only a count.

The tradeoff is that per-install salting breaks cross-install deduplication:
one person reporting from two machines counts as two reporters when those
exports are merged. That over-counts agreement slightly, which is the safer
direction to be wrong in than publishing something that can be walked back to
an account.

Tests: `tests/test_blacklist.py`.


---

## 1m. A floor the client generates is described, not specified

`dungeonLayoutType` and `dungeonFloorType` do **not** tell the client what kind
of floor to build. They describe the layout the server is sending. When the
server sends no layout, it states no shape either, and the client decides.

Measured across 2,207 captured `/GetDungeon` responses, zero exceptions:

| server sends | `dungeonLayoutType` | `dungeonFloorType` | `dungeonFloorTotalCount` |
|---|---|---|---|
| a CDN layout | the layout's real value | the layout's real value | real |
| **no layout** | **0** | **0** | 0 for a temple that does not exist yet, the real count once it does |

The floor count behaves differently from the other two: the server plainly
knows it, it just does not state it until the temple exists. So the wire value
and the server's own bookkeeping have to be kept separate.

### The bug this caused

We inferred the shape rules from responses that carried a layout -- where the
values describe the layout being handed over -- and then sent those same values
on responses where the client was doing the generating. The client took them as
instructions.

The visible symptom was an Abyss run that could not be finished: on the last
floor of the Rift an exit door stood locked, and pausing near it surfaced an
"Insert Key" prompt that did nothing.

Three things about that symptom were diagnostic, and all three were missed at
first:

* It reproduced on **every** offline run, not occasionally. A genuinely bugged
  temple would be random.
* It had **never** been seen online, by this player or anyone else.
* Getting the game to ask for a key at all was odd -- which is what prompted
  playing the same content against the live service and diffing it.

Playing a real Abyss route to its final floor settled it: the real final floor
of that route **has no door at all**. The door was ours. Telling the client the
floor was `layoutType 1, floorType 3` made the generator build the kind of floor
those values name, complete with a locked door, while the key placement that
belongs with it never followed.

Nothing could recover from it at runtime, because **no server call happens at a
door**. The capture of the real final floor contains exactly two exchanges from
the moment it loaded: the `/GetDungeon` that produced it, and the client
uploading the layout it built. The door is entirely client-side.

### Why the earlier black-screen fix was right by accident

§1e records a floor served as `layoutType 2 / floorType 3` making the generator
emit `numWings 0` and hang the client. Serving `(3, 1)` fixed it, and the
conclusion drawn was that floors have a correct shape that must be supplied.

The real conclusion is that supplying a shape at all was the error. `(3, 1)`
happened to be a coherent instruction, so the generator produced something
playable. `(0, 0)` -- the answer the real server gives -- is better still,
because the client then applies its own rules for that floor at that depth in
that mode.

### Consequences elsewhere

`dungeonFloorTotalCount` had to be dropped from the blacklist fingerprint in
§1l. The server reports 0 for a temple it has just minted and the real count on
every later request, so a temple reported at mint time and re-requested a moment
later would have fingerprinted as two different temples, and the report would
have silently never taken effect.

Tests: `test_generated_temples.py::test_a_floor_the_client_builds_is_given_no_shape`,
`::test_a_new_temple_states_no_floor_count`,
`::test_archived_floors_keep_their_real_shape`,
`test_abyss_routes.py::test_a_generated_floor_describes_no_shape`.

### Fidelity

Replaying a complete real Abyss route (Master, 8 floors, all four areas)
through the offline backend afterwards: 7 of 8 floors identical on every
non-identifier field, including the final floor. The remaining difference is
`relicIDs` / `relicCollectorIDs` / `relicCollectorNames` arriving as `[]`
rather than `null` on some first-floor responses. The trigger is unknown and
the two are equivalent to Unreal's deserialiser, which treats a null array and
an empty array alike.


---

## 1n. Where Abyss phantoms actually live

Abyss is the mode an offline archive is most likely to be short of, and the
obvious way to harvest it -- start runs and keep what comes back -- cannot work.
Measured across every captured mint request:

| response to `dungeonId: 0` | temples | phantoms | avg |
|---|---|---|---|
| carries a CDN layout | 21 | 366 | 17.4 |
| `layoutDownloadURL` empty | 32 | **0** | **0.0** |

No exceptions. An empty layout URL means no layout exists, which means nobody
has played the temple, which means it has no phantoms. Abyss retires a temple
once somebody beats it, so a fresh Abyss route can only ever hand out something
uncleared -- in practice, something brand new and empty. Three live Abyss runs
played end to end produced eight temples and zero phantoms between them.

`playerCount` tracks the phantom count closely (178 players -> 44-50 returned,
against the ~50 cap), so either field identifies a worthwhile temple.

### Shared routes are the source

`/GetDungeonShareList` returns a player's shared routes, and every one observed
is `gameMode 0`. Each route embeds a `dungeons` array carrying `dungeonID`,
`numFloors` and **`numGhosts`** -- so the phantom count of every temple behind a
share code is known before spending a single request.

105 shared routes on file named 221 distinct Abyss temples advertising 11,875
phantoms, of which 10 were archived.

Harvesting them returned 4,373 phantoms across 162 floors from 55 routes; the
other 50 codes no longer resolve. The gap between advertised and retrieved is
the ~50-per-floor cap, not a fault.

### Two bugs this exposed

**Dead codes aborted the harvest.** Roughly half of share codes return 500 --
documented in `shares.py` from the start -- but a 500 counted toward
`max_failures`, and four consecutive failures stopped the run. The first four
codes on file are dead, so the harvester killed itself before reaching a working
one, every time. Only `FATAL_STATUSES` (network, 401, 403, 429) count now.

**A share code opens the whole route, not floor 0.** The docstring claimed
otherwise. That claim came from a sample taken while the harvester was aborting
early and had never reached a resolving code; 55 live routes averaged three
floors each.

The lesson worth keeping: an early measurement taken through a broken code path
is not a measurement. Both this and the floor-shape error in 1m came from
generalising a rule off a sample that could not have shown the alternative.


---

## 1o. Why temples at the same depth feel like reskins

A player running the same area repeatedly reports that the temples "look very
similar", and the layouts bear that out without being duplicates.

Each decoded layout carries two fields that explain it:

* **`roomsHash`** -- the pool of rooms the generator may draw from
* **`randomCurrent`** -- the RNG state the seed produced, which chooses the
  arrangement

Three temples served back to back at area 3, Adventure, curseLevel 1000:

| dungeon | rooms | `roomsHash` | `randomCurrent` |
|---|---|---|---|
| 701978 | 19 | 504573130 | 176906194 |
| 701430 | 18 | 504573130 | -269327915 |
| 701992 | 19 | 504573130 | -286035869 |

Same pool, different arrangement -- and comparing the rooms directly, **zero**
are identical between any pair. Same building blocks, different assembly.

### What fixes the pool

Across 700 archived layouts, grouped by (areaID, gameMode, dungeonFloorType):

| area | mode | floorType | layouts | distinct `roomsHash` |
|---|---|---|---|---|
| 1 | Adventure | 1 | 41 | 1 |
| 2 | Adventure | 1 | 76 | 1 |
| 3 | Adventure | 1 | 77 | 1 |
| 4 | Adventure | 3 | 41 | 1 |

One pool per combination, across dozens of temples each. The whole sample holds
only 38 distinct pools.

Notably the **challenge level does not affect it**: those 77 area-3 layouts span
many different `curseLevel` values and still share a single pool. A player
comparing runs at one challenge level cannot separate that variable from the
area, which is what makes the "same challenge generates similarly" reading a
natural but incorrect one.

Abyss shows 3-6 pools per group rather than 1, consistent with the settings
collection of 1k selecting among them.

### Why it matters for archiving

Temples at a given depth are structurally interchangeable, so breadth of
*coverage* matters less than breadth of *phantoms*. A drained pool yields
newly minted empties (1n), and those add nothing an existing temple at the same
depth does not already provide.


---

## 1p. An Abyss route does not have a uniform floor count

Floors per area vary *within* a route. The difficulty screen advertises a total
per tier, and one of them cannot be produced by any single per-area number:

| difficulty | per area | total | advertised |
|---|---|---|---|
| Master (0) | 2, 2, 2, 2 | 8 | 8 |
| Insane (1) | 2, 2, 3, 3 | 10 | 10 |
| Nightmare (2) | 3, 3, 3, 3 | 12 | 12 |
| Unchained (3) | 3, 3, 3, 4 | 13 | 13 |

Master and Nightmare divide evenly by four, so a model storing one count per
difficulty served them correctly and hid the flaw. It made Unchained 16 floors
and Insane 12.

Unchained was settled by playing one: areas 1, 2 and 3 each reported
`dungeonFloorTotalCount: 3`, and the fourth follows from the advertised 13.

All four tiers are settled. Insane was the last open one, and captured traffic
reports 2, 2, 3, 3 across its four areas -- matching both the advertised total
and the route map the game draws before a run.

Unchained's Rift is the one figure no capture contains, but it is not a guess:
three areas measured at 3 floors each account for 9 of an advertised 13, so the
fourth is 4 and cannot be otherwise. It rests on the same menu total that every
other tier was checked against, and would only be wrong if that total were.

Implemented as `ABYSS_FLOORS_BY_AREA` with the `abyss_floors(difficulty, area)`
helper, since the count is now a function of both.

### An aside on floor counts and fresh temples

The Unchained capture reports a real count on the very first request, which
looks like it contradicts 1m -- until you notice the temples were existing ones
carrying 7-11 phantoms, not fresh mints. A count of 0 marks a temple that does
not exist yet, not a mode that withholds it.
