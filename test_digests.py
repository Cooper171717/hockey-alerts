import datetime as dt
import unittest
from unittest.mock import patch
import bot


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.store = bot.Store(':memory:')

    def tearDown(self):
        self.store.db.close()

    def game(self, state='OFF', gid=1):
        return {'id': gid, 'gameState': state, 'gameScheduleState': 'OK',
                'homeTeam': {'commonName': {'default': 'Home'}, 'score': 4},
                'awayTeam': {'commonName': {'default': 'Away'}, 'score': 2},
                'playerByGameStats': {'homeTeam': {}, 'awayTeam': {}}}

    def khl_game(self, state='finished', gid=1):
        return {'id': gid, 'khl_id': gid, 'type_id': 18, 'game_state_key': state,
                'start_at': 1792256400000, 'score': '4:2',
                'team_a': {'name': 'А', 'players': []}, 'team_b': {'name': 'Б', 'players': []}}

    def test_nhl_waits_for_every_game_including_future(self):
        for state in ('LIVE', 'CRIT', 'FUT', 'PRE', 'UNKNOWN'):
            with self.subTest(state=state), patch.object(bot, 'request', return_value={'games': [self.game(), self.game(state, 2)]}), patch.object(bot, 'deliver') as send:
                bot.completed_day_digest(self.store, '1', 'token', 'НХЛ', '2026-10-17')
                send.assert_not_called()

    def test_nhl_final_results_once_and_no_clock_gate(self):
        games = [self.game('FINAL'), self.game('OFF', 2)]
        def api(url):
            return {'games': games} if '/score/' in url else self.game()
        with patch.object(bot, 'request', side_effect=api), patch.object(bot, 'deliver') as send:
            bot.completed_day_digest(self.store, '1', 'token', 'НХЛ', '2026-10-17')
            bot.completed_day_digest(self.store, '1', 'token', 'НХЛ', '2026-10-17')
            self.assertEqual(send.call_count, 1)
            self.assertIn('Итоги игрового дня НХЛ', send.call_args.args[2])
            self.assertIn('4:2', send.call_args.args[2])

    def test_postponed_is_listed_and_does_not_block_completed_games(self):
        postponed = dict(self.game('FUT', 2), gameScheduleState='PPD')
        with patch.object(bot, 'request', side_effect=lambda url: {'games': [self.game(), postponed]} if '/score/' in url else self.game()), patch.object(bot, 'deliver') as send:
            bot.completed_day_digest(self.store, '1', 'token', 'НХЛ', '2026-10-17')
            self.assertIn('перенесён / отменён', send.call_args.args[2])

    def test_no_games_or_only_cancelled_no_digest(self):
        for games in ([], [dict(self.game(), gameScheduleState='CNCL')]):
            with patch.object(bot, 'request', return_value={'games': games}), patch.object(bot, 'deliver') as send:
                bot.completed_day_digest(self.store, '1', 'token', 'НХЛ', '2026-10-17')
                send.assert_not_called()

    def test_khl_waits_for_last_game(self):
        for state in ('not_yet_started', 'in_progress', 'unknown'):
            with patch.object(bot, 'khl_day_games', return_value=[self.khl_game(), self.khl_game(state, 2)]), patch.object(bot, 'deliver') as send:
                bot.completed_day_digest(self.store, '1', 'token', 'КХЛ', '2026-10-17')
                send.assert_not_called()

    def test_khl_final_digest_and_league_independent_history(self):
        with patch.object(bot, 'khl_day_games', return_value=[self.khl_game()]), patch.object(bot, 'request', return_value={'event': self.khl_game()}), patch.object(bot, 'deliver') as send:
            bot.completed_day_digest(self.store, '1', 'token', 'КХЛ', '2026-10-17')
            bot.completed_day_digest(self.store, '1', 'token', 'КХЛ', '2026-10-17')
            self.assertEqual(send.call_count, 1)
            self.assertIn('А — Б: 4:2', send.call_args.args[2])
        self.assertFalse(self.store.seen('1', {'league': 'final-digest:НХЛ', 'game': '2026-10-17', 'player': 'complete'}))

    def test_failed_delivery_retry(self):
        with patch.object(bot, 'request', side_effect=lambda url: {'games': [self.game()]} if '/score/' in url else self.game()), patch.object(bot, 'deliver', side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):
                bot.completed_day_digest(self.store, '1', 'token', 'НХЛ', '2026-10-17')
        self.assertFalse(self.store.seen('1', {'league': 'final-digest:НХЛ', 'game': '2026-10-17', 'player': 'complete'}))

    def test_source_failure_never_sends_partial_digest(self):
        with patch.object(bot, 'request', side_effect=[{'games': [self.game()]}, RuntimeError('source down')]), patch.object(bot, 'deliver') as send:
            with self.assertRaises(RuntimeError):
                bot.completed_day_digest(self.store, '1', 'token', 'НХЛ', '2026-10-17')
            send.assert_not_called()

    def test_midnight_uses_original_days_and_isolates_errors(self):
        now = dt.datetime(2026, 10, 17, 22, 0, tzinfo=dt.timezone.utc)
        calls = []
        def digest(store, chat, token, league, day, dry):
            calls.append((league, day))
            if league == 'НХЛ':
                raise RuntimeError('down')
        with patch.object(bot, 'completed_day_digest', side_effect=digest):
            self.assertEqual(bot.completed_digests(self.store, '1', 'token', now), 3)
        self.assertIn(('НХЛ', '2026-10-17'), calls)
        self.assertIn(('КХЛ', '2026-10-18'), calls)
        self.assertIn(('КХЛ', '2026-10-17'), calls)

    def test_khl_pagination_includes_pending_and_deduplicates(self):
        # 2026-10-17 17:00 UTC belongs to Moscow October 17.
        rows = [{'event': self.khl_game()}, {'event': self.khl_game('not_yet_started', 2)}]
        with patch.object(bot, 'request', side_effect=[rows, rows]) as api:
            games = bot.khl_day_games('2026-10-17')
            self.assertEqual(len(games), 2)
            self.assertEqual(api.call_count, 2)


if __name__ == '__main__':
    unittest.main()
