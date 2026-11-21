import threading
import unittest

from firstchair.parallel import run_bounded


class ParallelTests(unittest.TestCase):
    def test_results_stay_in_input_order(self):
        finished = [threading.Event() for _ in range(3)]

        def make(index: int):
            def call() -> int:
                try:
                    if index < 2 and not finished[index + 1].wait(timeout=5):
                        raise AssertionError(f"call {index + 1} did not finish")
                    return index
                finally:
                    finished[index].set()

            return call

        self.assertEqual(run_bounded([make(index) for index in range(3)], limit=3), [0, 1, 2])

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(run_bounded([], limit=1), [])
        self.assertEqual(run_bounded((), limit=4), [])

    def test_limit_outside_1_through_4_raises_value_error(self):
        def unused() -> int:
            raise AssertionError("callable should not run")

        for limit in (0, -1, 5, 99, True, False, 1.0, None):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    run_bounded([unused], limit=limit)
        self.assertEqual(run_bounded([lambda: "low"], limit=1), ["low"])
        self.assertEqual(run_bounded([lambda: "high"], limit=4), ["high"])

    def test_calls_run_on_worker_threads(self):
        seen: list[threading.Thread] = []

        def call() -> str:
            seen.append(threading.current_thread())
            return "ok"

        self.assertEqual(run_bounded([call], limit=1), ["ok"])
        self.assertEqual(len(seen), 1)
        self.assertIsNot(seen[0], threading.current_thread())
        self.assertTrue(seen[0].name.startswith("firstchair-parallel"))

    def test_pool_threads_do_not_outlive_the_call(self):
        def work() -> int:
            return 1

        def fail() -> int:
            raise RuntimeError("stop")

        run_bounded([work], limit=1)
        self.assertFalse(_pool_threads())
        with self.assertRaises(RuntimeError):
            run_bounded([fail, work], limit=1)
        self.assertFalse(_pool_threads())

    def test_limit_one_does_not_start_second_until_first_returns(self):
        first_entered = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        order: list[str] = []

        def first() -> str:
            order.append("first-enter")
            first_entered.set()
            if not release_first.wait(timeout=5):
                raise AssertionError("first was not released")
            if second_started.is_set():
                raise AssertionError("second started before first returned")
            order.append("first-exit")
            return "first"

        def second() -> str:
            second_started.set()
            order.append("second-enter")
            return "second"

        outcome: dict[str, object] = {}

        def runner() -> None:
            try:
                outcome["result"] = run_bounded([first, second], limit=1)
            except BaseException as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=runner)
        thread.start()
        try:
            self.assertTrue(first_entered.wait(timeout=5))
            self.assertEqual(order, ["first-enter"])
            self.assertFalse(second_started.is_set())
        finally:
            release_first.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome["result"], ["first", "second"])
        self.assertEqual(order, ["first-enter", "first-exit", "second-enter"])

    def test_limit_two_callables_overlap(self):
        barrier = threading.Barrier(2)

        def make(value: str):
            def call() -> str:
                barrier.wait(timeout=5)
                return value

            return call

        self.assertEqual(run_bounded([make("a"), make("b")], limit=2), ["a", "b"])

    def test_limit_four_callables_overlap(self):
        barrier = threading.Barrier(4)

        def make(value: int):
            def call() -> int:
                barrier.wait(timeout=5)
                return value

            return call

        calls = [make(index) for index in range(4)]
        self.assertEqual(run_bounded(calls, limit=4), [0, 1, 2, 3])

    def test_failure_does_not_start_later_callables(self):
        started: list[str] = []

        def fail() -> None:
            started.append("fail")
            raise RuntimeError("fail")

        def later() -> str:
            started.append("later")
            return "later"

        with self.assertRaises(RuntimeError) as caught:
            run_bounded([fail, later], limit=1)
        self.assertEqual(str(caught.exception), "fail")
        self.assertEqual(started, ["fail"])

    def test_inflight_later_finishes_when_earlier_fails(self):
        barrier = threading.Barrier(2)
        later_finished = threading.Event()

        def earlier() -> None:
            barrier.wait(timeout=5)
            raise RuntimeError("earlier")

        def later() -> str:
            barrier.wait(timeout=5)
            later_finished.set()
            return "later"

        with self.assertRaises(RuntimeError) as caught:
            run_bounded([earlier, later], limit=2)
        self.assertEqual(str(caught.exception), "earlier")
        self.assertTrue(later_finished.is_set())

    def test_failure_does_not_start_a_call_beyond_the_inflight_pair(self):
        entered = [threading.Event(), threading.Event()]
        release_later = threading.Event()
        third_started = threading.Event()
        outcome: dict[str, BaseException] = {}

        def earlier() -> None:
            entered[0].set()
            if not entered[1].wait(timeout=5):
                raise AssertionError("later callable did not start")
            raise RuntimeError("earlier")

        def later() -> str:
            entered[1].set()
            if not release_later.wait(timeout=5):
                raise AssertionError("later callable was not released")
            return "later"

        def third() -> str:
            third_started.set()
            return "third"

        def runner() -> None:
            try:
                run_bounded([earlier, later, third], limit=2)
            except BaseException as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=runner)
        thread.start()
        try:
            self.assertTrue(entered[0].wait(timeout=5))
            self.assertTrue(entered[1].wait(timeout=5))
            self.assertFalse(third_started.is_set())
            # later is held, so a runner that waits for in-flight work is still blocked.
            self.assertTrue(thread.is_alive())
        finally:
            release_later.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(third_started.is_set())
        self.assertIsInstance(outcome["error"], RuntimeError)
        self.assertEqual(str(outcome["error"]), "earlier")

    def test_first_failure_in_input_order_propagates(self):
        entered = [threading.Event(), threading.Event()]
        first_error = RuntimeError("first")
        second_error = ValueError("second")
        finished: list[int] = []

        def first() -> None:
            entered[0].set()
            if not entered[1].wait(timeout=5):
                raise AssertionError("second did not enter")
            finished.append(0)
            raise first_error

        def second() -> None:
            entered[1].set()
            if not entered[0].wait(timeout=5):
                raise AssertionError("first did not enter")
            finished.append(1)
            raise second_error

        with self.assertRaises(RuntimeError) as caught:
            run_bounded([first, second], limit=2)
        self.assertIs(caught.exception, first_error)
        self.assertEqual(sorted(finished), [0, 1])


def _pool_threads() -> list[threading.Thread]:
    return [
        thread for thread in threading.enumerate()
        if thread.name.startswith("firstchair-parallel")
    ]
