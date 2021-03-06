import argparse
import csv
import logging
import math
from collections.abc import Callable
from typing import Generator, List, Optional, Tuple

import chess
import chess.engine
import chess.pgn
import pyparsing
from pyswip import Prolog
from pyswip.prolog import Prolog
from tqdm import tqdm

from prolog_parser import create_parser, parse_result_to_str
from util import *

logger = logging.getLogger(__name__)
logger.propagate = False # https://stackoverflow.com/a/2267567

def evaluate(evaluated_suggestions: List[Tuple[chess.Move, int]], top_moves: List[Tuple[chess.Move, int]], metric_fn: Callable[[int, float], float]) -> float:
    "Calculate a metric by comparing a given list of evaluated moves to the top recommended moves"
    metric: float = 0
    for idx, (evaluated_move, top_move) in enumerate(zip(evaluated_suggestions, top_moves)):
        _, score = evaluated_move
        _, top_score = top_move
        error = abs(top_score - score)
        metric += metric_fn(idx, error)
    return metric / len(evaluated_suggestions)

def get_tactic_match(prolog: Prolog, text: str, board: chess.Board, limit: int=3, time_limit_sec: Optional[int]=None, use_foreign_predicate: bool=False) -> Tuple[Optional[bool], Optional[List[chess.Move]]]:
    "Given the text of a Prolog-based tactic, and a position, check whether the tactic matched in the given position or and if so, what were the suggested moves"
    
    results = chess_query(prolog, text, board, limit=limit, time_limit_sec=time_limit_sec, use_foreign_predicate=use_foreign_predicate)
    if results is None:
        match, suggestions = None, None
    elif not results:
        match, suggestions = False, None
    else:
        match = True
        # convert suggestions to chess.Moves
        def suggestion_to_move(suggestion):
            from_sq = chess.parse_square(suggestion['From'])
            to_sq = chess.parse_square(suggestion['To'])
            return chess.Move(from_sq, to_sq)
        suggestions = list(map(suggestion_to_move, results))
    
    return match, suggestions

def print_metrics(metrics: dict, log_level=logging.INFO, **kwargs) -> None:
    tactic_text = kwargs['tactic_text']
    logger.log(log_level, f"Tactic: {tactic_text}")
    logger.log(log_level, f"# of positions: {metrics['total_positions']}")
    logger.log(log_level, f"Coverage: {metrics['total_matches'] / metrics['total_positions'] * 100:.2f}%") # % of matched positions
    logger.log(log_level, f"Total matches: {metrics['total_matches']}")
    if metrics['total_matches'] > 0:
        logger.log(log_level, f"Average number of suggestions per matched position: {metrics['num_suggestions'] / metrics['total_matches']:.2f}")
    logger.log(log_level, f"# of empty suggestions: {metrics['empty_suggestions']}/{metrics['total_positions']}") # number of positions where tactic did not suggest any move
    logger.log(log_level, f"Divergence = {metrics['divergence']:.2f}")
    logger.log(log_level, f"Average = {metrics['avg']:.2f}")
    logger.log(log_level, f"# of correct move suggestions = {metrics['correct_move']}")

@contextmanager
def write_metrics(metrics_list: List[dict], csv_filename: str) -> None:
    "Write metrics to csv file for analysis"
    with open(csv_filename, 'w') as csv_file:
        field_names = list(metrics_list[0].keys())
        writer = csv.DictWriter(csv_file, fieldnames=field_names)
        writer.writeheader()
        for metrics in metrics_list:
            writer.writerow(metrics)

def calc_metrics(prolog, tactic_text: str, engine: chess.engine.SimpleEngine, positions: Generator[chess.Board, None, None], settings) -> Optional[dict]:
    SUGGESTIONS_PER_TACTIC = 3
    metrics = {
        'total_positions': 0, # total number of positions (across all games)
        'total_matches': 0,
        'divergence': 0.0,
        'avg': 0.0,
        'empty_suggestions': 0,
        'num_suggestions': 0,
        'correct_move': 0,
        'tactic_evals': 0,
        'ground_evals': 0,
        'best_move_evals': 0
    }

    divergence_fn = lambda idx, error: error / math.log2(1 + (idx + 1))
    avg_fn = lambda _, error: error

    with tqdm(desc='Positions', unit='positions', leave=False) as pos_progress_bar:
        for board, move, label in positions:
            logger.debug(board)
            match, suggestions = get_tactic_match(prolog, tactic_text, board, limit=SUGGESTIONS_PER_TACTIC, time_limit_sec=settings.eval_timeout, use_foreign_predicate=settings.fpred)
            if match is None: # skip position for which we timeout
                continue
            logger.debug(f'Suggestions: {suggestions}')

            metrics['total_positions'] += 1

            ground_evals = get_evals(engine, board, [move], mate_score=settings.mate_score)
            best_moves = get_top_n_moves(engine, board, 1)
            best_move_evals = get_evals(engine, board, best_moves[:1], mate_score=settings.mate_score)
            metrics['ground_evals'] += ground_evals[0][1]
            metrics['best_move_evals'] += best_move_evals[0][1]
            
            if match:
                metrics['total_matches'] += 1
                if suggestions:
                    if move in suggestions:
                        metrics['correct_move'] += 1
                    tactic_evals = get_evals(engine, board, suggestions, mate_score=settings.mate_score)
                    metrics['divergence'] += evaluate(tactic_evals, ground_evals, divergence_fn)
                    metrics['avg'] += evaluate(tactic_evals, ground_evals, avg_fn)
                    metrics['num_suggestions'] += len(suggestions)
                    metrics['tactic_evals'] += tactic_evals[0][1]
                    
            else:
                logger.debug(f'Updated empty suggestions')
                metrics['empty_suggestions'] += 1
            pos_progress_bar.update(1)
    
    print_metrics(metrics, log_level=logging.DEBUG, tactic_text=tactic_text)
    return metrics

def parse_args():
    parser = argparse.ArgumentParser(description='Calculate metrics for a set of chess tactics')
    parser.add_argument('tactics_file', type=str, help='file containing list of tactics')
    parser.add_argument('--log', dest='log_level', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], help='Set the logging level', default='INFO')
    parser.add_argument('-n', '--num_tactics', dest='tactics_limit', type=int, help='Number of tactics to analyze', default=None)
    parser.add_argument('-e', '--engine', dest='engine_path', default='STOCKFISH', choices=['STOCKFISH', 'MAIA1100', 'MAIA1600', 'MAIA1900'], help='Path to engine executable to use for calculating divergence')
    parser.add_argument('--pgn', dest='pgn_file', default=LICHESS_2013, help='Path to PGN file of positions to use for calculating divergence')
    parser.add_argument('--num-games', dest='num_games', type=int, default=10, help='Number of games to use')
    parser.add_argument('--pos-per-game', dest='pos_per_game', type=int, default=10, help='Number of positions to use per game')
    parser.add_argument('--data-path', dest='data_path', type=str, default='tactics/data/stats/metrics_data.csv', help='File path to which metrics should be written')
    parser.add_argument('--pos-list', dest='pos_list', type=str, help='Path to file contatining list of positions to use for calculating divergence')
    parser.add_argument('--fpred', default=False, action='store_true', help='Use legal_move as a foreign predicate')
    parser.add_argument('--eval-timeout', type=int, default=None, help='Prolog evaluation timeout in seconds')
    parser.add_argument('--mate-score', type=int, default=2000, help='Score to use to approximate a Mate in X evaluation')
    return parser.parse_args()

def create_logger(log_level):
    logging.basicConfig(level=getattr(logging, log_level))
    logger = logging.getLogger(__name__)
    fmt = logging.Formatter('[%(levelname)s] [%(asctime)s] %(funcName)s:%(lineno)d - %(message)s')
    hdlr = logging.FileHandler('info.log', encoding='utf-8')
    hdlr.setFormatter(fmt)
    hdlr.setLevel(logging.DEBUG)
    logger.addHandler(hdlr)
    return logger

def main():
    # Create argument parser
    args = parse_args()
    if args.engine_path == 'STOCKFISH':
        engine_path = STOCKFISH
    elif args.engine_path == 'MAIA1100':
        engine_path = get_lc0_cmd(LC0, MAIA_1100)
    elif args.engine_path == 'MAIA1600':
        engine_path = get_lc0_cmd(LC0, MAIA_1600)
    elif args.engine_path == 'MAIA1900':
        engine_path = get_lc0_cmd(LC0, MAIA_1900)

    # Create logger
    logger = create_logger(args.log_level)
    
    # Calculate metrics for each tactic
    prolog_parser = create_parser()
    prolog = get_prolog(BK_FILE, args.fpred)
    metrics_list = []
    with get_engine(engine_path) as engine:
        with open(args.tactics_file) as hspace_handle:
            tactics_seen = 0
            with tqdm(total=args.tactics_limit, desc='Tactics', unit='tactics') as tactics_progress_bar:
                for line in hspace_handle:
                    logger.debug(line)
                    if line[0] == '%': # skip comments
                        continue
                    
                    # Get tactic
                    try:
                        tactic = prolog_parser.parse_string(line)
                    except pyparsing.exceptions.ParseException:
                        logger.error(f'Parsing error on {line}')
                        continue
                    logger.debug(tactic)
                    tactic_text = parse_result_to_str(tactic)
                    logger.debug(tactic_text)

                    # Get position list
                    if args.pos_list:
                        positions = chess_examples(args.pos_list)
                    elif args.pgn_file:
                        positions = positions_pgn(args.pgn_file, args.num_games, args.pos_per_game)
                    
                    metrics = calc_metrics(prolog, tactic_text, engine, positions, args)
                    if metrics:
                        metrics['tactic_text'] = tactic_text
                        metrics_list.append(metrics)
                    tactics_seen += 1
                    tactics_progress_bar.update(1)
                    if args.tactics_limit and tactics_seen >= args.tactics_limit:
                        break

    logger.info(f'% Calculated metrics for {tactics_seen} tactics')
    write_metrics(metrics_list, args.data_path)

if __name__ == '__main__':
    main()
