import argparse
import concurrent.futures
import csv
import html
import json
import string
import sys

import chess
import chess.engine
import chess.pgn
import chess.svg

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


def time_control(game):
    """Return (base_seconds, increment_seconds) from the TimeControl header, or (None, 0)."""
    time_control = game.headers.get("TimeControl", "")
    base, _, inc = time_control.partition("+")
    try:
        return float(base), float(inc or 0)
    except ValueError:
        return None, 0

def move_duration(game, move_num):
    """Return the seconds spent on the move_num, or None."""
    moves = list(game.mainline())
    move = moves[move_num]
    if move.emt() is not None:
        return move.emt()
    clk = move.clock()
    if clk is None:
        return None
    base, inc = time_control(game)
    prev_clk = moves[move_num - 2].clock() if move_num >= 2 else base
    if prev_clk is None:
        return None
    return prev_clk - clk + inc


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
            one_move_blunders_move = i
            break

    game.analyses["one_move_blunders"] = one_move_blunders
    if one_move_blunders:
    	# 0-based half-move index
        game.analyses["one_move_blunders.move"] = one_move_blunders_move
        stats.increment("one_move_blunders")

def format_clock(seconds):
    if seconds is None:
        return ""
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes}:{secs:02d}"

def render_svg_position(move):
    board = move.board()
    orientation = not board.turn
    check = board.king(board.turn) if board.is_check() else None
    return chess.svg.board(
        board,
        orientation=orientation,
        lastmove=move.move,
        check=check,
        size=400,
    )

def position_desc(game, move_num, svg):
    move = list(game.mainline())[move_num]
    link = game.headers.get("Link")
    return {
        "fen": move.board().fen(),
        "move": move.parent.board().lan(move.move),
        "clock": format_clock(move.clock()),
        "move_duration": format_clock(move_duration(game, move_num)),
        "url": f"{link}?move={move_num + 1}" if link else "",
        "svg": render_svg_position(move) if svg else "",
    }

def write_position_desc_csv(f, rows):
    writer = csv.DictWriter(f, fieldnames=["fen", "move", "clock", "move_duration", "url"], extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)

def write_position_desc_html(f, positions):
    page_template = string.Template("""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Positions</title>
</head>
<body>
$positions
</body>
</html>
""")

    pos_template = string.Template("""
<div>
<h3><a href="$url">$num. $url</a></h3>
$svg
<p>
Move: $move<br>
Clock: $clock<br>
Move duration: $move_duration<br>
Comment: - <br>
</p>
</div>""")

    pos_blocks = []
    for i, pos in enumerate(positions):
        pos_block = pos_template.substitute(
            num=i + 1,
            move=html.escape(pos["move"]),
            clock=html.escape(str(pos["clock"])),
            move_duration=html.escape(str(pos["move_duration"])),
            url=html.escape(pos["url"]),
            svg=pos["svg"],
        )
        pos_blocks.append(pos_block)
    f.write(page_template.substitute(positions="\n".join(pos_blocks)))


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
    parser.add_argument("--report", choices=["stats", "games", "positions:csv", "positions:html"], default="stats",
                        help="What to print: stats (default), games (PGN of games matching --type), positions:csv (position descriptions as CSV), positions:html (position descriptions as HTML with board images)")
    parser.add_argument("--games-count", type=int, help="Analyze only the first N games (0 means all)")
    args = parser.parse_args()

    if args.report in ["games", "positions:csv", "positions:html"] and args.type is None:
        parser.error(f"--report={args.report} requires --type")

    games = read_games(args.pgn)
    games = games[:args.games_count or None]
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
    elif args.report == "positions:csv":
        rows = [position_desc(g, g.analyses[f"{args.type}.move"], svg=False) for g in games if g.analyses.get(args.type)]
        write_position_desc_csv(sys.stdout, rows)
    elif args.report == "positions:html":
        rows = [position_desc(g, g.analyses[f"{args.type}.move"], svg=True) for g in games if g.analyses.get(args.type)]
        write_position_desc_html(sys.stdout, rows)

if __name__ == "__main__":
    main()
