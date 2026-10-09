from fastapi import FastAPI, HTTPException, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse # Import HTMLResponse
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
import uuid
import time # For potential timeout logic later
import json

app = FastAPI()

# Allow CORS for local dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allows all origins
    allow_credentials=True,
    allow_methods=["*"], # Allows all methods
    allow_headers=["*"], # Allows all headers
)

# --- Serve index.html at root ---
@app.get("/", response_class=HTMLResponse)
async def read_index():
    with open("index.html") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)

# --- Serve Static Files ---
# This tells FastAPI that when a request comes for /static/...,
# look inside the physical 'static' directory relative to where server.py is.
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- Health check ---
# Used by the client's keep-alive ping (every 4 minutes while a tab is open) to
# keep the free-tier worker from spinning down mid-match, and useful for monitoring.
@app.get("/healthz")
def healthz():
    return {"status": "ok", "games": len(games)}

# --- Pydantic Models for Game State ---
class Tile(BaseModel):
    letter: str
    id: str
    used: bool
    selected: bool

class PlayerState(BaseModel):
    tiles: List[Tile]
    originalTiles: List[Tile] = Field(default_factory=list) # Ensure this is part of state for reset
    isBuilder: bool
    score: int
    blockPoints: Optional[int] = 0 # Saboteur counter-score: own placed tiles inside no word
    firstWordTilesLeft: Optional[int] = None
    name: str

class FoundWord(BaseModel):
    word: str
    path: List[List[int]]
    pathKey: Optional[str] = None # Canonical dedupe key for the path's cell set
    p1Tiles: int
    p2Tiles: int
    # score: int # Score is dynamic based on who is builder, calculated client-side for display

class GameStateModel(BaseModel):
    board: List[List[Optional[Dict[str, Any]]]] # [[{letter, owner}, ...], null, ...]
    player1: PlayerState
    player2: PlayerState
    currentPlayerId: str
    currentPhase: int
    placedTilesThisPhase: int
    selectedTile: Optional[Dict[str, Any]] = None # {letter, id, owner}
    # moveHistory: list # Not sending full history to server, too large
    foundWords: List[FoundWord]
    # visibleWordLineIndices: Dict # Client handles this set, no need to sync its stringified version
    gameMode: str
    gameOver: bool
    statusText: Optional[str] = ""
    endOfPhase1Board: Optional[List[List[Optional[Dict[str, Any]]]]] = None


# In-memory game storage
games: Dict[str, Dict[str, Any]] = {} # game_id -> { game_state, players, last_action, readiness, last_updated }

MAX_GAMES = 100 # Simple limit to prevent memory exhaustion
GAME_TIMEOUT_SECONDS = 3600 # 1 hour for inactive games (optional feature)

@app.post("/create_game")
def create_game_endpoint():
    if len(games) >= MAX_GAMES:
        # Optional: Clean up old games here
        cleanup_old_games()
        if len(games) >= MAX_GAMES: # Check again after cleanup
             raise HTTPException(status_code=503, detail="Server busy, max games reached. Please try again later.")

    game_id = str(uuid.uuid4())[:8].upper() # 8-char uppercase ID
    
    # Player creating the game is 'player1' by default convention client-side
    player_id_creator = "player1" 

    # --- Initialize the game state on the server ---
    # This should be the authoritative initial state
    initial_game_state = GameStateModel(
        board=[[None for _ in range(5)] for _ in range(5)], # Matches CONFIG.boardSize on the client
        player1=PlayerState(tiles=[], originalTiles=[], isBuilder=True, score=0, name="You", firstWordTilesLeft=None), # Initialize player1 state
        player2=PlayerState(tiles=[], originalTiles=[], isBuilder=False, score=0, name="Opponent", firstWordTilesLeft=None), # Initialize player2 state
        currentPlayerId="player1", # Player 1 (Host/Builder) starts
        currentPhase=1,
        placedTilesThisPhase=0,
        selectedTile=None,
        foundWords=[], # Empty list initially
        gameMode="multiplayer", # Set game mode
        gameOver=False,
        statusText="Waiting for opponent to join...", # Initial status
        endOfPhase1Board=None # No end of phase 1 board initially
    )
    # TODO: Server should probably generate initial tiles here as well to be truly authoritative.
    # For now, assuming client might still handle initial tile generation/sync after joining.

    games[game_id] = {
        "game_state": initial_game_state, # Assign the initialized game state object
        "players": {player_id_creator: {"id": player_id_creator, "last_seen": time.time()}},
        "last_action": None,
        "readiness": {player_id_creator: False}, # Initial readiness state
        "last_updated": time.time(),
        "host_id": player_id_creator
    }
    print(f"Server: Game created with ID: {game_id} by {player_id_creator}")
    return {"game_id": game_id, "player_id": player_id_creator, "player_num": 1}

@app.post("/join_game")
def join_game_endpoint(game_id: str = Query(...)):
    game_id = game_id.upper()
    if game_id not in games:
        raise HTTPException(status_code=404, detail="Game not found.")
    
    game = games[game_id]
    if len(game["players"]) >= 2:
        # Check if one of the players is old/stale (optional advanced rejoining)
        raise HTTPException(status_code=400, detail="Game is full.")

    # New joining player is 'player2' by convention
    player_id_joiner = "player2"
    
    game["players"][player_id_joiner] = {"id": player_id_joiner, "last_seen": time.time()}
    game["readiness"][player_id_joiner] = False # Initialize readiness for the new player
    game["last_updated"] = time.time()

    print(f"Player {player_id_joiner} joined game: {game_id}")
    return {"game_id": game_id, "player_id": player_id_joiner, "player_num": 2}


class UpdateStatePayload(BaseModel):
    game_id: str
    player_id: str # This is 'player1' or 'player2'
    game_state: GameStateModel
    last_action: Optional[Any] = None
    ready: Optional[bool] = None

def game_states_equal(gs1, gs2):
    if gs1 is None or gs2 is None:
        return False
    return json.dumps(gs1, sort_keys=True) == json.dumps(gs2, sort_keys=True)

@app.post("/update_state")
def update_state_endpoint(payload: UpdateStatePayload):
    game_id = payload.game_id.upper() # Ensure game_id is upper case
    player_id = payload.player_id
    # We receive client_game_state and last_action, but the server state is authoritative.
    # We will use last_action to update the server state.
    last_action = payload.last_action
    ready_flag = payload.ready

    if game_id not in games:
        print(f"Server: Game {game_id} not found in update_state")
        raise HTTPException(status_code=404, detail="Game not found for update.")

    game = games[game_id]
    current_server_state = game['game_state']
    readiness = game['readiness']

    if current_server_state is None:
        print(f"Server: Error in update_state for game {game_id} - game_state is None.")
        raise HTTPException(status_code=500, detail="Game state not initialized.")

    print(f"Server: Received update_state from {player_id} for game {game_id}. Action: {last_action.get('action') if last_action else 'None'}. Current server turn: {current_server_state.currentPlayerId}")

    # --- Handle Initial State Sync from Host ---
    if last_action is None and player_id == game.get('host_id') and \
       current_server_state.player1.tiles == [] and current_server_state.player2.tiles == [] and \
       payload.game_state.player1.tiles and payload.game_state.player2.tiles:
           print(f"Server: Detected initial state sync from host {player_id}. Adopting initial game state including tiles.")
           game['game_state'] = payload.game_state
           current_server_state = game['game_state'] # Update reference
           print(f"Server: Initial state adopted. Current server turn: {current_server_state.currentPlayerId}")

    # --- Always Update Found Words from Client State ---
    # Since word finding is client-side, the server needs to trust and store the client's list.
    # This should happen after the initial state sync might replace the whole state object.
    if payload.game_state and payload.game_state.foundWords is not None:
         current_server_state.foundWords = payload.game_state.foundWords
         # print(f"Server: Updated foundWords from client for game {game_id}. Count: {len(current_server_state.foundWords)}")


    # Update readiness if provided.
    if ready_flag is not None:
        readiness[player_id] = ready_flag
        print(f"Server: Game {game_id}: Player {player_id} readiness set to {ready_flag}")

    # Process actions and update state server-side (ONLY if there is an action)
    if last_action:
        action_type = last_action.get('action')
        print(f"Server: Processing action: {action_type} from player {player_id}")

        # Validate it's the player's turn for move actions
        if action_type != 'signalReady' and current_server_state.currentPlayerId != player_id:
             print(f"Server: 403 Forbidden - Player {player_id} attempted {action_type} out of turn. Current server turn is: {current_server_state.currentPlayerId}")
             raise HTTPException(status_code=403, detail="It's not your turn to perform this action.")

        # --- Place Tile Action ---
        if action_type == 'placeTile':
            row = last_action.get('row')
            col = last_action.get('col')
            letter = last_action.get('letter')
            placedBy = last_action.get('placedBy')

            print(f"Server: Processing placeTile details: row={row}, col={col}, letter={letter}, placedBy={placedBy}")

            if placedBy == player_id:
                 player_state_obj = current_server_state.player1 if placedBy == 'player1' else current_server_state.player2

                 tile_to_place_in_hand = None
                 for tile_model in player_state_obj.tiles:
                     if tile_model.letter == letter and not tile_model.used:
                          tile_to_place_in_hand = tile_model
                          break

                 if tile_to_place_in_hand:
                     print(f"Server: Found available tile {letter} in {placedBy}'s hand. Marking as used.")
                     tile_to_place_in_hand.used = True

                     if 0 <= row < len(current_server_state.board) and 0 <= col < len(current_server_state.board[row]) and current_server_state.board[row][col] is None:
                         current_server_state.board[row][col] = {'letter': letter, 'owner': placedBy}
                         print(f"Server: Placed tile {letter} at ({row},{col}) on board.")

                         # --- Server-Side Turn Switching Logic (after a move) ---
                         print(f"Server: Before turn switch (action processed), currentPlayerId is: {current_server_state.currentPlayerId}")
                         current_server_state.currentPlayerId = 'player2' if current_server_state.currentPlayerId == 'player1' else 'player1'
                         print(f"Server: After turn switch (action processed), currentPlayerId is: {current_server_state.currentPlayerId}")

                         current_server_state.placedTilesThisPhase += 1
                         print(f"Server: placedTilesThisPhase incremented to {current_server_state.placedTilesThisPhase}")

                         # --- Server-Side Phase Transition Check and Logic ---
                         # Check if all tiles for the phase have been placed
                         # Need access to CONFIG.tilesPerPlayer, which is client-side.
                         # For now, hardcode 10, but ideally this should be synced or defined server-side.
                         TILES_PER_PLAYER = 10 # Assuming 10 tiles per player per phase

                         if current_server_state.placedTilesThisPhase >= TILES_PER_PLAYER * 2:
                             print(f"Server: Phase {current_server_state.currentPhase} finished. Initiating phase transition.")

                             just_finished_builder = current_server_state.player1 if current_server_state.player1.isBuilder else current_server_state.player2

                             # Calculate score for the just finished builder based on foundWords from client
                             phase_builder_score = 0
                             builder_is_p1 = just_finished_builder is current_server_state.player1
                             for found_word in current_server_state.foundWords:
                                 if builder_is_p1:
                                     phase_builder_score += found_word.p1Tiles * 1 + found_word.p2Tiles * 2
                                 else:
                                     phase_builder_score += found_word.p2Tiles * 1 + found_word.p1Tiles * 2

                             just_finished_builder.score = phase_builder_score # Update the total score for the builder role in this phase

                             # Saboteur block points: the saboteur's placed tiles that sit inside
                             # no word. Computed from the board before it is cleared for phase 2.
                             just_finished_saboteur = (current_server_state.player2
                                                       if current_server_state.player1.isBuilder
                                                       else current_server_state.player1)
                             sab_owner = 'player1' if just_finished_saboteur is current_server_state.player1 else 'player2'
                             word_cells = set()
                             for found_word in current_server_state.foundWords:
                                 for cell in found_word.path:
                                     word_cells.add((cell[0], cell[1]))
                             blocked = 0
                             for r_idx, board_row in enumerate(current_server_state.board):
                                 for c_idx, board_cell in enumerate(board_row):
                                     if board_cell and board_cell.get('owner') == sab_owner \
                                             and (r_idx, c_idx) not in word_cells:
                                         blocked += 1
                             just_finished_saboteur.blockPoints = (just_finished_saboteur.blockPoints or 0) + blocked

                             # Store First Word Tiles Left for both players
                             # (calculated client side, trusting the client's value)
                             current_server_state.player1.firstWordTilesLeft = payload.game_state.player1.firstWordTilesLeft
                             current_server_state.player2.firstWordTilesLeft = payload.game_state.player2.firstWordTilesLeft


                             if current_server_state.currentPhase == 1:
                                 print("Server: Transitioning to Phase 2.")
                                 current_server_state.endOfPhase1Board = current_server_state.board # Store Phase 1 board
                                 current_server_state.currentPhase = 2

                                 # Switch roles
                                 current_server_state.player1.isBuilder = not current_server_state.player1.isBuilder
                                 current_server_state.player2.isBuilder = not current_server_state.player2.isBuilder

                                 # Reset board for Phase 2 (empty board)
                                 current_server_state.board = [[None for _ in range(len(current_server_state.board[0]))] for _ in range(len(current_server_state.board))]

                                 # Reset players' tiles (set used to False, keep originalTiles)
                                 for player_state_obj in [current_server_state.player1, current_server_state.player2]:
                                     # Assuming originalTiles are preserved and contain the full set for the phase
                                     if player_state_obj.originalTiles:
                                         player_state_obj.tiles = [tile.model_copy(update={'used': False, 'selected': False}) for tile in player_state_obj.originalTiles]
                                         print(f"Server: Resetting tiles for player {player_state_obj.name} for Phase 2. Tiles count: {len(player_state_obj.tiles)}")
                                     else:
                                         # This case should ideally not happen if initial sync worked
                                         player_state_obj.tiles = []
                                         print(f"Server: Warning - originalTiles not found for player {player_state_obj.name} during phase 2 reset.")


                                 # Set current player to the new builder for Phase 2 start
                                 current_server_state.currentPlayerId = 'player1' if current_server_state.player1.isBuilder else 'player2'
                                 print(f"Server: Phase 2 starts. New currentPlayerId: {current_server_state.currentPlayerId}. New builder: {'Player 1' if current_server_state.player1.isBuilder else 'Player 2'}")


                                 current_server_state.placedTilesThisPhase = 0
                                 current_server_state.selectedTile = None
                                 current_server_state.foundWords = [] # Clear found words for the new phase
                                 # visibleWordLineIndices is client-side state, will be reset on client render
                                 current_server_state.statusText = f"Phase 2: {'Player 1' if current_server_state.player1.isBuilder else 'Player 2'} is Builder."


                             else: # Current phase is 2, game is over
                                 print("Server: Phase 2 finished. Game Over.")
                                 current_server_state.gameOver = True
                                 # Final results calculation is done client-side for display

                             # After phase transition, update status text (client will poll and render)
                             # The status text update is already done within the if/else blocks above


                     else:
                         print(f"Server: Invalid coordinates ({row},{col}) or cell already occupied for placeTile action from {placedBy}. Action rejected.")
                         raise HTTPException(status_code=400, detail="Invalid move: Cannot place tile at specified coordinates or cell is occupied.")
                 else:
                      print(f"Server: Player {placedBy} attempted to place tile {letter} but it's not in their hand or already used according to server state. Action rejected.")
                      raise HTTPException(status_code=400, detail="Invalid move: Tile not available or already used.")
            else:
                print(f"Server: Internal logic error - placedBy ({placedBy}) did not match player_id ({player_id}) after turn check.")

        # --- Signal Ready Action ---
        elif action_type == 'signalReady':
             # Readiness is handled above based on ready_flag, no need for action processing here
             pass

        # TODO: Add handlers for other actions: undoMove etc.
        # Undo would be complex with server authority but less so if trusting client,
        # though still requires server-side state management.
        elif action_type == 'undoMove':
             print(f"Server: Received undoMove action from {player_id}. Server does not support undo.")
             # For a friendly game, you might choose to allow the client to simply revert its state
             # and sync the reverted state, but server needs to validate this isn't abused.
             # For now, we'll just log that it's not supported server-side.
             pass

    # Update last seen time and last action regardless of action type
    game["last_action"] = payload.last_action
    game["last_updated"] = time.time()
    game["players"][player_id]["last_seen"] = time.time()

    print(f"Server: update_state endpoint finished for game {game_id}. Current server turn: {current_server_state.currentPlayerId}. Game Over: {current_server_state.gameOver}. Phase: {current_server_state.currentPhase}")
    # Return the authoritative server state in the response so client gets immediate feedback (optional, but helpful)
    return {
        "message": "State updated successfully",
        "game_state": current_server_state,
        "readiness": readiness
    }


@app.get("/get_state")
def get_state_endpoint(game_id: str = Query(...), player_id: str = Query(...)):
    game_id = game_id.upper()
    if game_id not in games:
        print(f"Server: Game {game_id} not found in get_state")
        raise HTTPException(status_code=404, detail="Game not found")

    game = games[game_id]
    current_server_state = game['game_state']
    readiness = game['readiness']

    if current_server_state is None:
        print(f"Server: Error in get_state for game {game_id} - game_state is None.")
        raise HTTPException(status_code=500, detail="Game state not initialized on server.")

    print(f"Server: Sending state for game {game_id} to player {player_id}. Current server turn: {current_server_state.currentPlayerId}. Game Over: {current_server_state.gameOver}. Phase: {current_server_state.currentPhase}")


    return {
        "game_state": current_server_state,
        "readiness": readiness
    }

def cleanup_old_games():
    current_time = time.time()
    games_to_delete = [
        game_id for game_id, game_data in games.items()
        if current_time - game_data.get("last_updated", 0) > GAME_TIMEOUT_SECONDS
    ]
    for game_id in games_to_delete:
        print(f"Cleaning up timed-out game: {game_id}")
        del games[game_id]

# --- Main ---
if __name__ == "__main__":
    import uvicorn
    # To run: uvicorn server:app --reload
    uvicorn.run(app, host="0.0.0.0", port=8000)