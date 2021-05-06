from pyswip import Prolog

import re
import os
import sys
from contextlib import contextmanager
from . constrain import Outcome

class Tester():
    def __init__(self, experiment):
        self.prolog = Prolog()
        self.eval_timeout = experiment.args.eval_timeout
        self.test_all = experiment.args.test_all
        self.num_pos = 0
        self.num_neg = 0
        self.load_basic(experiment.args.kbpath)
        self.seen_clause = set()

    def first_result(self, q):
        return list(self.prolog.query(q))[0]

    def load_basic(self, kbpath):
        bk_pl_path = os.path.join(kbpath, 'bk.pl')
        exs_pl_path = os.path.join(kbpath, 'exs.pl')
        test_pl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'test.pl')

        for x in [bk_pl_path, exs_pl_path, test_pl_path]:
            if os.name == 'nt': # if on Windows, SWI requires escaped directory separators
                x = x.replace('\\', '\\\\')
            self.prolog.consult(x)

        self.num_pos = int(self.first_result('count_pos(N)')['N'])
        self.num_neg = int(self.first_result('count_neg(N)')['N'])

        self.prolog.assertz(f'timeout({self.eval_timeout})')
        self.prolog.assertz(f'num_pos({self.num_pos})')
        self.prolog.assertz(f'num_neg({self.num_neg})')

    @contextmanager
    def using(self, program):
        current_clauses = set()
        try:
            for clause in program.clauses:
                self.prolog.assertz(clause.to_code())
                current_clauses.add((clause.head.predicate, clause.head.arity))
            yield
        finally:
            for predicate, arity in current_clauses:
                args = ','.join(['_'] * arity)
                self.prolog.retractall(f'{predicate}({args})')

    def check_redundant_literal(self, program):
        for clause in program.clauses:
            k = clause.my_hash()
            if k in self.seen_clause:
                continue
            self.seen_clause.add(k)
            C = f"[{','.join(('not_'+ clause.head.to_code(),) + tuple(lit.to_code() for lit in clause.body))}]"
            res = list(self.prolog.query(f'redundant_literal({C})'))
            if res:
                yield clause

    def check_redundant_clause(self, program):
        # AC: if the overhead of this call becomes too high, such as when learning programs with lots of clauses, we can improve it by not comparing already compared clauses
        prog = []
        for clause in program.clauses:
            C = f"[{','.join(('not_'+ clause.head.to_code(),) + tuple(lit.to_code() for lit in clause.body))}]"
            prog.append(C)
        prog = f"[{','.join(prog)}]"
        return list(self.prolog.query(f'redundant_clause({prog})'))

    def test(self, program):
        with self.using(program):
            try:
                if self.test_all:
                    res = self.first_result('do_test(TP,FN,TN,FP)')
                else:
                    # AC: TN is not calculated when performing minmal testing
                    res = self.first_result('do_test_minimal(TP,FN,TN,FP)')
                TP, FN, TN, FP = res['TP'], res['FN'], res['TN'], res['FP']
            except:
                print("A Prolog error occurred when testing the program:")
                for clause in program.clauses:
                    print('\t' + clause.to_code())
                raise

        # complete
        if TP == self.num_pos:
            positive_outcome = Outcome.ALL
        # totally incomplete
        elif TP == 0 and FN > 0: # AC: we must use TP==0 rather than FN=|E+| because of minimal testing
            positive_outcome = Outcome.NONE
        # incomplete
        else:
            positive_outcome = Outcome.SOME

        # consistent
        if FP == 0:
            negative_outcome = Outcome.NONE
        # totally inconsistent
        # AC: this line may not work with minimal testing
        # elif FP == self.num_neg:
            # negative_outcome = Outcome.ALL
        # inconsistent
        else:
            negative_outcome = Outcome.SOME

        return ((positive_outcome, negative_outcome), (TP,FN,TN,FP))