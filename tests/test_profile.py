"""Progression must persist, so a self-hosted server feels like a real one."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.profile import KEY_TIERS, Profile, _clean_currency  # noqa: E402

# Shapes taken from real captured payloads.
CAPTURED_LOGIN = {
    "currency": {"essence": 1167, "dungeonKeys": [281, 13, 11, 1]},
    "upgrades": {"upgradeLevels": [4, 22, 4, 3, 21] + [0] * 20},
    "permanentPurchases": ["BigChestsDetails:Dungeon:141766:0"],
    "reputation": 5582,
    "currentUsername": "Tomb Raider",
}

PURCHASE = {
    "upgradeId": "PU_HEALTH",
    "toLevel": 4,
    "purchaseItemId": "UPGRADE_PU_HEALTH_4",
    "essences": 0,
    "keys": [{"keyIndex": 3, "amount": 3}],
    "userId": 62842,
}


def test_seeds_from_capture(tmp_path: Path) -> None:
    p = Profile(tmp_path / "profile.json", seed=CAPTURED_LOGIN)
    snap = p.snapshot()
    assert snap["currency"]["essence"] == 1167
    assert snap["reputation"] == 5582
    assert snap["currentUsername"] == "Tomb Raider"


def test_fresh_profile_matches_a_real_new_account(tmp_path: Path) -> None:
    """Captured from a brand new account (userID 404886).

    The real server sends `dungeonKeys: null`, not four zeroes, and
    `existingUser: false` -- both were wrong when invented.
    """
    snap = Profile(tmp_path / "profile.json").snapshot()
    assert snap["currency"] == {"essence": 0, "dungeonKeys": None}
    assert snap["upgrades"]["upgradeLevels"] == [0] * 25
    assert snap["existingUser"] is False
    assert snap["reputation"] == 0
    assert snap["permanentPurchases"] == []
    assert snap["playerStats"] is None
    assert snap["health"] == {"baseHealth": 3, "baseDailyHealth": 3}


def test_playing_marks_the_account_as_existing(tmp_path: Path) -> None:
    p = Profile(tmp_path / "profile.json")
    assert p.snapshot()["existingUser"] is False
    p.apply_run({"success": 2, "currency": {"essence": 5, "dungeonKeys": [1, 0, 0, 0]}})
    assert p.snapshot()["existingUser"] is True


def test_purchase_deducts_keys(tmp_path: Path) -> None:
    p = Profile(tmp_path / "profile.json", seed=CAPTURED_LOGIN)
    out = p.purchase_upgrade(PURCHASE)
    # 3 keys of tier 3 spent, from a starting count of 1 -> floors at 0.
    assert out["currency"]["dungeonKeys"][3] == 0
    # Captured /PurchaseUpgrade responses always report spentKeys as null,
    # even when keys were spent. /ConvertKeys is the one that lists them.
    assert out["spentKeys"] is None
    assert out["result"] == 0


def test_purchase_records_level_and_item(tmp_path: Path) -> None:
    p = Profile(tmp_path / "profile.json", seed=CAPTURED_LOGIN)
    out = p.purchase_upgrade(PURCHASE)
    assert 4 in out["upgrades"]["upgradeLevels"]
    assert "UPGRADE_PU_HEALTH_4" in p.snapshot()["permanentPurchases"]


def test_same_upgrade_reuses_its_slot(tmp_path: Path) -> None:
    p = Profile(tmp_path / "profile.json", seed=CAPTURED_LOGIN)
    first = p.purchase_upgrade(PURCHASE)["upgrades"]["upgradeLevels"]
    second = p.purchase_upgrade({**PURCHASE, "toLevel": 5})["upgrades"]["upgradeLevels"]
    changed = [i for i, (a, b) in enumerate(zip(first, second)) if a != b]
    assert len(changed) == 1, "a repeat purchase must not claim a new slot"
    assert second[changed[0]] == 5


def test_finished_run_adds_keys_to_the_total(tmp_path: Path) -> None:
    """The request carries the route's earnings; the server adds them.

    Confirmed against captured traffic:
    [281,13,11,0] + [2,2,0,1] = [283,15,11,1].
    """
    p = Profile(tmp_path / "profile.json", seed=CAPTURED_LOGIN)
    p.apply_run(
        {
            "success": 2,
            "currency": {"essence": 9, "dungeonKeys": [2, 2, 0, 1]},
            "playerStats": {"CoinsCollected": 99},
            "reputation": 6000,
        }
    )
    snap = p.snapshot()
    assert snap["currency"]["dungeonKeys"] == [283, 15, 11, 2]
    assert snap["playerStats"]["CoinsCollected"] == 99
    # Reputation accumulates like keys: the request carries what the route
    # earned, not the total. The captured login stood at 5582, so a route worth
    # 6000 lands at 11582. This previously asserted 6000 -- the delta replacing
    # the total -- which would wipe a player's standing on every run.
    assert snap["reputation"] == 11582


def test_essence_is_not_changed_by_a_run(tmp_path: Path) -> None:
    """Essence held at 1167 across runs reporting 9 and 2 essence."""
    p = Profile(tmp_path / "profile.json", seed=CAPTURED_LOGIN)
    p.apply_run({"success": 2, "currency": {"essence": 9, "dungeonKeys": [0, 0, 0, 0]}})
    assert p.snapshot()["currency"]["essence"] == 1167


def test_mid_route_floor_banks_nothing(tmp_path: Path) -> None:
    """success == 1 is a floor, not the end of a route."""
    p = Profile(tmp_path / "profile.json", seed=CAPTURED_LOGIN)
    banked = p.apply_run(
        {"success": 1, "currency": {"essence": 0, "dungeonKeys": [5, 5, 5, 5]}}
    )
    assert banked is None
    assert p.snapshot()["currency"]["dungeonKeys"] == [281, 13, 11, 1]


def test_death_still_banks_the_route(tmp_path: Path) -> None:
    p = Profile(tmp_path / "profile.json", seed=CAPTURED_LOGIN)
    banked = p.apply_run(
        {"success": 0, "currency": {"essence": 0, "dungeonKeys": [1, 0, 0, 0]}}
    )
    assert banked is not None
    assert p.snapshot()["currency"]["dungeonKeys"][0] == 282


def test_relic_is_recorded(tmp_path: Path) -> None:
    p = Profile(tmp_path / "profile.json", seed=CAPTURED_LOGIN)
    p.apply_run({"success": 2, "collectedRelicId": "SoldierDart", "dungeonId": 701203})
    relics = p.data["relics"]
    assert len(relics) == 1
    assert relics[0]["relicID"] == "SoldierDart"
    assert relics[0]["dungeonID"] == 701203


def test_empty_relic_is_not_recorded(tmp_path: Path) -> None:
    p = Profile(tmp_path / "profile.json", seed=CAPTURED_LOGIN)
    p.apply_run({"success": 2, "collectedRelicId": ""})
    assert p.data["relics"] == []


def test_stats_merge_rather_than_replace(tmp_path: Path) -> None:
    p = Profile(tmp_path / "profile.json", seed=CAPTURED_LOGIN)
    p.apply_run({"success": 2, "playerStats": {"A": 1}})
    p.apply_run({"success": 2, "playerStats": {"B": 2}})
    stats = p.snapshot()["playerStats"]
    assert stats["A"] == 1 and stats["B"] == 2


def test_persists_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    Profile(path, seed=CAPTURED_LOGIN).purchase_upgrade(PURCHASE)
    reopened = Profile(path)
    assert "UPGRADE_PU_HEALTH_4" in reopened.snapshot()["permanentPurchases"]
    assert reopened.snapshot()["currency"]["dungeonKeys"][3] == 0


def test_currency_never_goes_negative(tmp_path: Path) -> None:
    p = Profile(tmp_path / "profile.json")
    out = p.spend_currency({"essences": 9999, "keys": [{"keyIndex": 0, "amount": 50}]})
    assert out["currency"]["essence"] == 0
    assert all(k >= 0 for k in out["currency"]["dungeonKeys"])


def test_currency_shape_is_normalised() -> None:
    assert _clean_currency(None) == {"essence": 0, "dungeonKeys": [0] * KEY_TIERS}
    assert _clean_currency({"essence": 5}) == {
        "essence": 5,
        "dungeonKeys": [0] * KEY_TIERS,
    }
    # Overlong key arrays are truncated to the captured width.
    assert len(_clean_currency({"dungeonKeys": [1] * 9})["dungeonKeys"]) == KEY_TIERS


def test_profile_file_is_readable_json(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    Profile(path, seed=CAPTURED_LOGIN)
    assert json.loads(path.read_text("utf-8"))["reputation"] == 5582


def test_known_upgrade_slots_match_captured_traffic(tmp_path: Path) -> None:
    """Slots recovered by diffing real /PurchaseUpgrade responses."""
    from phantom_offline.profile import KNOWN_UPGRADE_SLOTS

    p = Profile(tmp_path / "profile.json", seed=CAPTURED_LOGIN)
    out = p.purchase_upgrade({**PURCHASE, "upgradeId": "PU_WHIP_SPEED", "toLevel": 4})
    assert out["upgrades"]["upgradeLevels"][KNOWN_UPGRADE_SLOTS["PU_WHIP_SPEED"]] == 4

    out = p.purchase_upgrade({**PURCHASE, "upgradeId": "PU_POWERS_TIER", "toLevel": 3})
    assert out["upgrades"]["upgradeLevels"][KNOWN_UPGRADE_SLOTS["PU_POWERS_TIER"]] == 3


def test_unknown_upgrade_never_corrupts_a_real_slot(tmp_path: Path) -> None:
    """All 25 slots are accounted for, so an unrecognised id claims none.

    Silently overwriting a real upgrade would be worse than ignoring the
    purchase: the player would see an unrelated stat change.
    """
    p = Profile(tmp_path / "profile.json")
    before = list(p.snapshot()["upgrades"]["upgradeLevels"])
    out = p.purchase_upgrade({**PURCHASE, "upgradeId": "PU_MYSTERY", "toLevel": 7})
    assert out["upgrades"]["upgradeLevels"] == before
    # The spend and the purchase record still happen.
    assert "UPGRADE_PU_HEALTH_4" in p.snapshot()["permanentPurchases"]


def test_upgrade_mapping_covers_the_whole_array() -> None:
    """25 upgrades, 25 slots, contiguous - matches PU_COUNT_OF_UPGRADE_TYPES."""
    from phantom_offline.profile import KNOWN_UPGRADE_SLOTS, UPGRADE_SLOTS

    assert len(KNOWN_UPGRADE_SLOTS) == UPGRADE_SLOTS
    assert sorted(KNOWN_UPGRADE_SLOTS.values()) == list(range(UPGRADE_SLOTS))


def test_every_upgrade_lands_in_its_own_slot(tmp_path: Path) -> None:
    from phantom_offline.profile import KNOWN_UPGRADE_SLOTS

    p = Profile(tmp_path / "profile.json")
    for name, slot in KNOWN_UPGRADE_SLOTS.items():
        out = p.purchase_upgrade(
            {"upgradeId": name, "toLevel": 1, "purchaseItemId": f"{name}_1",
             "essences": 0, "keys": []}
        )
        assert out["upgrades"]["upgradeLevels"][slot] == 1, f"{name} -> slot {slot}"


def test_game_mode_enum_matches_captured_traffic() -> None:
    """CLASSIC->0, ADVENTURE->1, DAILY->3. PRACTISE occupies 2.

    A naive reading of the mode list puts DAILY at 2, which is wrong: 99
    Adventure, 18 Classic and 3 Daily exchanges pin these values.
    """
    from phantom_offline.backend import GAME_MODES

    assert GAME_MODES["DGM_CLASSIC"] == 0
    assert GAME_MODES["DGM_ADVENTURE"] == 1
    assert GAME_MODES["DGM_PRACTISE"] == 2
    assert GAME_MODES["DGM_DAILY"] == 3


def test_reputation_accumulates(tmp_path: Path) -> None:
    """The request carries what the route earned, not the running total.

    Confirmed chronologically against live traffic: a reported total of 5582
    followed by a delta of -82 produced 5500.
    """
    p = Profile(tmp_path / "profile.json")
    p.data["reputation"] = 5582
    p._write(p.data)

    p.apply_run({"success": 0, "reputation": -82})
    assert p.snapshot()["reputation"] == 5500

    p.apply_run({"success": 2, "reputation": 351})
    assert p.snapshot()["reputation"] == 5851


def test_reputation_is_not_banked_mid_route(tmp_path: Path) -> None:
    """Every non-zero delta observed arrived at route end, never mid-route.

    No mid-route submission in the captures carried one, and none of the 16
    mid-route responses reported a non-zero updatedReputation.
    """
    p = Profile(tmp_path / "profile.json")
    p.data["reputation"] = 1000
    p._write(p.data)
    p.apply_run({"success": 1, "reputation": 250})
    assert p.snapshot()["reputation"] == 1000


def test_reputation_never_goes_negative(tmp_path: Path) -> None:
    p = Profile(tmp_path / "profile.json")
    p.data["reputation"] = 10
    p._write(p.data)
    p.apply_run({"success": 0, "reputation": -500})
    assert p.snapshot()["reputation"] == 0


def test_player_stats_take_the_servers_casing(tmp_path: Path) -> None:
    """The client sends camelCase; the real server stores PascalCase.

    Merging the client's spelling verbatim stored both, and everything that
    reads a stat looks at the server's -- so a challenge beaten offline landed
    in a key nothing ever read, and the player's level never moved.
    """
    p = Profile(tmp_path / "profile.json", seed=CAPTURED_LOGIN)
    p.apply_run(
        {
            "success": 2,
            "playerStats": {
                "highestAdventureHeatCompletedPerWhip": {"Bamboo": 530},
                "totalTimesWhipUsed": 12,
            },
        }
    )
    stats = p.snapshot()["playerStats"]
    assert stats["HighestAdventureHeatCompletedPerWhip"] == {"Bamboo": 530}
    assert stats["TotalTimesWhipUsed"] == 12
    assert "highestAdventureHeatCompletedPerWhip" not in stats


def test_a_challenge_beaten_offline_raises_the_level(tmp_path: Path) -> None:
    p = Profile(tmp_path / "profile.json", seed=CAPTURED_LOGIN)
    assert p.challenge_levels() == {}
    p.apply_run(
        {
            "success": 2,
            "playerStats": {
                "highestAdventureHeatCompletedPerWhip": {"Bamboo": 430, "Scales": 610}
            },
        }
    )
    assert p.challenge_levels() == {"Bamboo": 430, "Scales": 610}
    # 400 earns a whip skin, 600 a Gold Shine.
    assert p.cosmetics_earned() == {
        "Bamboo": ["whip skin"],
        "Scales": ["whip skin", "Gold Shine"],
    }


def test_the_captured_login_goes_to_the_account_named_in_it(tmp_path: Path) -> None:
    """A capture belongs to one account, and it says which one.

    The rule used to be first-come: with no owner recorded, whoever connected
    first was handed the capture. On one person's machine that is right. On a
    server hosted for other people it means the first stranger through the door
    starts with the host's relics, reputation and route history -- invisible to
    the host, obvious to their friends.
    """
    from phantom_offline.profile import ProfileStore

    store = ProfileStore(tmp_path / "profiles", seed=CAPTURED_LOGIN)
    stranger = store.for_player("76561198000000222", name="Alice")
    host = store.for_player("76561198000000111", name="Tomb Raider")

    assert stranger.snapshot()["reputation"] == 0, "arriving first earns nothing"
    assert stranger.snapshot()["currency"] == {"essence": 0, "dungeonKeys": None}
    assert stranger.snapshot()["existingUser"] is False
    assert host.snapshot()["reputation"] == 5582


def test_an_unnamed_login_claims_nothing(tmp_path: Path) -> None:
    """No name, no claim -- the capture stays where it is."""
    from phantom_offline.profile import ProfileStore

    store = ProfileStore(tmp_path / "profiles", seed=CAPTURED_LOGIN)
    assert store.for_player("76561198000000222").snapshot()["reputation"] == 0


def test_the_claim_survives_a_restart(tmp_path: Path) -> None:
    from phantom_offline.profile import ProfileStore

    ProfileStore(tmp_path / "profiles", seed=CAPTURED_LOGIN).for_player(
        "host-1", name="Tomb Raider")
    reopened = ProfileStore(tmp_path / "profiles", seed=CAPTURED_LOGIN)
    assert reopened.for_player("guest-2", name="Bob").snapshot()["reputation"] == 0
    assert reopened.for_player("host-1").snapshot()["reputation"] == 5582


def test_two_players_never_share_an_in_game_id(tmp_path: Path) -> None:
    """Sharing one is two phantoms under one actor name, which crashes spawn."""
    from phantom_offline.profile import ProfileStore

    store = ProfileStore(tmp_path / "profiles", seed=CAPTURED_LOGIN)
    a = store.for_player("76561198000000222", name="Alice").user_id()
    b = store.for_player("76561198000000333", name="Bob").user_id()
    assert a and b and a != b
    # And it must not move underneath them between sessions.
    reopened = ProfileStore(tmp_path / "profiles", seed=CAPTURED_LOGIN)
    assert reopened.for_player("76561198000000222").user_id() == a


def test_a_run_is_filed_under_the_name_the_client_sent(tmp_path: Path) -> None:
    """/SubmitRun carries no name, so the profile has to remember it."""
    from phantom_offline.profile import ProfileStore

    store = ProfileStore(tmp_path / "profiles")
    profile = store.for_player("76561198000000222", name="Alice")
    profile.set_name("Alice")
    assert profile.snapshot()["currentUsername"] == "Alice"


def test_an_explicit_owner_still_wins(tmp_path: Path) -> None:
    """Captured credentials name the owner outright; that must take priority."""
    from phantom_offline.profile import ProfileStore

    store = ProfileStore(
        tmp_path / "profiles", seed=CAPTURED_LOGIN, seed_owner="76561198000000999"
    )
    assert store.for_player("76561198000000111").snapshot()["reputation"] == 0
    assert store.for_player("76561198000000999").snapshot()["reputation"] == 5582
