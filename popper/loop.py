#!/usr/bin/env python3

import logging
import sys
from . util import Settings, Stats, timeout, parse_settings, format_program
from . asp import ClingoGrounder, ClingoSolver
from . tester import Tester
from . constrain import Constrain
from . generate import generate_program
from . core import Grounding, Clause
from . chess_test import ChessTester

class Outcome:
    ALL = 'all'
    SOME = 'some'
    NONE = 'none'

class Con:
    GENERALISATION = 'generalisation'
    SPECIALISATION = 'specialisation'
    REDUNDANCY = 'redundancy'
    BANISH = 'banish'

OUTCOME_TO_CONSTRAINTS = {
        (Outcome.ALL, Outcome.NONE)  : (Con.BANISH,),
        (Outcome.ALL, Outcome.SOME)  : (Con.GENERALISATION,),
        (Outcome.SOME, Outcome.NONE) : (Con.SPECIALISATION,),
        (Outcome.SOME, Outcome.SOME) : (Con.SPECIALISATION, Con.GENERALISATION),
        (Outcome.NONE, Outcome.NONE) : (Con.SPECIALISATION, Con.REDUNDANCY),
        (Outcome.NONE, Outcome.SOME) : (Con.SPECIALISATION, Con.REDUNDANCY, Con.GENERALISATION)
    }

def ground_rules(stats, grounder, max_clauses, max_vars, clauses):
    out = set()
    for clause in clauses:
        head, body = clause
        # find bindings for variables in the constraint
        assignments = grounder.find_bindings(clause, max_clauses, max_vars)

        # keep only standard literals
        body = tuple(literal for literal in body if not literal.meta)

        # ground the clause for each variable assignment
        for assignment in assignments:
            out.add(Grounding.ground_clause((head, body), assignment))
    
    stats.register_ground_rules(out)

    return out

def decide_outcome(conf_matrix):
    tp, fn, tn, fp = conf_matrix
    if fn == 0:
        positive_outcome = Outcome.ALL # complete
    elif tp == 0 and fn > 0:
        positive_outcome = Outcome.NONE # totally incomplete
    else:
        positive_outcome = Outcome.SOME # incomplete

    if fp == 0:
        negative_outcome = Outcome.NONE  # consistent
    # elif FP == self.num_neg:     # AC: this line may not work with minimal testing
        # negative_outcome = Outcome.ALL # totally inconsistent
    else:
        negative_outcome = Outcome.SOME # inconsistent

    return (positive_outcome, negative_outcome)

def write_valid_programs(programs):
    for program in programs:
        print(program)

def build_rules(settings, stats, constrainer, tester, program, before, min_clause, outcome, conf_matrix):
    (positive_outcome, negative_outcome) = outcome
    # RM: If you don't use these two lines you need another three entries in the OUTCOME_TO_CONSTRAINTS table (one for every positive outcome combined with negative outcome ALL).
    if negative_outcome == Outcome.ALL:
         negative_outcome = Outcome.SOME

    rules = set()

    tp, fn, tn, fp = conf_matrix
    if tp + fp == 0: # if coverage is 0, exclude specializations of this program (specializations will also have 0 coverage)
        # print('% adding constraints')
        rules.update(constrainer.specialisation_constraint(program, before, min_clause))

    # for constraint_type in OUTCOME_TO_CONSTRAINTS[(positive_outcome, negative_outcome)]:
    #     if constraint_type == Con.GENERALISATION:
    #         rules.update(constrainer.generalisation_constraint(program, before, min_clause))
    #     elif constraint_type == Con.SPECIALISATION:
    #         rules.update(constrainer.specialisation_constraint(program, before, min_clause))
    #     elif constraint_type == Con.REDUNDANCY:
    #         rules.update(constrainer.redundancy_constraint(program, before, min_clause))
    #     elif constraint_type == Con.BANISH:
    #         rules.update(constrainer.banish_constraint(program, before, min_clause))

    # if settings.functional_test and tester.is_non_functional(program):
    #     rules.update(constrainer.generalisation_constraint(program, before, min_clause))

    # eliminate generalisations of clauses that contain redundant literals
    # for rule in tester.check_redundant_literal(program):
    #     rules.update(constrainer.redundant_literal_constraint(rule, before, min_clause))

    # eliminate generalisations of programs that contain redundant clauses
    # if tester.check_redundant_clause(program):
    #     rules.update(constrainer.generalisation_constraint(program, before, min_clause))

    # if len(program) > 1:
    #     # evaluate inconsistent sub-clauses
    #     for rule in program:
    #         if Clause.is_separable(rule) and tester.is_inconsistent(rule):
    #             for x in constrainer.generalisation_constraint([rule], before, min_clause):
    #                 rules.add(x)

        # # eliminate totally incomplete rules
        # if all(Clause.is_separable(rule) for rule in program):
        #     for rule in program:
        #         if tester.is_totally_incomplete(rule):
        #             for x in constrainer.redundancy_constraint([rule], before, min_clause):
        #                 rules.add(x)

    stats.register_rules(rules)

    return rules

PROG_KEY = 'prog'

def calc_score(conf_matrix):
    tp, fn, tn, fp = conf_matrix
    return tp + tn

def popper(settings, stats):
    solver = ClingoSolver(settings)
    tester = ChessTester(settings)
    settings.num_pos, settings.num_neg = len(tester.pos), len(tester.neg)
    grounder = ClingoGrounder()
    constrainer = Constrain()
    constraint_rule_buffer = []
    valid_tactics = set()
    BUFFER_LIMIT = 1000 # update after every `BUFFER_LIMIT` constraints added

    for size in range(1, settings.max_literals + 1):
        stats.update_num_literals(size)
        solver.update_number_of_literals(size)
        solver.solver.configuration.solve.models = 0
        all_rules = []

        print(f'% searching programs of size:{size}')

        while True:
            with solver.solver.solve(yield_ = True) as handle:
                for m in handle:
                    model = m.symbols(shown = True)
                    # GENERATE HYPOTHESIS
                    with stats.duration('generate'):
                        program, before, min_clause = generate_program(model)

                    # TEST HYPOTHESIS
                    with stats.duration('test'):
                        conf_matrix = tester.test(program)
                        outcome = decide_outcome(conf_matrix)
                        score = calc_score(conf_matrix)

                    stats.register_program(program, conf_matrix)

                    # # UPDATE BEST PROGRAM
                    # if best_score == None or score > best_score:
                    #     best_score = score

                    #     if outcome == (Outcome.ALL, Outcome.NONE):
                    #         stats.register_solution(program, conf_matrix)
                    #         return stats.solution.code

                    #     stats.register_best_program(program, conf_matrix)

                    # BUILD RULES
                    with stats.duration('build'):
                        rules = build_rules(settings, stats, constrainer, tester, program, before, min_clause, outcome, conf_matrix)

                    # GROUND RULES
                    with stats.duration('ground'):
                        rules = ground_rules(stats, grounder, solver.max_clauses, solver.max_vars, rules)

                    # if we generate constraints, add them to the buffer
                    if rules:
                        constraint_rule_buffer.append(rules)
                    else:
                        print(f'% {format_program(program)}')
                        valid_tactics.add(format_program(program))
                    
                    # if the buffer exceeds the limit, apply the constraints and restart the solver
                    if len(constraint_rule_buffer) >= BUFFER_LIMIT:
                        break

            # UPDATE SOLVER
            if len(constraint_rule_buffer) >= BUFFER_LIMIT:
                with stats.duration('add'):
                    for rules in constraint_rule_buffer:
                        solver.add_ground_clauses(rules)
                    constraint_rule_buffer = []
                continue

            # all models of this size exhausted, restart with new size
            break

    write_valid_programs(valid_tactics)
    stats.register_completion()
    return stats.best_program.code if stats.best_program else None

def show_hspace(settings):
    f = lambda i, m: print(f'% program {i}\n{format_program(generate_program(m)[0])}')
    ClingoSolver.get_hspace(settings, f)

def learn_solution(settings):
    stats = Stats(log_best_programs=settings.info, stats_file=settings.stats_file)
    log_level = logging.DEBUG if settings.debug else logging.INFO
    logging.basicConfig(level=log_level, stream=sys.stderr, format='%(message)s')
    popper(settings, stats)
    #timeout(popper, (settings, stats), timeout_duration=int(settings.timeout))

    if stats.solution:
        prog_stats = stats.solution
    elif stats.best_programs:
        prog_stats = stats.best_programs[-1]
    else:
        return None, stats

    return prog_stats.code, stats
