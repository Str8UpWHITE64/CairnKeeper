"""Persistent player progression for a self-hosted server.

Where progression actually lives
--------------------------------
The server is mostly a store, not an authority. Captured traffic shows the
client reporting its own totals:

* ``/SubmitRun`` sends ``currency`` (essence + the four dungeon-key counts),
  the full ``playerStats`` block and the run outcome.
* ``/PurchaseUpgrade`` sends what to buy and what to spend
  (``essences``, ``keys: [{keyIndex, amount}]``); the response echoes the
  updated ``currency`` and ``upgrades.upgradeLevels``.
* ``/VerifyUserID`` returns the whole profile at login.

So offline progression does not require reverse-engineering reward formulas.
Accept what the client reports, apply purchases, and hand the profile back at
login. That is what the real service appears to do.

A profile is seeded from a captured ``/VerifyUserID`` response when one exists,
so an existing player keeps their whips, upgrades and relics. A fresh server
starts from sensible defaults instead.
"""
from __future__ import annotations

import hashlib
import json
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Fields carried in a profile and returned at login.
PROFILE_FIELDS = (
    "currency",
    "upgrades",
    "permanentPurchases",
    "playerStats",
    "reputation",
    "numSandbags",
    "temporaryPowerIndex",
    "currentUsername",
    "victoryRoutes",
    "activeClassicRoutes",
    "existingUser",
    "health",
)

# Tracked in the profile but deliberately NOT returned at login: the live
# server has no such field. It records relics inside
# victoryRoutes[].dungeons[].relicIDs. Sending an extra key would be harmless
# to the client but is a divergence, and the point here is fidelity.
INTERNAL_FIELDS = ("relics",)

# Four key tiers, matching dungeonKeys in captured payloads.
KEY_TIERS = 4
# upgradeLevels is a fixed-width array in every captured response.
UPGRADE_SLOTS = 25

# Captured verbatim from a brand new account's /VerifyUserID response
# (userID 404886, 2026-08-26). Notably the real server sends `dungeonKeys: null`
# rather than four zeroes, and `existingUser: false` -- both of which an
# invented default got wrong.
DEFAULTS: dict[str, Any] = {
    "currency": {"essence": 0, "dungeonKeys": None},
    "upgrades": {"upgradeLevels": [0] * UPGRADE_SLOTS},
    "permanentPurchases": [],
    "playerStats": None,
    "reputation": 0,
    "numSandbags": 0,
    "temporaryPowerIndex": -1,
    "currentUsername": "Explorer",
    "victoryRoutes": [],
    "activeClassicRoutes": [],
    "existingUser": False,
    "health": {"baseHealth": 3, "baseDailyHealth": 3},
    # Internal only -- see INTERNAL_FIELDS.
    "relics": [],
}


# The type the real server used for every player stat, taken from 22 captured
# logins with no field it was inconsistent about.
#
# The client and the server do not agree on these. The client sends
# justBeatArea as a name -- "Ruins" -- and the server answers with a number.
# Merging what the client sent and handing it straight back put a string
# where the client's own struct wanted an int, and a nested struct that will
# not convert takes the whole login down with it.
STAT_TYPES: dict[str, type] = {
    "BlessingsPurchased": int,
    "BlessingsUsed": int,
    "ChestsFound": int,
    "ChestsSpawned": int,
    "CoinDoorsOpened": int,
    "CoinsCollected": int,
    "CoinsSpent": int,
    "CollectedCoinsThisFloor": dict,
    "CollectedKeysThisFloor": dict,
    "CurrentArea": int,
    "CurrentDepth": int,
    "CurrentFloor": int,
    "CurrentPhantomsAll": int,
    "DeepestAreaReached": int,
    "DeepestDepthReached": int,
    "FloorTimers": list,
    "FurthestDropToRoll": float,
    "HighestAdventureHeatCompletedPerWhip": dict,
    "HighestJump": float,
    "JustBeatArea": int,
    "JustBlessedWhip": str,
    "JustClaimedRelic": str,
    "JustClaimedSandbag": bool,
    "JustCollectedCoins": int,
    "JustCollectedKeys": int,
    "JustFacedGuardian": str,
    "JustFreedSouls": bool,
    "JustGotWhip": str,
    "JustGotWhipSkin": str,
    "JustHadBlessings": list,
    "JustKilledBy": str,
    "JustSeenDialogues": list,
    "JustSpentCoins": int,
    "JustUsedBlessings": list,
    "KeysCollected": int,
    "KeysTraded": int,
    "LongestJump": float,
    "LongestSlide": float,
    "NumTimesDiedBy": dict,
    "NumTimesEnteredTempleWithWhip": dict,
    "NumTimesHadBlessing": dict,
    "NumTimesPurchasedTemporaryPower": dict,
    "NumTimesRunWithPlayer": dict,
    "NumTimesUsedBlessing": dict,
    "PhantomsFreed": int,
    "ScaledFloorTimers": list,
    "TemporaryPowersPurchased": int,
    "TimesTalkedToUna": int,
    "TimesTalkedToUnaThisRun": int,
    "Timestamp": str,
    "TopSpeed": float,
    "TotalDistanceSlid": float,
    "TotalDistanceTravelled": float,
    "TotalNumberOfDropRolls": int,
    "TotalNumberOfJumps": int,
    "TotalSandbagsCollected": int,
    "TotalTimesDashed": int,
    "TotalTimesWhipUsed": int,
}


# The keys inside a floor timer, which the client spells one way and the server
# another -- the same disagreement as the top level, one layer down, and missed
# because normalising stopped at the outside.
TIMER_KEYS = {"areaindex": "AreaIndex", "floorindex": "FloorIndex",
              "floortime": "FloorTime"}


def _as_timers(rows: Any) -> list:
    """Floor timers with the keys spelled the way the server spelled them."""
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append({TIMER_KEYS.get(str(k).lower(), k): v for k, v in row.items()})
    return out


def as_server_stats(stats: "dict[str, Any] | None") -> "dict[str, Any] | None":
    """Player stats in the types the real server answered with.

    Anything it never sent is dropped, and anything of the wrong type is
    replaced by an empty value of the right one rather than guessed at. A
    number that arrived as a name is not recoverable here, and inventing one
    would be worse than saying nothing.
    """
    if not isinstance(stats, dict):
        return stats
    empty = {int: 0, float: 0.0, str: "", bool: False, list: [], dict: {}}
    out: dict[str, Any] = {}
    for name, want in STAT_TYPES.items():
        value = stats.get(name)
        if want is float and isinstance(value, int) and not isinstance(value, bool):
            out[name] = float(value)
        elif isinstance(value, want) and not (want is int and isinstance(value, bool)):
            out[name] = value
        else:
            out[name] = empty[want]
    for timers in ("FloorTimers", "ScaledFloorTimers"):
        out[timers] = _as_timers(out.get(timers))
    return out


PURCHASE_OK = 0

# Challenge reward tiers, read off the in-game challenge screen. A challenge
# completed at or above these levels awards the corresponding cosmetic; the
# base completion awards the relic and the whip itself.
#
# Nothing server-side records which cosmetics a player owns -- all 58 stats the
# server returns carry no such field, and a Gold Shine was granted in-game with
# no server traffic at all. They are derived from the per-whip challenge level,
# so preserving that stat preserves the cosmetics with it.
#
# Live corroboration: Bamboo at 530 has the skin but no Gold Shine, Scales at
# 670 has both, and a Lightning run completed at 1000 granted both. That
# brackets the Gold Shine tier within (530, 670] -- consistent with 600, though
# only the screen states it exactly.
WHIP_SKIN_THRESHOLD = 400
GOLD_SHINE_THRESHOLD = 600

# Upgrade id -> index in `upgradeLevels`, recovered from the EPlayerUpgradeType
# enum in the shipping binary. Unreal emits enum members in declaration order,
# and that order is the array index.
#
# Two independent checks confirm it:
#   * captured /PurchaseUpgrade responses change exactly the slot this predicts
#     (PU_HEALTH 0, PU_WHIP_SPEED 3, PU_POWERS_TIER 9)
#   * PU_COUNT_OF_UPGRADE_TYPES lands on 25, exactly the width of every
#     upgradeLevels array seen in captured traffic
#
# PU_NONE (-1) and the PU_DAMAGE_REDUCTION_* range markers (26, 27) sit outside
# the array and are excluded.
KNOWN_UPGRADE_SLOTS: dict[str, int] = {
    "PU_HEALTH": 0,
    "PU_COINS": 1,
    "PU_WHIP_LENGTH": 2,
    "PU_WHIP_SPEED": 3,
    "PU_DODGE": 4,
    "PU_1_RUN_POWER_OPTIONS": 5,
    "PU_DASH_SPEED": 6,
    "PU_DASH_COOLDOWN": 7,
    "PU_KEY_EXCHANGE_OPTIONS": 8,
    "PU_POWERS_TIER": 9,
    "PU_CURSES_TIER": 10,
    "PU_PENDULUM_TRAPS": 11,
    "PU_DART_TRAPS": 12,
    "PU_BLADE_TRAPS": 13,
    "PU_SPIKE_TRAPS": 14,
    "PU_MINE_TRAPS": 15,
    "PU_BOULDER_TRAPS": 16,
    "PU_CRUSHER_TRAPS": 17,
    "PU_DEFENDER_TRAPS": 18,
    "PU_GAS_TRAPS": 19,
    "PU_DEVOURING_RAGE": 20,
    "PU_EYE_OF_AGONY": 21,
    "PU_MASKED_DEFILER": 22,
    "PU_PIT_DAMAGE": 23,
    "PU_LANDING_DAMAGE": 24,
}


class ProfileStore:
    """One profile per player, so a server can host more than one person.

    Keyed by the platform id the client sends (``playerId`` -- a SteamID64).
    A captured login can seed exactly one profile: whichever player it actually
    belonged to. Everyone else starts fresh, which is what a new player should
    get.
    """

    def __init__(
        self,
        directory: Path,
        *,
        seed: dict[str, Any] | None = None,
        seed_owner: str | None = None,
    ):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.seed = seed
        self._lock = threading.Lock()
        self._cache: dict[str, Profile] = {}
        # A captured login belongs to exactly one account. When the caller
        # cannot say which -- no credentials on disk -- the first player to
        # appear claims it and is recorded here, so the next account to join
        # starts fresh instead of inheriting a stranger's relics, reputation
        # and route history. Getting this wrong is invisible to the host and
        # obvious to their friends.
        self._owner_path = self.dir / "seed_owner.txt"
        self.seed_owner = str(seed_owner) if seed_owner else self._recorded_owner()

    def _recorded_owner(self) -> str | None:
        try:
            recorded = self._owner_path.read_text("utf-8").strip()
        except OSError:
            return None
        return recorded or None

    def _claim_seed(self, key: str) -> None:
        """Record who the captured profile belongs to, once."""
        self.seed_owner = key
        try:
            self._owner_path.write_text(key, encoding="utf-8")
        except OSError:
            # Not fatal: the claim simply will not survive a restart.
            pass

    @staticmethod
    def _safe(player_id: Any) -> str:
        text = str(player_id or "").strip()
        keep = "".join(c for c in text if c.isalnum() or c in "-_")
        return keep or "default"

    def for_player(self, player_id: Any, name: Any = None) -> Profile:
        """The profile for this platform id, seeded only if it is the owner's.

        `name` is the display name the client sent at login. It is how the
        captured profile finds its way home on a server other people connect
        to: the capture carries the account name it belongs to, so the account
        with that name inherits it and nobody else does.

        This used to hand the capture to whoever connected first, on the
        reasoning that a solo player without credentials on disk should still
        get their own save. On a machine with one player that is right. On a
        server hosted for other people it means the first stranger through the
        door is handed the host's whips, relics, reputation and route history --
        and the host cannot see it happen.
        """
        key = self._safe(player_id)
        with self._lock:
            profile = self._cache.get(key)
            if profile is None:
                existing = (self.dir / f"{key}.json").exists()
                if (self.seed_owner is None and self.seed and not existing
                        and self._is_seed_owner(name)):
                    self._claim_seed(key)
                seed = self.seed if key == self.seed_owner else None
                profile = Profile(self.dir / f"{key}.json", seed=seed)
                self._cache[key] = profile
            return profile

    def _is_seed_owner(self, name: Any) -> bool:
        """Whether a login by this name is the one the capture came from."""
        wanted = str((self.seed or {}).get("currentUsername") or "").strip()
        return bool(wanted) and str(name or "").strip() == wanted

    def known_players(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json"))


class Profile:
    """The player's saved progression, persisted as JSON."""

    def __init__(self, path: Path, seed: dict[str, Any] | None = None):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.data = self._load(seed)

    # ------------------------------------------------------------- storage

    def _load(self, seed: dict[str, Any] | None) -> dict[str, Any]:
        if self.path.exists():
            try:
                stored = json.loads(self.path.read_text("utf-8"))
                if isinstance(stored, dict):
                    return {**deepcopy(DEFAULTS), **stored}
            except (ValueError, OSError):
                pass

        data = deepcopy(DEFAULTS)
        if seed:
            # Carry over a real captured profile so an existing player keeps
            # everything they earned online.
            for field in PROFILE_FIELDS:
                if seed.get(field) is not None:
                    data[field] = deepcopy(seed[field])
            # The account's real in-game id comes with it. Without this a
            # returning player is handed a fresh one, and stops matching the
            # phantoms they already left in the archive under the old one.
            if isinstance(seed.get("userID"), int) and seed["userID"] > 0:
                data["userID"] = seed["userID"]
            data["seededFromCapture"] = True
        self._write(data)
        return data

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def save(self) -> None:
        with self._lock:
            self._write(self.data)

    # -------------------------------------------------------------- access

    def user_id(self, platform_id: Any = None) -> int:
        """The player's in-game id -- the number a phantom is named after.

        A seeded profile already has the real one. Everyone else needs one that
        is theirs alone and does not move: two players sharing an id are two
        phantoms sharing an actor name, which crashes the client on spawn. Kept
        above the range the live service reached so a made-up id is
        recognisable as one.
        """
        with self._lock:
            existing = self.data.get("userID")
            if isinstance(existing, int) and existing > 0:
                return existing
            source = str(platform_id or self.path.stem)
            digest = hashlib.sha256(source.encode("utf-8")).digest()
            allocated = 800_000_000 + int.from_bytes(digest[:5], "big") % 90_000_000
            self.data["userID"] = allocated
            self._write(self.data)
            return allocated

    def set_name(self, name: Any) -> None:
        """Remember what this player calls themselves.

        The client sends no name when it uploads a run, so a phantom is labeled
        from the profile. Without this every stranger's ghost would carry
        whatever name the profile happened to be created with.
        """
        text = str(name or "").strip()
        if not text:
            return
        with self._lock:
            if self.data.get("currentUsername") != text:
                self.data["currentUsername"] = text
                self._write(self.data)

    def snapshot(self) -> dict[str, Any]:
        """The profile fields to merge into a /VerifyUserID response."""
        with self._lock:
            return {f: deepcopy(self.data.get(f)) for f in PROFILE_FIELDS}

    # ------------------------------------------------------------ mutation

    def apply_run(self, req: dict[str, Any]) -> dict[str, Any] | None:
        """Bank a run's results. Returns the new totals, or None mid-route.

        How the real server behaves, established by tracing currency
        chronologically across every endpoint in a captured session:

        * ``currency`` in the request is what was earned **on this route so
          far**, accumulating across floors -- not the player's total.
        * It is banked only when the route ends: ``success`` 0 (died) or 2
          (completed). A mid-route floor (``success`` 1) returns
          ``currency: null`` and changes nothing.
        * ``dungeonKeys`` are **added** to the stored total. Confirmed three
          times, e.g. ``[281,13,11,0] + [2,2,0,1] = [283,15,11,1]``.
        * ``essence`` is **not** changed by a run. It sat at 1167 across runs
          reporting 9 and 2 essence. Treating it like keys would inflate it.

        Overwriting the total with the request -- which this used to do -- would
        have reduced a player's keys to a single run's earnings.
        """
        success = req.get("success")
        route_ended = success != 1

        with self._lock:
            earned = _clean_currency(req.get("currency"))
            if route_ended:
                total = _clean_currency(self.data.get("currency"))
                total["dungeonKeys"] = [
                    a + b for a, b in zip(total["dungeonKeys"], earned["dungeonKeys"])
                ]
                # essence deliberately left alone; see docstring.
                self.data["currency"] = total

                relic = req.get("collectedRelicId")
                if isinstance(relic, str) and relic:
                    relics = list(self.data.get("relics") or [])
                    relics.append(
                        {
                            "relicID": relic,
                            "dungeonID": req.get("dungeonId") or req.get("dungeonID"),
                            "at": _now(),
                        }
                    )
                    self.data["relics"] = relics

            stats = req.get("playerStats")
            if isinstance(stats, dict) and stats:
                merged = dict(self.data.get("playerStats") or {})
                merged.update(_normalise_stats(stats))
                merged["Timestamp"] = _now()
                self.data["playerStats"] = merged

            # Reputation behaves exactly like dungeonKeys: the request carries
            # what the route earned, not the player's total, and it is banked
            # only when the route ends. Confirmed chronologically --
            # 5582 + (-82) = 5500 -- and every non-zero delta observed arrived
            # with success 0 or 2, never mid-route (0 of 16 mid-route
            # submissions carried one).
            #
            # Assigning the delta instead of adding it, which this used to do,
            # reduced a player's standing to whatever their last route earned.
            if route_ended:
                reputation = req.get("reputation")
                if isinstance(reputation, (int, float)) and reputation:
                    current = self.data.get("reputation")
                    current = int(current) if isinstance(current, (int, float)) else 0
                    self.data["reputation"] = max(0, current + int(reputation))

            self.data["existingUser"] = True
            self._write(self.data)
            return deepcopy(self.data["currency"]) if route_ended else None

    def purchase_upgrade(self, req: dict[str, Any]) -> dict[str, Any]:
        """Apply a `/PurchaseUpgrade`, deducting what the client offered."""
        with self._lock:
            currency = _clean_currency(self.data.get("currency"))
            essence = int(req.get("essences") or 0)
            spent_keys: list[dict[str, int]] = []

            currency["essence"] = max(0, currency["essence"] - essence)
            for entry in req.get("keys") or []:
                if not isinstance(entry, dict):
                    continue
                index = int(entry.get("keyIndex") or 0)
                amount = int(entry.get("amount") or 0)
                if 0 <= index < len(currency["dungeonKeys"]) and amount:
                    currency["dungeonKeys"][index] = max(
                        0, currency["dungeonKeys"][index] - amount
                    )
                    spent_keys.append({"keyIndex": index, "amount": amount})

            levels = list(
                (self.data.get("upgrades") or {}).get("upgradeLevels")
                or [0] * UPGRADE_SLOTS
            )
            slot = _upgrade_slot(req.get("upgradeId"), self.data)
            to_level = req.get("toLevel")
            if slot is not None and isinstance(to_level, int):
                while len(levels) <= slot:
                    levels.append(0)
                levels[slot] = to_level

            item = req.get("purchaseItemId") or req.get("purchaseItemID") or ""
            purchases = list(self.data.get("permanentPurchases") or [])
            if item and item not in purchases:
                purchases.append(item)

            self.data["currency"] = currency
            self.data["upgrades"] = {"upgradeLevels": levels}
            self.data["permanentPurchases"] = purchases
            self.data["existingUser"] = True
            # Remember which slot an upgrade id maps to, so repeat purchases of
            # the same upgrade land in the same place.
            if slot is not None and req.get("upgradeId"):
                slots = dict(self.data.get("upgradeSlots") or {})
                slots[str(req["upgradeId"])] = slot
                self.data["upgradeSlots"] = slots
            self._write(self.data)

            return {
                "result": PURCHASE_OK,
                "purchaseItemID": item,
                "userID": req.get("userId") or 0,
                "spentEssence": essence,
                # Always null here. All four captured /PurchaseUpgrade responses
                # returned null even when keys were spent -- unlike
                # /ConvertKeys, which does report them.
                "spentKeys": None,
                "currency": deepcopy(currency),
                "upgrades": {"upgradeLevels": list(levels)},
            }

    def spend_currency(self, req: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            currency = _clean_currency(self.data.get("currency"))
            currency["essence"] = max(
                0, currency["essence"] - int(req.get("essences") or 0)
            )
            for entry in req.get("keys") or []:
                if not isinstance(entry, dict):
                    continue
                index = int(entry.get("keyIndex") or 0)
                amount = int(entry.get("amount") or 0)
                if 0 <= index < len(currency["dungeonKeys"]):
                    currency["dungeonKeys"][index] = max(
                        0, currency["dungeonKeys"][index] - amount
                    )
            self.data["currency"] = currency
            self._write(self.data)
            return {"currency": deepcopy(currency), "result": PURCHASE_OK}

    def convert_keys(self, req: dict[str, Any]) -> dict[str, Any]:
        """Trade keys between tiers. Rates come from `globalValues`."""
        with self._lock:
            currency = _clean_currency(self.data.get("currency"))
            keys = currency["dungeonKeys"]
            source = int(req.get("fromKeyIndex") or req.get("sourceIndex") or 0)
            target = int(req.get("toKeyIndex") or req.get("targetIndex") or 0)
            amount = int(req.get("amount") or 0)
            rate = int(req.get("rate") or 1) or 1

            requested_spend = int(req.get("spendAmount") or 0)
            spent = 0
            received = 0
            if 0 <= source < len(keys) and 0 <= target < len(keys) and amount > 0:
                spent = min(keys[source], amount * rate)
                received = spent // rate if rate else 0
                keys[source] -= spent
                keys[target] += received

            currency["dungeonKeys"] = keys
            self.data["currency"] = currency
            self._write(self.data)
            return {
                "sourceKeyType": source,
                "destinationKeyType": target,
                "amountExchanged": spent,
                "amountReceived": received,
                "userID": req.get("userId") or 0,
                "purchaseItemID": req.get("purchaseId")
                or req.get("purchaseItemId")
                or f"ConvertKeysUpFrom{source}",
                "spentEssence": 0,
                "spentKeys": (
                    [{"keyIndex": source, "amount": spent or requested_spend}]
                    if (spent or requested_spend)
                    else None
                ),
                "result": PURCHASE_OK,
                "currency": deepcopy(currency),
            }

    def record_victory(self, route: dict[str, Any]) -> None:
        """Append a completed route to the player's history.

        This is the most important thing the profile stores. The server has no
        list of unlocked whips, no relic collection and no challenge-completion
        flags -- the client derives all of them from route history
        (``GetUniqueRelicsEarned``, ``GetUnlockedWhipByDungeonID``,
        ``IsAdventureRelicChallengeComplete``). Relics live in
        ``victoryRoutes[].dungeons[].relicIDs``, and the whip used lives in
        ``wageredWhipID``.

        So if this is not appended, a player's relics, whips and challenges
        silently stop accumulating even though everything else looks fine.
        """
        route_id = route.get("routeID")
        if not route_id:
            return
        with self._lock:
            routes = list(self.data.get("victoryRoutes") or [])
            for index, existing in enumerate(routes):
                if existing.get("routeID") == route_id:
                    routes[index] = _merge_route(existing, route)
                    break
            else:
                routes.append(route)
            self.data["victoryRoutes"] = routes
            self.data["existingUser"] = True
            self._write(self.data)

    def relics_earned(self) -> list[str]:
        """Distinct relics, derived from route history the way the client does."""
        found: list[str] = []
        for route in self.data.get("victoryRoutes") or []:
            for dungeon in route.get("dungeons") or []:
                for relic in dungeon.get("relicIDs") or []:
                    if relic and relic not in found:
                        found.append(relic)
        return sorted(found)

    def completed_dungeons(self) -> set[int]:
        """Temples this player has beaten.

        A player who has finished an archived temple has already seen its
        phantoms, so serving it again spends the archive on someone who has
        nothing left to find there. Everything they have beaten is recorded in
        the route history the client reads its collection from, so nothing
        extra needs storing to know this.
        """
        beaten: set[int] = set()
        for route in self.data.get("victoryRoutes") or []:
            if not isinstance(route, dict):
                continue
            for dungeon in route.get("dungeons") or []:
                if not isinstance(dungeon, dict):
                    continue
                identifier = dungeon.get("dungeonID")
                if isinstance(identifier, int) and identifier > 0:
                    beaten.add(identifier)
        return beaten

    def challenge_levels(self) -> dict[str, int]:
        """Highest challenge level beaten per whip.

        The in-game challenge screen calls this "Challenge Level"; the protocol
        calls it `curseLevel`, and a completed run at 250 was recorded as
        `curseLevel: 250`. Reward tiers observed on the challenge screen:
        base = relic + whip, 400 = whip skin, 600 = Gold Shine.
        """
        stats = self.data.get("playerStats") or {}
        levels = stats.get("HighestAdventureHeatCompletedPerWhip")
        return dict(levels) if isinstance(levels, dict) else {}

    def cosmetics_earned(self) -> dict[str, list[str]]:
        """Whip skins and Gold Shines implied by challenge levels."""
        out: dict[str, list[str]] = {}
        for whip, level in self.challenge_levels().items():
            earned = []
            if isinstance(level, (int, float)):
                if level >= WHIP_SKIN_THRESHOLD:
                    earned.append("whip skin")
                if level >= GOLD_SHINE_THRESHOLD:
                    earned.append("Gold Shine")
            if earned:
                out[whip] = earned
        return out

    def whips_used(self) -> list[str]:
        whips: list[str] = []
        for route in self.data.get("victoryRoutes") or []:
            whip = route.get("wageredWhipID")
            if whip and whip not in whips:
                whips.append(whip)
        return sorted(whips)


def _merge_route(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Fold a later floor of the same route into the stored entry.

    A route spans several dungeons; each submission reports one. Replacing
    wholesale would drop the earlier dungeons -- and with them their relics.
    """
    merged = dict(existing)
    merged.update({k: v for k, v in incoming.items() if v is not None})
    dungeons = list(existing.get("dungeons") or [])
    for dungeon in incoming.get("dungeons") or []:
        key = (dungeon.get("dungeonID"), dungeon.get("routeStage"))
        for index, known in enumerate(dungeons):
            if (known.get("dungeonID"), known.get("routeStage")) == key:
                combined = dict(known)
                combined.update({k: v for k, v in dungeon.items() if v is not None})
                relics = list(known.get("relicIDs") or [])
                for relic in dungeon.get("relicIDs") or []:
                    if relic not in relics:
                        relics.append(relic)
                combined["relicIDs"] = relics or None
                dungeons[index] = combined
                break
        else:
            dungeons.append(dungeon)
    merged["dungeons"] = dungeons
    return merged


def _normalise_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """Match the casing the real server stores player stats under.

    The client sends camelCase (`highestAdventureHeatCompletedPerWhip`); the
    server returns PascalCase (`HighestAdventureHeatCompletedPerWhip`). Merging
    the client's keys verbatim stores both spellings, and everything that reads
    a stat -- challenge levels, whip cosmetics -- looks at the server's, so
    progress made offline would land in a key nothing ever reads.

    Capitalising the first letter maps 58 of the 60 keys the client sends onto
    a key the server returns, and the server sends none the client does not.
    The two without a counterpart follow the same convention.
    """
    return {key[:1].upper() + key[1:]: value for key, value in stats.items()}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _clean_currency(value: Any) -> dict[str, Any]:
    """Always return the captured shape: an int and exactly four key counts."""
    essence = 0
    keys = [0] * KEY_TIERS
    if isinstance(value, dict):
        raw_essence = value.get("essence")
        if isinstance(raw_essence, (int, float)):
            essence = int(raw_essence)
        raw_keys = value.get("dungeonKeys")
        if isinstance(raw_keys, list):
            for i in range(min(KEY_TIERS, len(raw_keys))):
                if isinstance(raw_keys[i], (int, float)):
                    keys[i] = int(raw_keys[i])
    return {"essence": essence, "dungeonKeys": keys}


def _upgrade_slot(upgrade_id: Any, data: dict[str, Any]) -> int | None:
    """Map an upgrade id onto its index in `upgradeLevels`.

    The mapping is not in any captured payload, so it is learned: the first
    time an id is bought, it claims the next unused slot and that choice is
    remembered. Stable within a profile, which is what matters for progression.
    """
    if not upgrade_id:
        return None
    name = str(upgrade_id)
    if name in KNOWN_UPGRADE_SLOTS:
        return KNOWN_UPGRADE_SLOTS[name]
    learned = data.get("upgradeSlots") or {}
    if name in learned:
        return int(learned[name])
    # Claim the next slot no known or learned id already owns.
    used = set(KNOWN_UPGRADE_SLOTS.values()) | {int(v) for v in learned.values()}
    for slot in range(UPGRADE_SLOTS):
        if slot not in used:
            return slot
    return None
