"""Tests for the agent kanban board.

Grouped by the property being defended rather than by module, because the
things worth protecting here are the seven invariants and the upgrade
guarantees -- not any particular function.

Run: python -m unittest discover tests -v
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kanban import (  # noqa: E402
    hooks,
    lease,
    locks,
    migrations,
    model,
    scaffold,
    scope,
    store,
    transitions,
    yamlite,
)
from kanban.actor import Actor  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


class IsolatedHome(unittest.TestCase):
    """Keeps the board registry out of the real user's home directory.

    Without this, running the tests wrote throwaway temp paths into
    ``~/.kanban/boards.json`` -- so ``kanban upgrade --all`` would then try to
    visit directories that no longer existed. Tests must not touch $HOME.
    """

    def setUp(self):
        self._home = tempfile.mkdtemp(prefix="kanban-home-")
        self._prev_home = os.environ.get(scaffold.ENV_HOME)
        os.environ[scaffold.ENV_HOME] = self._home
        super().setUp()

    def tearDown(self):
        super().tearDown()
        if self._prev_home is None:
            os.environ.pop(scaffold.ENV_HOME, None)
        else:
            os.environ[scaffold.ENV_HOME] = self._prev_home
        shutil.rmtree(self._home, ignore_errors=True)


class TempBoard(IsolatedHome):
    """A board in a throwaway directory."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="kanban-test-")
        self.board = store.Board.create(self.root)
        self.you = Actor.as_human("tester@host")
        self.agent = Actor("claude-code:test", human=False)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def make(self, card_id="001", status=None, **kwargs):
        card = model.new_card(card_id, kwargs.pop("title", "a card"), **kwargs)
        self.board.create_card_file(card, "")
        if status and status != "inbox":
            transitions.promote(self.board, card_id, self.you)
            if status != "ready":
                lease.claim(self.board, card_id, self.agent)
        return card_id


# ---------------------------------------------------------------------------
# yamlite: syntax, and the forward-compatibility promise
# ---------------------------------------------------------------------------

class TestYamlite(unittest.TestCase):
    def test_round_trips_the_spec_example(self):
        path = os.path.join(FIXTURES, "board-v1", "cards", "042.md")
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
        data, body = yamlite.parse_document(raw)
        again, body2 = yamlite.parse_document(
            yamlite.format_document(data, body)
        )
        self.assertEqual(data, again, "frontmatter is not stable")
        self.assertEqual(body, body2, "body is not stable")

    def test_leading_zero_id_stays_a_string(self):
        data, _ = yamlite.parse_document('---\nid: "042"\n---\n')
        self.assertEqual(data["id"], "042")
        self.assertIsInstance(data["id"], str)
        # ...and survives a write, which is where a naive emitter loses it.
        again, _ = yamlite.parse_document(yamlite.format_document(data, ""))
        self.assertEqual(again["id"], "042")

    def test_unquoted_leading_zero_also_stays_a_string(self):
        data, _ = yamlite.parse_document("---\nid: 042\n---\n")
        self.assertEqual(data["id"], "042")

    def test_real_numbers_stay_numbers(self):
        data, _ = yamlite.parse_document("---\na: 7\nb: 1.5\nc: -3\n---\n")
        self.assertEqual((data["a"], data["b"], data["c"]), (7, 1.5, -3))

    def test_unknown_keys_round_trip(self):
        source = '---\nid: "1"\nfuture_thing:\n  nested: keep me\n---\nbody\n'
        data, body = yamlite.parse_document(source)
        out = yamlite.format_document(data, body)
        self.assertIn("future_thing", out)
        self.assertIn("keep me", out)

    def test_comments_are_stripped_but_not_inside_quotes(self):
        data, _ = yamlite.parse_document(
            '---\na: value  # trailing\nb: "has # inside"\n---\n'
        )
        self.assertEqual(data["a"], "value")
        self.assertEqual(data["b"], "has # inside")

    def test_null_and_bool(self):
        data, _ = yamlite.parse_document(
            "---\na: null\nb: true\nc: false\nd:\n---\n"
        )
        self.assertIsNone(data["a"])
        self.assertIs(data["b"], True)
        self.assertIs(data["c"], False)
        self.assertIsNone(data["d"])

    def test_inline_and_block_lists(self):
        data, _ = yamlite.parse_document(
            '---\na: ["x", "y"]\nb:\n  - one\n  - two\nc: []\n---\n'
        )
        self.assertEqual(data["a"], ["x", "y"])
        self.assertEqual(data["b"], ["one", "two"])
        self.assertEqual(data["c"], [])

    def test_unterminated_frontmatter_is_an_error(self):
        with self.assertRaises(yamlite.YamliteError):
            yamlite.parse_document("---\nid: 1\nno closing fence\n")

    def test_document_without_frontmatter_is_all_body(self):
        data, body = yamlite.parse_document("just notes\n")
        self.assertEqual(data, {})
        self.assertEqual(body, "just notes\n")


# ---------------------------------------------------------------------------
# model: ordering, validation, body sections
# ---------------------------------------------------------------------------

class TestModel(unittest.TestCase):
    def test_normalize_orders_known_keys_and_appends_unknown(self):
        card = {"zz_custom": 1, "status": "inbox", "id": "1", "title": "t"}
        keys = list(model.normalize(card))
        self.assertEqual(keys[:3], ["id", "title", "status"])
        self.assertEqual(keys[-1], "zz_custom", "unknown key was not preserved")

    def test_validate_catches_bad_enums_and_types(self):
        card = model.new_card("1", "t")
        card["status"] = "nonsense"
        card["tier"] = "nonsense"
        problems = " ".join(model.validate(card))
        self.assertIn("status", problems)
        self.assertIn("tier", problems)

    def test_validate_requires_review_when_in_review(self):
        card = model.new_card("1", "t")
        card["status"] = "needs_review"
        self.assertTrue(
            any("review" in p for p in model.validate(card))
        )

    def test_non_string_id_is_rejected(self):
        card = model.new_card("1", "t")
        card["id"] = 42
        self.assertTrue(any("string" in p for p in model.validate(card)))

    def test_sections_round_trip(self):
        body = "notes here\n\n## Plan\n\nstep one\n\n## Log\n\n- entry\n"
        preamble, sections, _ = model.split_sections(body)
        self.assertEqual(preamble, "notes here")
        self.assertEqual(sections["Plan"], "step one")
        self.assertEqual(sections["Log"], "- entry")

    def test_subheadings_do_not_start_a_new_section(self):
        body = "## Plan\n\n### substep\n\ntext\n"
        _, sections, _ = model.split_sections(body)
        self.assertIn("### substep", sections["Plan"])
        self.assertNotIn("substep", [s for s in sections if s != "Plan"])

    def test_append_log_adds_one_line(self):
        body = model.append_log("", "actor", "ready", "claimed")
        self.assertEqual(body.count("ready → claimed"), 1)
        body = model.append_log(body, "actor", "claimed", "needs_review")
        self.assertEqual(len(model.get_section(body, "Log").splitlines()), 2)


# ---------------------------------------------------------------------------
# store: atomic writes, ids, config
# ---------------------------------------------------------------------------

class TestStore(TempBoard):
    def test_cards_are_written_as_utf8_with_lf(self):
        self.make("001")
        with open(self.board.card_path("001"), "rb") as handle:
            raw = handle.read()
        self.assertNotIn(b"\r\n", raw, "CRLF would break cross-platform bodies")

    def test_audit_arrow_survives_a_write(self):
        card_id = self.make("001", status="ready")
        lease.claim(self.board, card_id, self.agent)
        _, body = self.board.load(card_id)
        self.assertIn("→", body)

    def test_next_id_skips_used_ids(self):
        self.make("001")
        self.make("002")
        self.assertEqual(self.board.next_id(), "003")

    def test_duplicate_id_fails_loudly(self):
        self.make("001")
        with self.assertRaises(store.BoardError):
            self.board.create_card_file(model.new_card("001", "dupe"), "")

    def test_config_keeps_unknown_keys(self):
        config = self.board.read_config()
        config["something_new"] = "keep"
        self.board.write_config(config)
        self.assertEqual(self.board.read_config()["something_new"], "keep")

    def test_card_ids_cannot_escape_the_cards_directory(self):
        for bad in ("../CLAUDE", "..", "a/b", "a\\b", "", "-rf", "x y"):
            with self.assertRaises(store.BoardError, msg=bad):
                self.board.create_card_file(model.new_card(bad, "evil"), "")
        self.assertFalse(os.path.exists(os.path.join(self.root, "CLAUDE.md")))
        with self.assertRaises(store.BoardError):
            self.board.load("../../etc/notes")
        self.make("card-7.b")  # ordinary ids are untouched

    def test_board_inside_a_card_worktree_defers_to_the_live_board(self):
        # Regression: once the board is committed, wt/<id> holds a checkout of
        # .kanban/ and cards/, and an agent there was editing that stale copy.
        stale = os.path.join(self.root, "wt", "001")
        store.Board.create(stale)
        found = store.Board.find(os.path.join(stale, "src"))
        self.assertEqual(found.root, self.board.root)

    def test_unreadable_card_does_not_break_the_listing(self):
        self.make("001")
        store.atomic_write(self.board.card_path("002"), "not a card at all\n")
        results = list(self.board.all_cards())
        self.assertEqual(len(results), 2)
        self.assertTrue(any(card is None for _, card, _ in results))


# ---------------------------------------------------------------------------
# Invariants 1 and 2: atomic claiming
# ---------------------------------------------------------------------------

class TestClaiming(TempBoard):
    def test_claim_takes_the_lock(self):
        card_id = self.make("001", status="ready")
        lease.claim(self.board, card_id, self.agent)
        self.assertIsNotNone(locks.read(self.board.lock_path(card_id)))

    def test_second_claim_fails_loudly(self):
        card_id = self.make("001", status="ready")
        lease.claim(self.board, card_id, self.agent)
        with self.assertRaises(locks.ClaimConflict) as caught:
            lease.claim(self.board, card_id, Actor("other"))
        self.assertIn("claude-code:test", str(caught.exception))

    def test_claiming_a_non_ready_card_leaves_no_orphan_lock(self):
        card_id = self.make("001")  # still in inbox
        with self.assertRaises(lease.ClaimRefused):
            lease.claim(self.board, card_id, self.agent)
        self.assertIsNone(
            locks.read(self.board.lock_path(card_id)),
            "a refused claim left the card permanently unclaimable",
        )

    def test_lock_is_released_on_every_exit_from_claimed(self):
        # This is the regression that motivated moving lock release into
        # transitions.apply: submit_work used to leak the lock.
        for index, verb in enumerate(
            ("submit_work", "submit_plan", "submit_question")
        ):
            card_id = self.make("%03d" % (index + 1), status="claimed")
            arg = {"submit_plan": "a plan", "submit_question": "a question?"}
            if verb in arg:
                getattr(transitions, verb)(
                    self.board, card_id, self.agent, arg[verb]
                )
            else:
                getattr(transitions, verb)(self.board, card_id, self.agent)
            self.assertIsNone(
                locks.read(self.board.lock_path(card_id)),
                "%s leaked the claim lock" % verb,
            )

    def test_lock_and_status_stay_consistent(self):
        card_id = self.make("001", status="claimed")
        self.assertEqual(lease.audit_locks(self.board), [])
        os.unlink(self.board.lock_path(card_id))
        self.assertTrue(lease.audit_locks(self.board))

    def test_claim_increments_attempts(self):
        card_id = self.make("001", status="ready")
        lease.claim(self.board, card_id, self.agent)
        card, _ = self.board.load(card_id)
        self.assertEqual(card["attempts"], 1)


class TestClaimRace(unittest.TestCase):
    """Real processes, not mocks: only one may win."""

    def test_twenty_processes_one_winner(self):
        root = tempfile.mkdtemp(prefix="kanban-race-")
        try:
            board = store.Board.create(root)
            card = model.new_card("900", "race target", tier="act")
            board.create_card_file(card, "")
            transitions.promote(board, "900", Actor.as_human("t"))

            tool_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            env = dict(os.environ)
            env["PYTHONPATH"] = tool_root
            env.pop("KANBAN_HUMAN", None)

            procs = []
            for index in range(20):
                child_env = dict(env)
                child_env["KANBAN_ACTOR"] = "racer-%d" % index
                procs.append(
                    subprocess.Popen(
                        [sys.executable, "-m", "kanban", "claim", "900"],
                        cwd=root,
                        env=child_env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                )
            codes = [proc.wait() for proc in procs]
            self.assertEqual(
                codes.count(0), 1, "expected exactly one winner, got %r" % codes
            )
            self.assertEqual(len([c for c in codes if c != 0]), 19)
        finally:
            shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Invariant 3: leases expire, but only when genuinely stale
# ---------------------------------------------------------------------------

class TestLeaseExpiry(TempBoard):
    def _future(self, seconds):
        return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            seconds=seconds
        )

    def test_live_lease_is_not_reaped(self):
        self.make("001", status="claimed")
        self.assertEqual(lease.sweep(self.board), [])

    def test_expired_but_recently_beating_lease_is_not_reaped(self):
        # Both conditions are required. Expiry alone would kill an agent that
        # is working steadily through a long card.
        card_id = self.make("001", status="claimed")
        card, body = self.board.load(card_id)
        card["lease"]["expires_at"] = "2000-01-01T00:00:00Z"
        self.board.save(card_id, card, body)
        self.assertEqual(lease.sweep(self.board), [])

    def test_expired_and_stale_lease_is_reaped(self):
        card_id = self.make("001", status="claimed")
        reaped = lease.sweep(self.board, now=self._future(86400))
        self.assertEqual(reaped, [(card_id, "released")])
        card, body = self.board.load(card_id)
        self.assertEqual(card["status"], "ready")
        self.assertIsNone(card["lease"])
        self.assertIn("incident", model.get_section(body, "Context"))
        self.assertIsNone(locks.read(self.board.lock_path(card_id)))

    def test_heartbeat_prevents_reaping(self):
        card_id = self.make("001", status="claimed")
        lease.heartbeat(self.board, card_id, self.agent)
        self.assertEqual(lease.sweep(self.board), [])

    def test_heartbeat_rejects_a_different_owner(self):
        card_id = self.make("001", status="claimed")
        with self.assertRaises(lease.ClaimRefused):
            lease.heartbeat(self.board, card_id, Actor("someone-else"))

    def test_exhausted_attempts_park_instead_of_respawning(self):
        card_id = self.make("001", status="ready")
        config = self.board.read_config()
        config["max_attempts"] = 1
        self.board.write_config(config)
        lease.claim(self.board, card_id, self.agent)
        reaped = lease.sweep(self.board, now=self._future(86400))
        self.assertEqual(reaped, [(card_id, "parked")])
        card, _ = self.board.load(card_id)
        self.assertEqual(card["status"], "needs_review")


# ---------------------------------------------------------------------------
# Concurrent writers: the dashboard, the sweeper and agents share card files
# ---------------------------------------------------------------------------

class TestConcurrentWriters(TempBoard):
    def test_stale_transition_cannot_undo_a_newer_one(self):
        # The sweeper loads a claimed card, the agent submits, then the sweeper
        # saves "ready" -- which used to silently discard the submission.
        card_id = self.make("001", status="claimed")
        stale_card, stale_body = self.board.load(card_id)
        transitions.submit_work(self.board, card_id, self.agent)
        with self.assertRaises(transitions.TransitionError):
            transitions.apply(
                self.board, card_id, stale_card, stale_body, "ready",
                Actor.system(), "reaped",
            )
        card, _ = self.board.load(card_id)
        self.assertEqual(card["status"], "needs_review")

    def test_heartbeat_does_not_resurrect_a_reaped_lease(self):
        card_id = self.make("001", status="claimed")
        locks.release(self.board.lock_path(card_id))
        with self.assertRaises(lease.ClaimRefused):
            lease.heartbeat(self.board, card_id, self.agent)
        self.assertIsNone(locks.read(self.board.lock_path(card_id)))

    def test_atomic_write_leaves_no_temp_files(self):
        self.make("001")
        for _ in range(3):
            card, body = self.board.load("001")
            self.board.save("001", card, body)
        self.assertEqual(os.listdir(self.board.cards_dir), ["001.md"])

    def test_agents_still_starting_count_toward_the_cap(self):
        # Regression: only claimed cards counted, so every poll during an
        # agent's startup launched more agents for other queued cards.
        from kanban import dispatch

        self.make("001", status="ready")
        self.make("002", status="ready")
        self.assertEqual(dispatch.running_count(self.board), 0)
        card, body = self.board.load("001")
        card["dispatched_at"] = model.utcnow()
        self.board.save("001", card, body)
        self.assertEqual(dispatch.running_count(self.board), 1)
        card["dispatched_at"] = "2000-01-01T00:00:00Z"  # long dead
        self.board.save("001", card, body)
        self.assertEqual(dispatch.running_count(self.board), 0)


# ---------------------------------------------------------------------------
# Invariants 4 and 5: the review gate
# ---------------------------------------------------------------------------

class TestReviewGate(TempBoard):
    def test_agent_cannot_promote(self):
        card_id = self.make("001")
        with self.assertRaises(transitions.TransitionError):
            transitions.promote(self.board, card_id, self.agent)

    def test_human_can_promote(self):
        card_id = self.make("001")
        transitions.promote(self.board, card_id, self.you)
        card, _ = self.board.load(card_id)
        self.assertEqual(card["status"], "ready")

    def test_agent_cannot_approve_or_reject(self):
        card_id = self.make("001", status="claimed")
        transitions.submit_work(self.board, card_id, self.agent)
        with self.assertRaises(transitions.TransitionError):
            transitions.approve(self.board, card_id, self.agent)
        with self.assertRaises(transitions.TransitionError):
            transitions.reject(self.board, card_id, self.agent, "no")

    def test_only_finished_work_can_reach_merged(self):
        card_id = self.make("001", status="claimed")
        transitions.submit_plan(self.board, card_id, self.agent, "the plan")
        transitions.approve(self.board, card_id, self.you)
        card, _ = self.board.load(card_id)
        self.assertEqual(card["status"], "ready", "a plan must not merge")
        self.assertEqual(card["tier"], "act", "approval must flip the tier")

    def test_tier_only_flips_via_plan_approval(self):
        card_id = self.make("001", status="claimed", tier="plan")
        transitions.submit_work(self.board, card_id, self.agent)
        transitions.approve(self.board, card_id, self.you)
        card, _ = self.board.load(card_id)
        self.assertEqual(card["status"], "merged")
        self.assertEqual(card["tier"], "plan", "work approval changed the tier")

    def test_rejection_requires_feedback(self):
        card_id = self.make("001", status="claimed")
        transitions.submit_work(self.board, card_id, self.agent)
        with self.assertRaises(transitions.TransitionError):
            transitions.reject(self.board, card_id, self.you, "   ")

    def test_rejection_records_feedback_and_returns_to_ready(self):
        card_id = self.make("001", status="claimed")
        transitions.submit_work(self.board, card_id, self.agent)
        transitions.reject(self.board, card_id, self.you, "tests missing")
        card, body = self.board.load(card_id)
        self.assertEqual(card["status"], "ready")
        self.assertIn("tests missing", model.get_section(body, "Feedback"))

    def test_answer_records_question_and_answer(self):
        card_id = self.make("001", status="claimed")
        transitions.submit_question(self.board, card_id, self.agent, "which lib?")
        transitions.answer(self.board, card_id, self.you, "the existing one")
        card, body = self.board.load(card_id)
        self.assertEqual(card["status"], "ready")
        context = model.get_section(body, "Context")
        self.assertIn("which lib?", context)
        self.assertIn("the existing one", context)

    def test_illegal_transitions_are_refused(self):
        card_id = self.make("001")
        card, body = self.board.load(card_id)
        with self.assertRaises(transitions.TransitionError):
            transitions.apply(
                self.board, card_id, card, body, "merged", self.you
            )


# ---------------------------------------------------------------------------
# Invariant 7: the card is its own audit log
# ---------------------------------------------------------------------------

class TestSubmitGuards(TempBoard):
    def test_unclaimed_card_cannot_be_submitted_into_review(self):
        # ready -> needs_review exists for parking, so without a holder check
        # an agent could put a card nobody worked straight up for approval.
        card_id = self.make("001", status="ready")
        for verb, arg in (
            (transitions.submit_work, None),
            (transitions.submit_plan, "a plan"),
            (transitions.submit_question, "why?"),
        ):
            with self.assertRaises(transitions.TransitionError):
                verb(self.board, card_id, self.agent, *([arg] if arg else []))
        self.assertEqual(self.board.load(card_id)[0]["status"], "ready")

    def test_agent_cannot_submit_or_release_another_agents_card(self):
        card_id = self.make("001", status="claimed")
        other = Actor("claude-code:002")
        with self.assertRaises(transitions.TransitionError):
            transitions.submit_work(self.board, card_id, other)
        with self.assertRaises(lease.ClaimRefused):
            lease.release(self.board, card_id, other)
        lease.release(self.board, card_id, self.you)  # a human still can

    def test_agent_name_does_not_change_when_it_cds_into_the_worktree(self):
        # Regression: the default name came from the cwd, so claiming from
        # the root and heartbeating from wt/<id> looked like two agents.
        saved, here = os.environ.pop("KANBAN_ACTOR", None), os.getcwd()
        inside = os.path.join(self.root, "wt", "001")
        os.makedirs(inside)
        try:
            os.chdir(self.root)
            at_root = Actor.from_env("001").name
            os.chdir(inside)
            self.assertEqual(Actor.from_env("001").name, at_root)
        finally:
            os.chdir(here)
            if saved is not None:
                os.environ["KANBAN_ACTOR"] = saved


class TestDashboardOrigin(TempBoard):
    def setUp(self):
        super().setUp()
        import threading

        from kanban import server

        self.server = server.DashboardServer(
            ("127.0.0.1", 0), self.board, auto_dispatch=False
        )
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def _get(self, host):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", "/", headers={"Host": host})
            response = conn.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            conn.close()

    def test_a_rebound_hostname_gets_neither_page_nor_token(self):
        # DNS rebinding: evil.example resolves to 127.0.0.1, so the page is
        # same-origin with us and could read the CSRF token from our HTML.
        status, text = self._get("evil.example:%d" % self.port)
        self.assertEqual(status, 403)
        self.assertNotIn(self.server.session.csrf, text)

    def test_loopback_hosts_still_work(self):
        for host in ("127.0.0.1:%d" % self.port, "localhost:%d" % self.port):
            status, text = self._get(host)
            self.assertEqual(status, 200, host)
            self.assertIn(self.server.session.csrf, text)


class TestAuditLog(TempBoard):
    def test_every_transition_appends_exactly_one_line(self):
        card_id = self.make("001", tier="act")
        transitions.promote(self.board, card_id, self.you)
        lease.claim(self.board, card_id, self.agent)
        transitions.submit_work(self.board, card_id, self.agent)
        transitions.reject(self.board, card_id, self.you, "again please")
        lease.claim(self.board, card_id, self.agent)
        transitions.submit_work(self.board, card_id, self.agent)
        transitions.approve(self.board, card_id, self.you)

        _, body = self.board.load(card_id)
        entries = [
            line
            for line in model.get_section(body, "Log").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(entries), 7, "\n".join(entries))
        self.assertIn("needs_review → merged", entries[-1])

    def test_log_names_the_actor(self):
        card_id = self.make("001")
        transitions.promote(self.board, card_id, self.you)
        _, body = self.board.load(card_id)
        self.assertIn("tester@host", model.get_section(body, "Log"))


# ---------------------------------------------------------------------------
# depends_on gating
# ---------------------------------------------------------------------------

class TestDependencies(TempBoard):
    def test_blocked_card_cannot_be_claimed(self):
        self.make("001", status="ready")
        self.make("002", status="ready", depends_on=["001"])
        with self.assertRaises(lease.ClaimRefused) as caught:
            lease.claim(self.board, "002", self.agent)
        self.assertIn("001", str(caught.exception))
        self.assertIsNone(locks.read(self.board.lock_path("002")))

    def test_force_claims_and_records_the_override(self):
        self.make("001", status="ready")
        self.make("002", status="ready", depends_on=["001"])
        lease.claim(self.board, "002", self.agent, force=True)
        _, body = self.board.load("002")
        self.assertIn("forced past unmerged deps", body)

    def test_merged_dependency_unblocks(self):
        card_id = self.make("001", status="claimed", tier="act")
        transitions.submit_work(self.board, card_id, self.agent)
        transitions.approve(self.board, card_id, self.you)
        self.make("002", status="ready", depends_on=["001"])
        lease.claim(self.board, "002", self.agent)  # must not raise

    def test_missing_dependency_blocks_rather_than_being_ignored(self):
        self.make("002", status="ready", depends_on=["nope"])
        with self.assertRaises(lease.ClaimRefused):
            lease.claim(self.board, "002", self.agent)


# ---------------------------------------------------------------------------
# Invariant 6: scope
# ---------------------------------------------------------------------------

class TestScope(unittest.TestCase):
    def test_double_star_matches_across_directories(self):
        self.assertTrue(scope.matches("src/auth/token.py", "src/auth/**"))
        self.assertTrue(scope.matches("src/auth", "src/auth/**"))
        self.assertFalse(scope.matches("src/board/x.py", "src/auth/**"))

    def test_dotfile_pattern_matches_nested_dotfiles(self):
        # Regression: normalise() used lstrip("./"), which turned ".env*" into
        # "env*" and let a denied .env through.
        for path in (".env", ".env.local", "config/.env", "a/b/.env.prod"):
            self.assertEqual(
                scope.classify(path, [], [".env*"]),
                scope.DENIED,
                "%s should be denied" % path,
            )

    def test_single_star_does_not_cross_separators(self):
        self.assertFalse(scope.matches("src/a/b.py", "src/*.py"))
        self.assertTrue(scope.matches("src/b.py", "src/*.py"))

    def test_deny_beats_allow(self):
        verdict = scope.classify("src/auth/x.py", ["src/**"], ["src/auth/**"])
        self.assertEqual(verdict, scope.DENIED)

    def test_empty_allow_means_anything_not_denied(self):
        self.assertEqual(scope.classify("any/path.py", [], []), scope.ALLOWED)

    def test_allow_list_puts_other_paths_outside(self):
        self.assertEqual(
            scope.classify("docs/x.md", ["src/**"], []), scope.OUTSIDE
        )


class TestScopeHook(TempBoard):
    def test_card_id_derived_from_worktree_path(self):
        card_id = self.make("007")
        found = hooks.card_id_for_cwd(
            self.board, os.path.join(self.root, "wt", "007")
        )
        self.assertEqual(found, card_id)

    def test_unrelated_directory_yields_no_card(self):
        self.make("007")
        self.assertIsNone(
            hooks.card_id_for_cwd(self.board, os.path.join(self.root, "src"))
        )


# ---------------------------------------------------------------------------
# Upgradeability
# ---------------------------------------------------------------------------

class TestMigrations(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="kanban-mig-")
        self.board_dir = os.path.join(self.root, "board")
        shutil.copytree(
            os.path.join(FIXTURES, "board-v1"), self.board_dir
        )
        self.board = store.Board(self.board_dir)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _bodies(self):
        return {
            card_id: yamlite.parse_document(
                store.read_text(self.board.card_path(card_id))
            )[1]
            for card_id in self.board.card_ids()
        }

    def test_v1_board_is_refused_until_upgraded(self):
        with self.assertRaises(store.SchemaTooOld):
            self.board.check_compat()

    def test_newer_board_is_refused_rather_than_downgraded(self):
        config = self.board.read_config()
        config["schema_version"] = model.SCHEMA_VERSION + 5
        self.board.write_config(config)
        with self.assertRaises(store.SchemaTooNew):
            self.board.check_compat()
        with self.assertRaises(migrations.MigrationError):
            migrations.migrate_board(self.board)

    def test_migration_leaves_bodies_byte_identical(self):
        before = self._bodies()
        migrations.migrate_board(self.board, allow_dirty=True)
        after = self._bodies()
        self.assertEqual(
            before,
            after,
            "a migration changed a card body -- the audit log and agent "
            "findings are not migrations' business",
        )

    def test_migration_converts_the_schema(self):
        migrations.migrate_board(self.board, allow_dirty=True)
        self.board.check_compat()  # must not raise

        card, _ = self.board.load("038")
        self.assertEqual(card["status"], "needs_review")
        self.assertEqual(card["review"]["reason"], "question")
        self.assertEqual(
            card["review"]["question"], "Which auth library should this use?"
        )
        self.assertEqual(card["runtime"], "claude-code")
        self.assertIn("max_turns", card["budget"])
        self.assertEqual(card["spec"]["skills"], [])
        self.assertEqual(card["attempts"], 0)

    def test_removed_fields_are_kept_not_stripped(self):
        migrations.migrate_board(self.board, allow_dirty=True)
        card, _ = self.board.load("038")
        self.assertIn("max_usd", card["budget"], "a removed field was deleted")
        self.assertEqual(card["unknown_future_field"], "preserve me exactly")

    def test_unknown_config_keys_survive(self):
        migrations.migrate_board(self.board, allow_dirty=True)
        self.assertEqual(
            self.board.read_config()["a_field_this_tool_has_never_heard_of"],
            "must survive the upgrade",
        )

    def test_migration_is_idempotent(self):
        migrations.migrate_board(self.board, allow_dirty=True)
        first = self._bodies(), self.board.read_config()["schema_version"]
        report = migrations.migrate_board(self.board, allow_dirty=True)
        self.assertTrue(report["already_current"])
        self.assertEqual((self._bodies(), model.SCHEMA_VERSION), first)

    def test_dry_run_writes_nothing(self):
        before = self._bodies()
        report = migrations.migrate_board(self.board, dry_run=True)
        self.assertTrue(report["migrated"])
        self.assertEqual(self._bodies(), before)
        self.assertEqual(self.board.read_config()["schema_version"], 1)
        self.assertIsNone(report["snapshot"])

    def test_failure_mid_migration_restores_everything(self):
        before = self._bodies()
        original = dict(migrations.MIGRATIONS)

        calls = {"n": 0}

        def exploding(card):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("boom")
            return migrations.migrate_1_to_2(card)

        migrations.MIGRATIONS[1] = exploding
        try:
            with self.assertRaises(migrations.MigrationError):
                migrations.migrate_board(self.board, allow_dirty=True)
        finally:
            migrations.MIGRATIONS.clear()
            migrations.MIGRATIONS.update(original)

        self.assertEqual(self._bodies(), before, "board was not restored")
        self.assertEqual(self.board.read_config()["schema_version"], 1)

    def test_invalid_result_rolls_back_the_whole_batch(self):
        before = self._bodies()
        original = dict(migrations.MIGRATIONS)

        def breaks_validation(card):
            card = migrations.migrate_1_to_2(card)
            card["status"] = "not-a-real-status"
            return card

        migrations.MIGRATIONS[1] = breaks_validation
        try:
            with self.assertRaises(migrations.MigrationError):
                migrations.migrate_board(self.board, allow_dirty=True)
        finally:
            migrations.MIGRATIONS.clear()
            migrations.MIGRATIONS.update(original)

        self.assertEqual(self._bodies(), before)

    def test_rollback_restores_the_latest_snapshot(self):
        migrations.migrate_board(self.board, allow_dirty=True)
        migrated = self._bodies()
        card, body = self.board.load("038")
        body = model.append_to_section(body, "Context", "- later edit")
        self.board.save("038", card, body)
        self.assertNotEqual(self._bodies(), migrated)

        migrations.rollback(self.board)
        restored = self._bodies()
        self.assertNotIn("later edit", restored["038"])


class TestWorktreeLifecycle(unittest.TestCase):
    """One card, one worktree: kept across rejection, removed once merged."""

    def setUp(self):
        from kanban import worktree

        self.worktree = worktree
        self.root = tempfile.mkdtemp(prefix="kanban-wt-")
        if subprocess.run(
            ["git", "init", "-q", self.root], capture_output=True
        ).returncode != 0:
            self.skipTest("git unavailable")
        for args in (
            ["config", "user.email", "t@example.com"],
            ["config", "user.name", "t"],
        ):
            subprocess.run(["git"] + args, cwd=self.root, capture_output=True)
        store.atomic_write(os.path.join(self.root, "README.md"), "x\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, capture_output=True)
        subprocess.run(
            ["git", "commit", "-qm", "init"], cwd=self.root, capture_output=True
        )

        self.board = store.Board.create(self.root, base_branch="master")
        self.you = Actor.as_human("tester@host")
        self.agent = Actor("claude-code:test", human=False)
        card = model.new_card("001", "a card", tier="act")
        self.board.create_card_file(card, "")
        transitions.promote(self.board, "001", self.you)

    def tearDown(self):
        for card_id in ("001",):
            try:
                self.worktree.remove(self.board, card_id, force=True)
            except Exception:
                pass
        shutil.rmtree(self.root, ignore_errors=True)

    def _commits(self):
        result = subprocess.run(
            ["git", "log", "--oneline", "card/001"],
            cwd=self.root, capture_output=True, text=True,
        )
        return len([l for l in result.stdout.splitlines() if l.strip()])

    def test_claim_creates_worktree_reject_keeps_it_merge_removes_it(self):
        lease.claim(self.board, "001", self.agent)
        card, _ = self.board.load("001")
        path = os.path.join(self.root, card["artifacts"]["worktree"])
        self.assertTrue(os.path.isdir(path), "claim did not create a worktree")
        self.assertEqual(card["artifacts"]["branch"], "card/001")

        # An agent does some work and commits it.
        store.atomic_write(os.path.join(path, "f.txt"), "work\n")
        subprocess.run(["git", "add", "-A"], cwd=path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-qm", "wip"], cwd=path, capture_output=True
        )
        before = self._commits()
        self.assertGreater(before, 1)

        transitions.submit_work(self.board, "001", self.agent)
        transitions.reject(self.board, "001", self.you, "not yet")
        self.assertTrue(
            os.path.isfile(os.path.join(path, "f.txt")),
            "rejection destroyed the agent's work; it should be reusable",
        )
        self.assertEqual(self._commits(), before)

        # Second attempt reuses the same branch rather than starting over.
        lease.claim(self.board, "001", self.agent)
        card, _ = self.board.load("001")
        self.assertEqual(card["artifacts"]["branch"], "card/001")

        transitions.submit_work(self.board, "001", self.agent)
        transitions.approve(self.board, "001", self.you)
        self.assertNotIn(
            os.path.abspath(path),
            self.worktree.list_worktrees(self.root),
            "worktree should be removed once the card is merged",
        )
        self.assertEqual(
            self._commits(), before, "merging must not lose the branch history"
        )


def _git(root, *args):
    return subprocess.run(
        ["git"] + list(args), cwd=root, capture_output=True, text=True
    )


class TestScopeGuardInGit(IsolatedHome):
    """The guard against what agents actually type, in a real repository."""

    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="kanban-guard-")
        if _git(self.root, "init", "-q").returncode != 0:
            self.skipTest("git unavailable")
        _git(self.root, "config", "user.email", "t@example.com")
        _git(self.root, "config", "user.name", "t")
        store.atomic_write(os.path.join(self.root, "README.md"), "x\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "init")
        base = _git(self.root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

        self.board = store.Board.create(self.root, base_branch=base)
        self.agent = Actor("claude-code:001")
        card = model.new_card("001", "a card", tier="act", scope_deny=[".env*"])
        self.board.create_card_file(card, "")
        transitions.promote(self.board, "001", Actor.as_human("tester@host"))
        card, _ = lease.claim(self.board, "001", self.agent)
        self.tree = os.path.join(self.root, card["artifacts"]["worktree"])

    def tearDown(self):
        from kanban import worktree

        worktree.remove(self.board, "001", force=True)
        shutil.rmtree(self.root, ignore_errors=True)
        super().tearDown()

    def _guard(self, command, cwd=None):
        import io

        event = {"cwd": cwd or self.tree, "tool_input": {"command": command}}
        saved = sys.stdin, sys.stderr
        sys.stdin, sys.stderr = io.StringIO(json.dumps(event)), io.StringIO()
        try:
            return hooks.scope_guard()
        finally:
            sys.stdin, sys.stderr = saved

    def test_staging_and_committing_in_one_command_is_blocked(self):
        # Regression: the hook runs before the command, so only checking the
        # index let `git add -A && git commit` through with nothing staged.
        store.atomic_write(os.path.join(self.tree, ".env.local"), "SECRET=1\n")
        for command in ("git add -A && git commit -m x", "git commit -am x"):
            self.assertEqual(self._guard(command), hooks.BLOCK, command)

    def test_commit_issued_from_the_project_root_is_still_guarded(self):
        # `cd wt/001 && git commit` from the root: the cwd names no card, so
        # the guard falls back to the card dispatch put in the environment.
        store.atomic_write(os.path.join(self.tree, ".env.local"), "SECRET=1\n")
        command = "cd wt/001 && git add -A && git commit -m x"
        self.assertEqual(self._guard(command, cwd=self.root), hooks.ALLOW)
        os.environ[hooks.ENV_CARD] = "001"
        try:
            self.assertEqual(self._guard(command, cwd=self.root), hooks.BLOCK)
        finally:
            os.environ.pop(hooks.ENV_CARD, None)

    def test_allowed_changes_still_commit(self):
        store.atomic_write(os.path.join(self.tree, "notes.txt"), "fine\n")
        self.assertEqual(self._guard("git add -A && git commit -m x"), hooks.ALLOW)

    def test_review_flags_denied_paths_the_branch_contains(self):
        from kanban import render

        # A commit the hook never saw, e.g. from a shell it does not cover.
        store.atomic_write(os.path.join(self.tree, ".env.local"), "SECRET=1\n")
        _git(self.tree, "add", "-A")
        _git(self.tree, "commit", "-qm", "sneaky")
        transitions.submit_work(self.board, "001", self.agent)

        card, body = self.board.load("001")
        view = render.card_view(
            self.board, "001", card, body, datetime.datetime.now(datetime.timezone.utc),
            self.board.read_config(), True,
        )
        self.assertEqual([v["path"] for v in view["scope_violations"]], [".env.local"])
        approve = next(a for a in view["actions"] if a["action"] == "approve")
        self.assertIn("must not touch", approve["confirm"])


class TestDispatchGuards(TempBoard):
    """The loop guards, which matter because a runaway burns the rate limit."""

    def setUp(self):
        super().setUp()
        from kanban import dispatch

        self.dispatch = dispatch
        if not dispatch.available(self.board.read_config()):
            self.skipTest("Claude Code CLI not available on this machine")

    def test_repeated_launch_failures_park_the_card(self):
        # The failure this guards: an agent that dies before claiming never
        # increments `attempts`, so without a separate counter the dashboard
        # would respawn it on every poll forever.
        card_id = self.make("001", status="ready", tier="act")
        card, body = self.board.load(card_id)
        card["dispatch_failures"] = self.board.read_config()["max_attempts"]
        self.board.save(card_id, card, body)

        with self.assertRaises(self.dispatch.DispatchError):
            self.dispatch.dispatch(self.board, card_id)
        card, _ = self.board.load(card_id)
        self.assertEqual(card["status"], "needs_review")
        self.assertIn("never claimed", card["review"]["question"])

    def test_exhausted_attempts_park_the_card(self):
        card_id = self.make("001", status="ready", tier="act")
        card, body = self.board.load(card_id)
        card["attempts"] = self.board.read_config()["max_attempts"]
        self.board.save(card_id, card, body)

        with self.assertRaises(self.dispatch.DispatchError):
            self.dispatch.dispatch(self.board, card_id)
        card, _ = self.board.load(card_id)
        self.assertEqual(card["status"], "needs_review")

    def test_claim_clears_the_launch_failure_counter(self):
        card_id = self.make("001", status="ready")
        card, body = self.board.load(card_id)
        card["dispatch_failures"] = 2
        self.board.save(card_id, card, body)
        lease.claim(self.board, card_id, self.agent)
        card, _ = self.board.load(card_id)
        self.assertEqual(card["dispatch_failures"], 0)

    def test_blocked_card_is_not_dispatched(self):
        self.make("001", status="ready")
        self.make("002", status="ready", depends_on=["001"])
        with self.assertRaises(self.dispatch.DispatchError):
            self.dispatch.dispatch(self.board, "002")

    def test_non_ready_card_is_not_dispatched(self):
        card_id = self.make("001")  # inbox
        with self.assertRaises(self.dispatch.DispatchError):
            self.dispatch.dispatch(self.board, card_id)

    def test_agent_still_starting_is_not_redispatched_or_counted(self):
        # Regression: dashboard polls re-dispatched a card whose agent had not
        # claimed yet, spawning three agents in five seconds and parking it.
        card_id = self.make("001", status="ready", tier="act")
        card, body = self.board.load(card_id)
        card["dispatched_at"] = model.utcnow()
        self.board.save(card_id, card, body)

        with self.assertRaises(self.dispatch.DispatchError):
            self.dispatch.dispatch(self.board, card_id)
        card, _ = self.board.load(card_id)
        self.assertEqual(card["status"], "ready")
        self.assertEqual(card.get("dispatch_failures") or 0, 0)

    def test_human_requeue_resets_the_loop_guards(self):
        card_id = self.make("001", status="claimed", tier="act")
        transitions.submit_question(self.board, card_id, self.agent, "why?")
        card, body = self.board.load(card_id)
        card["dispatch_failures"] = 3
        self.board.save(card_id, card, body)
        transitions.answer(self.board, card_id, self.you, "try again")
        card, _ = self.board.load(card_id)
        self.assertEqual((card["attempts"], card["dispatch_failures"]), (0, 0))

    def test_stream_json_invocation_includes_verbose(self):
        # `claude -p --output-format stream-json` refuses to start without
        # --verbose. Omitting it made every spawned agent die on its first
        # line, and the transcript was the only place that showed it.
        card, _ = self.board.load(self.make("001", status="ready"))
        cmd, _ = self.dispatch.kanban_invocation()
        prompt = self.dispatch.build_prompt("001", card, cmd)
        self.assertIn("claim 001", prompt)
        source = store.read_text(
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "kanban",
                "dispatch.py",
            )
        )
        stream = source.index('"stream-json"')
        self.assertIn('"--verbose"', source[stream:stream + 300])
        self.assertNotIn('"--bare"', source, "never --bare: it forces API billing")


class TestReadOnly(TempBoard):
    def test_read_only_agent_cannot_edit_or_commit(self):
        from kanban import dispatch

        mode, tools = dispatch.agent_permissions(
            {"read_only": True}, {"extra_allowed_tools": ["Bash(python *x.py *)"]}
        )
        # dontAsk denies anything unlisted; verified live against the CLI.
        self.assertEqual(mode, "dontAsk")
        for banned in ("Edit", "Write", "Bash(git *)", "PowerShell(git *)"):
            self.assertNotIn(banned, tools)
        self.assertIn("Bash(kanban *)", tools)
        self.assertIn("Bash(python *x.py *)", tools, "board extras still apply")

        mode, tools = dispatch.agent_permissions({}, {"extra_allowed_tools": []})
        self.assertNotEqual(mode, "dontAsk")
        self.assertIn("Edit", tools)

    def test_read_only_is_saved_on_the_card(self):
        self.board.create_card_file(model.new_card("009", "lookup", read_only=True), "")
        card, _ = self.board.load("009")
        self.assertIs(card["read_only"], True)

    def test_read_only_prompt_says_so(self):
        from kanban import dispatch

        prompt = dispatch.build_prompt("009", {"tier": "act", "read_only": True}, "kanban")
        self.assertIn("READ-ONLY", prompt)
        self.assertNotIn("card's branch", prompt)


class TestBackToInbox(TempBoard):
    def test_human_can_move_a_running_card_to_inbox(self):
        card_id = self.make("001", status="claimed")
        transitions.to_inbox(self.board, card_id, self.you)
        card, body = self.board.load(card_id)
        self.assertEqual(card["status"], "inbox")
        self.assertIsNone(card["lease"])
        self.assertIsNone(locks.read(self.board.lock_path(card_id)), "lock leaked")
        self.assertIn("claimed → inbox", model.get_section(body, "Log"))

    def test_agent_cannot_move_a_card_to_inbox(self):
        card_id = self.make("001", status="claimed")
        with self.assertRaises(transitions.TransitionError):
            transitions.to_inbox(self.board, card_id, self.agent)

    def test_stop_agent_never_kills_a_process_that_is_not_claude(self):
        # A finished agent's PID can be reused by an unrelated program.
        from kanban import dispatch

        card_id = self.make("001", status="claimed")
        card, body = self.board.load(card_id)
        card["agent_pid"] = os.getpid()  # this test runner, not Claude Code
        self.board.save(card_id, card, body)
        self.assertFalse(dispatch.stop_agent(self.board, card_id))

    def test_stop_agent_without_a_pid_is_a_no_op(self):
        from kanban import dispatch

        self.assertFalse(dispatch.stop_agent(self.board, self.make("001", status="claimed")))


class TestScaffold(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="kanban-scaffold-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        super().tearDown()

    def _write(self, name, text):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        store.atomic_write(path, text)
        return path

    def test_init_creates_a_working_board(self):
        board, report = scaffold.init(self.root)
        self.assertTrue(os.path.isfile(board.config_path))
        self.assertTrue(os.path.isdir(board.cards_dir))
        self.assertTrue(
            os.path.isfile(os.path.join(self.root, "Dashboard.cmd"))
        )
        self.assertTrue(
            os.path.isfile(
                os.path.join(self.root, ".claude", "skills", "kanban", "SKILL.md")
            )
        )
        board.check_compat()

    def test_init_does_not_clobber_existing_files(self):
        self._write("CLAUDE.md", "# my rules\n\nkeep me\n")
        self._write(".gitignore", "node_modules/\n")
        self._write(
            ".claude/settings.json",
            json.dumps({"permissions": {"allow": ["Read(**)"]}}),
        )
        scaffold.init(self.root)

        self.assertIn(
            "keep me", store.read_text(os.path.join(self.root, "CLAUDE.md"))
        )
        self.assertIn(
            "node_modules/",
            store.read_text(os.path.join(self.root, ".gitignore")),
        )
        settings = json.loads(
            store.read_text(os.path.join(self.root, ".claude", "settings.json"))
        )
        self.assertIn("permissions", settings)
        self.assertIn("hooks", settings)

    def test_gitignore_only_ignores_the_boards_own_directories(self):
        # Regression: unanchored `runs/` and `board/` silently ignored any
        # project folder with that name -- the spec's own src/board/ example.
        if _git(self.root, "init", "-q").returncode != 0:
            self.skipTest("git unavailable")
        scaffold.init(self.root)
        for path, ignored in (
            ("runs/001.jsonl", True),
            ("wt/001/a.txt", True),
            ("cards/001.lock", True),
            (".kanban/bin/kanban", True),
            ("cards/001.md", False),
            ("src/runs/a.py", False),
            ("src/board/a.py", False),
            ("src/wt/a.py", False),
        ):
            result = _git(self.root, "check-ignore", "-q", path)
            self.assertEqual(result.returncode == 0, ignored, path)

    def test_scope_guard_covers_bash_and_powershell_commits(self):
        # Agents may commit from either shell; only Bash used to be guarded.
        scaffold.init(self.root)
        settings = json.loads(
            store.read_text(os.path.join(self.root, ".claude", "settings.json"))
        )
        guards = {
            group["matcher"]: group["hooks"][0]["if"]
            for group in settings["hooks"]["PreToolUse"]
        }
        self.assertEqual(
            guards,
            {"Bash": "Bash(git commit *)", "PowerShell": "PowerShell(git commit *)"},
        )

    def test_reinit_is_idempotent(self):
        scaffold.init(self.root)
        first = {
            name: store.read_text(os.path.join(self.root, name))
            for name in ("CLAUDE.md", ".gitignore", ".claude/settings.json")
        }
        scaffold.init(self.root)
        for name, text in first.items():
            self.assertEqual(
                store.read_text(os.path.join(self.root, name)),
                text,
                "%s changed on re-init" % name,
            )

    def test_marked_block_is_replaced_not_duplicated(self):
        path = self._write("CLAUDE.md", "before\n")
        scaffold.upsert_marked(path, "one\n", "<!--")
        scaffold.upsert_marked(path, "two\n", "<!--")
        text = store.read_text(path)
        self.assertEqual(text.count(scaffold.MARK_START), 1)
        self.assertIn("two", text)
        self.assertNotIn("one", text)
        self.assertIn("before", text)

    def test_scaffolded_files_hold_no_logic(self):
        # The upgradeability promise: scaffolded files delegate to the
        # installed package, so improving behaviour is a tool change rather
        # than a sweep through every project that ever ran `kanban init`.
        #
        # Counted excluding comments and blanks: this deliberately does not
        # penalise explaining *why* a stub is shaped the way it is.
        scaffold.init(self.root)
        comment_prefixes = ("REM ", "rem ", "#", "::")

        for name, cap in (
            ("Dashboard.cmd", 14),
            (".claude/hooks/scope-guard.py", 8),
        ):
            text = store.read_text(os.path.join(self.root, name))
            code = [
                line.strip()
                for line in text.splitlines()
                if line.strip()
                and not line.strip().startswith(comment_prefixes)
                and not line.strip().startswith('"""')
            ]
            self.assertLessEqual(
                len(code), cap, "%s looks like it grew logic:\n%s"
                % (name, "\n".join(code))
            )
            self.assertTrue(
                any("kanban" in line for line in code),
                "%s does not delegate to the kanban package" % name,
            )
            # Board logic must never be reimplemented in a copied file.
            for forbidden in ("O_EXCL", "cards/", "frontmatter", "needs_review"):
                self.assertNotIn(
                    forbidden, text, "%s contains board logic" % name
                )

        skill = store.read_text(
            os.path.join(self.root, ".claude", "skills", "kanban", "SKILL.md")
        )
        self.assertIn("brief agent", skill)

    def test_dashboard_stub_does_not_pass_a_trailing_backslash_path(self):
        # Regression: the stub used --root "%~dp0". %~dp0 always ends in a
        # backslash, so "%~dp0" ends in \" -- the C runtime reads that as an
        # escaped quote and folds it into the string, yielding a path with a
        # literal quote in it. The board was then "not found" and the window
        # closed before the error could be read.
        scaffold.init(self.root)
        text = store.read_text(os.path.join(self.root, "Dashboard.cmd"))
        # Only the executable lines: the comment explaining this bug quite
        # reasonably contains the bad pattern itself.
        code = "\n".join(
            line
            for line in text.splitlines()
            if not line.strip().lower().startswith("rem ")
        )
        self.assertNotIn(
            '--root "%~dp0"',
            code,
            "passing %~dp0 as a quoted argument corrupts the path",
        )
        self.assertIn('cd /d "%~dp0"', code)
        self.assertIn(
            "pause", code, "a failing dashboard must not close silently"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
