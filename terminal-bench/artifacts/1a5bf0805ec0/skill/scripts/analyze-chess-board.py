#!/usr/bin/env python3
"""Analyze a chess board position by enumerating candidate FENs and validating
against expected tactical patterns (mate-in-1, winning material, etc.).

Usage:
  python3 analyze-chess-board.py --fen CANDIDATE_FEN --ambiguous SQUARE:TYPE1,TYPE2... --expected mate-in-1
  python3 analyze-chess-board.py --fen 'r1bk3r/1p3pp1/p1p1p3/3pPnbP/8/P1P5/1P2QPP1/R1B1K2R w KQ -' --ambiguous 'f5:n,b g5:n,b c1:b,q e2:q,k' --expected mate-in-1

Required args:
  --fen       A candidate FEN string (the agent's best guess)
  --ambiguous Square:type1,type2 pairs. For each ambiguous square, the script
              will try all listed piece types and validate each combination.
  --expected  Expected pattern: mate-in-1, mate-in-2, winning-material, best-move

The script:
  1. Parses the candidate FEN and ambiguous piece assignments
  2. Generates all valid combinations of ambiguous piece types
  3. For each combination, validates using python-chess:
     - mate-in-1: checks all legal moves for immediate checkmate
     - mate-in-2: checks all 2-move sequences
     - winning-material: evaluates captures that gain material
     - best-move: checks for any forced winning pattern
  4. Outputs FENs that produce the expected pattern, with the winning moves
"""
import argparse
import itertools
import sys

try:
    import chess
except ImportError:
    print("ERROR: python-chess not installed. Run: pip install python-chess")
    sys.exit(1)


def parse_ambiguous(spec):
    """Parse ambiguous spec like 'f5:n,b g5:n,b e2:q,k' into dict."""
    result = {}
    for pair in spec.split():
        sq, types = pair.split(':')
        piece_types = []
        for t in types.split(','):
            t = t.strip().lower()
            mapping = {'k': chess.KING, 'q': chess.QUEEN, 'r': chess.ROOK,
                       'b': chess.BISHOP, 'n': chess.KNIGHT, 'p': chess.PAWN}
            if t in mapping:
                piece_types.append(mapping[t])
        result[chess.parse_square(sq)] = piece_types
    return result


def check_mate_in_1(board):
    """Check if the side to move can deliver checkmate in 1. Returns list of mating moves."""
    mates = []
    for move in board.legal_moves:
        board.push(move)
        if board.is_checkmate():
            mates.append(move)
        board.pop()
    return mates


def check_mate_in_2(board):
    """Check if the side to move can force mate in 2. Returns list of first moves."""
    mate_first_moves = []
    for move in board.legal_moves:
        board.push(move)
        all_responses_mate = True
        if not board.is_checkmate():
            for resp in board.legal_moves:
                board.push(resp)
                can_mate = False
                for move2 in board.legal_moves:
                    board.push(move2)
                    if board.is_checkmate():
                        can_mate = True
                    board.pop()
                    if can_mate:
                        break
                board.pop()
                if not can_mate:
                    all_responses_mate = False
                    break
        board.pop()
        if all_responses_mate:
            mate_first_moves.append(move)
    return mate_first_moves


def find_winning_captures(board):
    """Find captures that win material (attacker value < captured value)."""
    values = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
              chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}
    wins = []
    for move in board.legal_moves:
        if board.is_capture(move):
            captured = board.piece_at(move.to_square)
            attacker = board.piece_at(move.from_square)
            if captured and attacker:
                if values.get(captured.piece_type, 0) > values.get(attacker.piece_type, 0):
                    wins.append((move, values[captured.piece_type] - values[attacker.piece_type]))
    return sorted(wins, key=lambda x: -x[1])


def generate_fen_combinations(base_fen, ambiguous, color):
    """Generate all valid FEN combinations by varying ambiguous pieces."""
    base_board = chess.Board(base_fen)
    squares = list(ambiguous.keys())
    type_options = [ambiguous[sq] for sq in squares]

    for combo in itertools.product(*type_options):
        board = base_board.copy()
        valid = True
        has_white_king = False
        has_black_king = False
        white_queens = 0
        black_queens = 0

        for sq, pt in zip(squares, combo):
            piece = board.piece_at(sq)
            if piece is None:
                valid = False
                break
            board.set_piece_at(sq, chess.Piece(pt, piece.color))

        # Validate: each side must have exactly one king
        for sq in chess.SQUARES:
            p = board.piece_at(sq)
            if p:
                if p.piece_type == chess.KING:
                    if p.color == chess.WHITE:
                        has_white_king = True
                    else:
                        has_black_king = True
                if p.piece_type == chess.QUEEN:
                    if p.color == chess.WHITE:
                        white_queens += 1
                    else:
                        black_queens += 1

        if not has_white_king or not has_black_king:
            valid = False
        if not board.is_valid():
            valid = False

        if valid:
            yield board


def main():
    parser = argparse.ArgumentParser(description='Analyze chess board by enumerating candidate FENs')
    parser.add_argument('--fen', required=True, help='Candidate FEN string')
    parser.add_argument('--ambiguous', required=True,
                        help='Ambiguous pieces: e.g. "f5:n,b g5:n,b e2:q,k"')
    parser.add_argument('--expected', required=True,
                        choices=['mate-in-1', 'mate-in-2', 'winning-material', 'best-move'],
                        help='Expected tactical pattern')
    args = parser.parse_args()

    ambiguous = parse_ambiguous(args.ambiguous)
    found = False
    total = 0

    print(f"Enumerating FEN combinations for {len(ambiguous)} ambiguous squares...")

    for board in generate_fen_combinations(args.fen, ambiguous, chess.WHITE):
        total += 1
        if total > 10000:
            print(f"Stopped after 10000 combinations. Narrow the ambiguous squares.")
            break

        if args.expected == 'mate-in-1':
            mates = check_mate_in_1(board)
            if mates:
                print(f"\nFOUND: Mate-in-1 in position:")
                print(board)
                moves_str = [f"{chess.square_name(m.from_square)}{chess.square_name(m.to_square)}" for m in mates]
                print(f"Mating moves: {' '.join(moves_str)}")
                found = True

        elif args.expected == 'mate-in-2':
            mates = check_mate_in_2(board)
            if mates:
                print(f"\nFOUND: Mate-in-2 in position:")
                print(board)
                moves_str = [f"{chess.square_name(m.from_square)}{chess.square_name(m.to_square)}" for m in mates]
                print(f"First moves: {' '.join(moves_str)}")
                found = True

        elif args.expected == 'winning-material':
            wins = find_winning_captures(board)
            if wins:
                print(f"\nFOUND: Winning material in position:")
                print(board)
                for move, gain in wins[:5]:
                    print(f"  {chess.square_name(move.from_square)}{chess.square_name(move.to_square)} (gain: {gain})")
                found = True

        elif args.expected == 'best-move':
            # Try mate-in-1 first, then winning material
            mates = check_mate_in_1(board)
            if mates:
                print(f"\nFOUND: Mate-in-1 in position:")
                print(board)
                moves_str = [f"{chess.square_name(m.from_square)}{chess.square_name(m.to_square)}" for m in mates]
                print(f"Mating moves: {' '.join(moves_str)}")
                found = True
            else:
                wins = find_winning_captures(board)
                if wins:
                    print(f"\nFOUND: Winning material in position:")
                    print(board)
                    for move, gain in wins[:3]:
                        print(f"  {chess.square_name(move.from_square)}{chess.square_name(move.to_square)} (gain: {gain})")
                    found = True

    if not found:
        print(f"\nNo position produced the expected pattern ({args.expected}) from {total} combinations.")
        print("The piece location detection or piece color detection may be wrong.")
        print("Try: re-examine the image with different pixel thresholds, or crop individual squares for image_read.")
    else:
        print(f"\nChecked {total} combinations total.")


if __name__ == '__main__':
    main()