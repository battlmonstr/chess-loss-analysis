import argparse
import concurrent.futures
import json
import sys

import chess
import chess.engine
import chess.pgn

def player_color(game, player):
    if game.headers.get("White") == player:
        return chess.WHITE
    return chess.BLACK


def player_outcome(game, player):
    color = player_color(game, player)
    result = game.headers.get("Result")
    if result == "1-0":
        return "win" if color == chess.WHITE else "loss"
    elif result == "0-1":
        return "win" if color == chess.BLACK else "loss"
    return "draw"


def player_lost(game, player):
    return player_outcome(game, player) == "loss"


def detect_hero(games):
    """Detect the player name by finding a name that appears in both of the first 2 games."""
    if len(games) < 2:
        return None
    players_0 = set([games[0].headers.get("White"), games[0].headers.get("Black")])
    players_1 = set([games[1].headers.get("White"), games[1].headers.get("Black")])
    common = players_0.intersection(players_1)
    if len(common) == 1:
        return common.pop()
    return None


def player_moves_reversed(game, player):
    """Yield the player's moves from the end of the game backwards."""
    color = player_color(game, player)
    move = game.end()
    if move is None:
        return
    if move.parent is None:
        if move.turn() != color:
            yield move
        return
    while move.parent is not None:
        if move.parent.turn() == color:  # parent.turn() is who made this move
            yield move
        move = move.parent


def player_time_remaining(game, player):
    """Return the player's clock time (seconds) after their last move, or None."""
    for move in player_moves_reversed(game, player):
        clk = move.clock()
        if clk is not None:
            return clk
    return None


def last_move_above_time(game, player, threshold):
    """Return the last move where the player still had >= threshold seconds on the clock, or None."""
    for move in player_moves_reversed(game, player):
        clk = move.clock()
        if clk is not None and clk >= threshold:
            return move
    return None

def write_games(f, games):
    for game in games:
        print(game, file=f, end="\n\n")

def read_games(pgn_path):
    games = []
    with open(pgn_path) as f:
        game = chess.pgn.read_game(f)
        while game is not None:
            games.append(game)
            game = chess.pgn.read_game(f)
    return games


def evaluate_advantage(board, color):
    limit = chess.engine.Limit(depth=12)
    with chess.engine.SimpleEngine.popen_uci("stockfish") as engine:
        info = engine.analyse(board, limit)
    score = info["score"].pov(color)
    return score.score(mate_score=10000) / 100


def evaluate_game(game, color):
    results = []
    limit = chess.engine.Limit(depth=12)
    with chess.engine.SimpleEngine.popen_uci("stockfish") as engine:
        for move in game.mainline():
            info = engine.analyse(move.board(), limit)
            score = info["score"].pov(color)
            value = score.score(mate_score=10000) / 100
            results.append(value)
    return results


class Stats:
    def __init__(self, total_games):
        self.data = {"total_games": total_games}

    @property
    def total_games(self):
        return self.data["total_games"]

    def increment(self, key):
        self.data[key] = self.data.get(key, 0) + 1
        self.data[key + "_percentage"] = round(self.data[key] / self.total_games * 100)


def analyze_lost_on_time(game, player, stats):
    termination = game.headers.get("Termination", "")
    lost_on_time = player_lost(game, player) and "time" in termination.lower()
    game.analyses["lost_on_time"] = lost_on_time
    if lost_on_time:
        stats.increment("lost_on_time")


SIGNIFICANT_ADVANTAGE = 1.5  # pawns (centipawns / 100)

def analyze_lost_on_time_with_advantage(game, player, stats):
    if not game.analyses["lost_on_time"]:
        game.analyses["lost_on_time_with_advantage"] = False
        return

    color = player_color(game, player)
    advantage = evaluate_advantage(game.end().board(), color)

    lost_on_time_with_advantage = advantage >= SIGNIFICANT_ADVANTAGE
    game.analyses["lost_on_time_with_advantage"] = lost_on_time_with_advantage
    if lost_on_time_with_advantage:
        stats.increment("lost_on_time_with_advantage")


LOW_TIME = 30  # seconds

def analyze_lost_in_time_trouble(game, player, stats):
    time_left = player_time_remaining(game, player)
    lost_in_time_trouble = (
        player_lost(game, player)
        and not game.analyses["lost_on_time"]
        and time_left is not None
        and time_left < LOW_TIME
    )
    game.analyses["lost_in_time_trouble"] = lost_in_time_trouble
    if lost_in_time_trouble:
        stats.increment("lost_in_time_trouble")


def analyze_lost_in_time_trouble_with_advantage(game, player, stats):
    if not game.analyses["lost_in_time_trouble"]:
        game.analyses["lost_in_time_trouble_with_advantage"] = False
        return

    move = last_move_above_time(game, player, LOW_TIME)
    if move is None:
        game.analyses["lost_in_time_trouble_with_advantage"] = False
        return

    color = player_color(game, player)
    advantage = evaluate_advantage(move.board(), color)

    lost_in_time_trouble_with_advantage = advantage >= SIGNIFICANT_ADVANTAGE
    game.analyses["lost_in_time_trouble_with_advantage"] = lost_in_time_trouble_with_advantage
    if lost_in_time_trouble_with_advantage:
        stats.increment("lost_in_time_trouble_with_advantage")


BLUNDER_THRESHOLD = 2  # pawns

def analyze_one_move_blunders(game, player, stats):
    if not player_lost(game, player) or "evals" not in game.analyses:
        game.analyses["one_move_blunders"] = False
        return

    color = player_color(game, player)
    moves = list(game.mainline())
    evals = game.analyses["evals"]

    one_move_blunders = False
    for i in range(len(evals)):
        move = moves[i]
        if move.board().turn == color:
            continue

        eval_before = evals[i - 1] if i > 0 else 0
        eval_after = evals[i]
        eval_drop = eval_before - eval_after

        is_last_move = i + 1 >= len(moves)
        next_move = moves[i + 1] if not is_last_move else None
        next_move_captures_or_mates = (
            (next_move is not None)
            and (move.board().is_capture(next_move.move) or next_move.board().is_checkmate())
        )

        one_move_blunders = (
            (eval_drop >= BLUNDER_THRESHOLD)
            and (eval_before >= -0.5)
            and (eval_after < -0.5)
            and (is_last_move or next_move_captures_or_mates)
        )
        if one_move_blunders:
            one_move_blunders_move = i // 2 + 1
            break

    game.analyses["one_move_blunders"] = one_move_blunders
    if one_move_blunders:
        game.analyses["one_move_blunders_move"] = one_move_blunders_move
        stats.increment("one_move_blunders")


def analyze_lost_evals(game, player):
    if not player_lost(game, player):
        return
    color = player_color(game, player)
    game.analyses["evals"] = evaluate_game(game, color)


ANALYSES = {
    "lost_on_time": analyze_lost_on_time,
    "lost_on_time_with_advantage": analyze_lost_on_time_with_advantage,
    "lost_in_time_trouble": analyze_lost_in_time_trouble,
    "lost_in_time_trouble_with_advantage": analyze_lost_in_time_trouble_with_advantage,
    "one_move_blunders": analyze_one_move_blunders,
}

ANALYSIS_GROUPS = {
    "lost_on_time": ["lost_on_time"],
    "lost_on_time_with_advantage": ["lost_on_time", "lost_on_time_with_advantage"],
    "lost_in_time_trouble": ["lost_on_time", "lost_in_time_trouble"],
    "lost_in_time_trouble_with_advantage": ["lost_on_time", "lost_in_time_trouble", "lost_in_time_trouble_with_advantage"],
    "one_move_blunders": ["one_move_blunders"],
}

def game_summary(game, player):
    white = game.headers.get("White")
    black = game.headers.get("Black")
    result = game.headers.get("Result")
    outcome = player_outcome(game, player)
    return {
        "white": white,
        "black": black,
        "result": result,
        "outcome": outcome,
        "analyses": game.analyses,
    }

def main():
    parser = argparse.ArgumentParser(description="Analyze chess losses")
    parser.add_argument("pgn", help="Path to a .pgn file")
    parser.add_argument("--player", help="Player name to analyze")
    parser.add_argument("--workers", type=int, default=4, help="Number of parallel Stockfish workers")
    parser.add_argument("--type", choices=ANALYSIS_GROUPS.keys(), help="Run only this analysis type (default: all)")
    parser.add_argument("--report", choices=["stats", "games", "one_move_blunders"], default="stats",
                        help="What to print: stats (default), games (PGN of games matching --type), one_move_blunders (blunder move numbers)")
    args = parser.parse_args()

    if args.report == "games" and args.type is None:
        parser.error("--report=games requires --type")
    if args.report == "one_move_blunders" and args.type not in (None, "one_move_blunders"):
        parser.error("--report=one_move_blunders requires --type=one_move_blunders or no --type")

    games = read_games(args.pgn)
    #games = games[:10]
    stats = Stats(total_games=len(games))

    if args.player is None:
        args.player = detect_hero(games)
        if args.player is None:
            parser.error("Could not auto-detect the player name. Please specify --player")

    for game in games:
        game.analyses = {}

    print(f"evaluating {stats.total_games} games...", file=sys.stderr)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(analyze_lost_evals, g, args.player) for g in games]
        for i, future in enumerate(concurrent.futures.as_completed(futures)):
            future.result()  # reraise exceptions
            print(f"evaluated {i + 1} of {stats.total_games}", file=sys.stderr)

    for i in range(len(games)):
        game = games[i]
        print(f"analyzing game {i + 1} of {stats.total_games}", file=sys.stderr)

        analysis_names = ANALYSIS_GROUPS[args.type] if args.type else list(ANALYSIS_GROUPS)
        for name in analysis_names:
            ANALYSES[name](game, args.player, stats)

        summary = game_summary(game, args.player)
        #print(json.dumps(summary, indent=2))

    if args.report == "stats":
        print(json.dumps(stats.data, indent=2))
    elif args.report == "games":
        write_games(sys.stdout, [g for g in games if g.analyses.get(args.type)])
    elif args.report == "one_move_blunders":
        moves = [g.analyses["one_move_blunders_move"] for g in games if g.analyses.get("one_move_blunders")]
        print(moves)

if __name__ == "__main__":
    main()
