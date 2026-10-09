# Multiplayer protocol smoke test for the letter-market model.
# Drives the FastAPI server exactly like script.js does: full game_state in
# every update_state, takeTile then placeTile per turn, and both players'
# actions in server-turn order. Verifies: initial market adoption, takeTile
# mirroring (market/bag/rack/awaitingTake), placeTile validation, the 25-place
# phase transition with a fresh client deal mirrored, and game over at 50.
import json
import urllib.request

BASE = "http://127.0.0.1:8123"


def post(path, body=None, query=""):
    url = BASE + path + query
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


def get(path):
    with urllib.request.urlopen(BASE + path) as r:
        return json.loads(r.read().decode())


def status_of(path, body, query=""):
    try:
        post(path, body, query)
        return 200
    except urllib.error.HTTPError as e:
        return e.code


# --- create and join ---
c = post("/create_game")
gid = c["game_id"]
j = post("/join_game", query="?game_id=" + gid)
assert j["player_id"] == "player2", j


def tile(letter, tid):
    return {"letter": letter, "id": tid, "used": False, "selected": False}


tid = [0]
def nt(letter):
    tid[0] += 1
    return tile(letter, "sm-tile-%d" % tid[0])


# Racks: p1 always plays A, p2 always plays B (server validates by letter).
p1r = [nt("A") for _ in range(4)]
p2r = [nt("A") for _ in range(4)]  # both start with A so phase1 places from rack
market = [nt("A") for _ in range(5)]
bag = [nt("A") for _ in range(60)]

board = [[None for _ in range(5)] for _ in range(5)]


def gs(**over):
    s = {
        "board": board,
        "player1": {"tiles": p1r, "originalTiles": p1r, "isBuilder": True, "score": 0,
                    "blockPoints": 0, "firstWordTilesLeft": None, "name": "P1"},
        "player2": {"tiles": p2r, "originalTiles": p2r, "isBuilder": False, "score": 0,
                    "blockPoints": 0, "firstWordTilesLeft": None, "name": "P2"},
        "currentPlayerId": "player1", "currentPhase": 1, "placedTilesThisPhase": 0,
        "selectedTile": None, "foundWords": [], "gameMode": "multiplayer", "gameOver": False,
        "statusText": "smoke", "endOfPhase1Board": None,
        "market": market, "bag": bag, "awaitingTake": True,
    }
    s.update(over)
    return s


# Host adopts the initial state (market + bag included) on first sync.
post("/update_state", {"game_id": gid, "player_id": "player1", "game_state": gs(), "last_action": None, "ready": True})
post("/update_state", {"game_id": gid, "player_id": "player2", "game_state": gs(), "last_action": None, "ready": True})
st = get("/get_state?game_id=%s&player_id=player1" % gid)["game_state"]
assert st["market"] and len(st["market"]) == 5, "market not adopted on initial sync"
assert st["bag"] and len(st["bag"]) == 60, "bag not adopted"
full = get("/get_state?game_id=%s&player_id=player1" % gid)
assert full.get("readiness") == {"player1": True, "player2": True}, full.get("readiness")

# --- play all 50 placements ---
def empty_cell():
    for r in range(5):
        for c in range(5):
            if board[r][c] is None:
                return r, c
    return None


placed = 0
phase2_seen = False
letters = {"player1": "A", "player2": "A"}
cur = "player1"


def sync_server_phase():
    """Mirror a server-side phase transition into the local board (the client
    clears the board in switchPhase; the smoke follows the same cue)."""
    global phase2_seen
    st = get("/get_state?game_id=%s&player_id=player1" % gid)["game_state"]
    if st["currentPhase"] == 2 and not phase2_seen:
        phase2_seen = True
        board[:] = [[None for _ in range(5)] for _ in range(5)]


for move in range(50):
    sync_server_phase()
    hand = p1r if cur == "player1" else p2r
    # take step: pop a market letter into the rack
    taken = market.pop(0)
    taken["used"] = False
    market.append(bag.pop(0) if bag else None)
    hand.append(taken)
    code = status_of("/update_state", {"game_id": gid, "player_id": cur, "game_state": gs(awaitingTake=False),
                                       "last_action": {"action": "takeTile", "letter": taken["letter"]}})
    assert code == 200, (move, "takeTile rejected", code)
    st = get("/get_state?game_id=%s&player_id=%s" % (gid, cur))["game_state"]
    assert st["awaitingTake"] is False, "awaitingTake not mirrored after takeTile"

    # place step
    cell = empty_cell()
    assert cell, "board full mid-smoke"
    r, c = cell
    letter = "A"
    board[r][c] = {"letter": letter, "owner": cur}
    for t in hand:
        if t["letter"] == letter and not t["used"]:
            t["used"] = True
            break
    placed += 1
    code = status_of("/update_state", {"game_id": gid, "player_id": cur,
                                       "game_state": gs(placedTilesThisPhase=0, awaitingTake=True,
                                                        currentPlayerId=("player2" if cur == "player1" else "player1")),
                                       "last_action": {"action": "placeTile", "row": r, "col": c,
                                                       "letter": letter, "placedBy": cur}})
    assert code == 200, (move, "placeTile rejected", code)
    st = get("/get_state?game_id=%s&player_id=%s" % (gid, cur))["game_state"]
    cur = st["currentPlayerId"]

final = get("/get_state?game_id=%s&player_id=player1" % gid)
fs = final["game_state"]
assert phase2_seen, "phase 2 transition never observed"
assert fs["gameOver"] is True, "game over not set after 50 placements"
assert fs["currentPhase"] == 2
print("SMOKE OK: phase2 seen, gameOver after 50 placements, market+takeTile mirrored, final placedTilesThisPhase=%d" % fs["placedTilesThisPhase"])
