import tempfile
import datetime as dt
import unittest
from pathlib import Path
from unittest.mock import patch
import bot


class Tests(unittest.TestCase):
    def nhl(self, goals=3):
        return {'id': 1, 'gameDate': '2026-10-17',
                'homeTeam': {'placeName': {'default': 'Washington'}, 'commonName': {'default': 'Capitals'}, 'score': 4},
                'awayTeam': {'placeName': {'default': 'Pittsburgh'}, 'commonName': {'default': 'Penguins'}, 'score': 2},
                'playerByGameStats': {'homeTeam': {'forwards': [{'playerId': 8, 'name': {'default': 'A. Ovechkin'}, 'goals': goals}]}, 'awayTeam': {}}}

    def test_threshold(self):
        self.assertEqual(bot.nhl_hats(self.nhl(2)), [])
        self.assertEqual(len(bot.nhl_hats(self.nhl(3))), 1)
        self.assertEqual(len(bot.nhl_hats(self.nhl(4))), 1)

    def test_khl_uses_match_stats_not_season_or_shootout(self):
        game = {'khl_id': 1, 'season': 2027, 'score': '4:3', 'start_at': 1792195200000,
                'goals': [{'author': {'gps': 30}}] * 4,
                'team_a': {'name': 'Спартак', 'players': [{'id': 8, 'name': 'Игрок Один', 'match_stats': [{'id': 'goals', 'val': 3}]}]},
                'team_b': {'name': 'СКА', 'players': [{'id': 9, 'name': 'Игрок Два', 'match_stats': [{'id': 'goals', 'val': 2}]}]}}
        hats = bot.khl_hats(game)
        self.assertEqual([h['name'] for h in hats], ['Игрок Один'])
        self.assertIn('Спартак — СКА', bot.message(hats[0]))

    def test_persistent_dedup_and_chat_scope(self):
        hat = bot.nhl_hats(self.nhl())[0]
        with tempfile.TemporaryDirectory(dir=bot.ROOT) as folder:
            path = Path(folder) / 'state.db'
            store = bot.Store(path)
            self.assertFalse(store.seen('1', hat))
            store.mark('1', hat)
            store.db.close()
            store = bot.Store(path)
            self.assertTrue(store.seen('1', hat))
            self.assertFalse(store.seen('2', hat))
            store.db.close()

    def test_failed_delivery_is_retried(self):
        game = self.nhl()
        store = bot.Store(':memory:')
        hat = bot.nhl_hats(game)[0]
        def api(url, data=None):
            if 'landing' in url:
                return {'firstName': {'default': 'Alex'}, 'lastName': {'default': 'Ovechkin'}}
            raise RuntimeError('delivery failed')
        with patch.object(bot, 'nhl_games', return_value=[(1, lambda: game)]), patch.object(bot, 'khl_games', return_value=[]), patch.object(bot, 'request', side_effect=api):
            bot.scan(store, '123', 'secret')
        self.assertFalse(store.seen('123', hat))
        store.db.close()

    def test_cloud_history_persists_without_plain_chat_id(self):
        hat = bot.nhl_hats(self.nhl())[0]
        with tempfile.TemporaryDirectory(dir=bot.ROOT) as folder:
            path = Path(folder) / 'cloud-state.json'
            store = bot.CloudStore(path)
            self.assertFalse(store.seen('private-chat-123', hat))
            store.mark('private-chat-123', hat)
            restored = bot.CloudStore(path)
            self.assertTrue(restored.seen('private-chat-123', hat))
            self.assertFalse(restored.seen('different-chat', hat))
            self.assertNotIn('private-chat-123', path.read_text())

    def test_source_failure_does_not_stop_other_league(self):
        game = self.nhl()
        store = bot.Store(':memory:')
        def api(url, data=None):
            if 'landing' in url:
                return {'firstName': {'default': 'Alex'}, 'lastName': {'default': 'Ovechkin'}}
            return {'ok': True}
        def broken_source(now):
            raise RuntimeError('unavailable')
        with patch.object(bot, 'nhl_games', return_value=[(1, lambda: game)]), patch.object(bot, 'khl_games', new=broken_source), patch.object(bot, 'request', side_effect=api):
            self.assertEqual(bot.scan(store, '123', 'secret'), 1)
        self.assertTrue(store.seen('123', bot.nhl_hats(game)[0]))
        store.db.close()

    def test_goal_milestones_and_legacy_history(self):
        hat = bot.nhl_hats(self.nhl(3))[0]
        self.assertEqual(next(bot.milestones(hat))['player'], hat['player'])
        four = bot.nhl_hats(self.nhl(4))[0]
        self.assertEqual(next(bot.milestones(four))['player'], '8:goals4')
        five = bot.nhl_hats(self.nhl(5))[0]
        self.assertEqual([e['player'] for e in bot.milestones(five)], ['8:goals5', '8:points5'])
        six = bot.nhl_hats(self.nhl(6))[0]
        self.assertEqual([e['player'] for e in bot.milestones(six)], ['8:goals5', '8:points5'])

    def test_points_without_hat_trick(self):
        game = self.nhl(1)
        game['playerByGameStats']['homeTeam']['forwards'][0]['assists'] = 4
        hat = bot.nhl_hats(game)[0]
        self.assertEqual(hat['points'], 5)
        events = list(bot.milestones(hat))
        self.assertEqual(len(events), 1)
        self.assertIn('points5', events[0]['player'])

    def test_khl_assists_from_match_goals_and_correct_team(self):
        player = {'id': 8, 'name': 'Игрок', 'shirt_number': 10,
                  'match_stats': [{'id': 'goals', 'val': 1}]}
        goal = {'period': 5, 'author': {'team_id': 1},
                'assistants': [{'shirt_number': 10, 'aps': 99}]}
        game = {'khl_id': 1, 'season': 2027, 'score': '5:2', 'start_at': 1792195200000,
                'team_a': {'id': 1, 'name': 'А', 'players': [player]},
                'team_b': {'id': 2, 'name': 'Б', 'players': []},
                'goals': [goal] * 4 + [dict(goal, author={'team_id': 2})]}
        hat = bot.khl_hats(game)[0]
        self.assertEqual((hat['goals'], hat['assists'], hat['points']), (1, 4, 5))

    def test_scan_skips_old_lower_milestones_after_poker(self):
        store = bot.Store(':memory:')
        game = self.nhl(4)
        profile = {'firstName': {'default': 'Alex'}, 'lastName': {'default': 'Ovechkin'}}
        with patch.object(bot, 'nhl_games', return_value=[(1, lambda: game)]), patch.object(bot, 'khl_games', return_value=[]), patch.object(bot, 'request', return_value=profile), patch.object(bot, 'deliver') as send:
            self.assertEqual(bot.scan(store, '1', 'token'), 0)
            bot.scan(store, '1', 'token')
            self.assertEqual(send.call_count, 1)
        self.assertTrue(store.seen('1', bot.nhl_hats(self.nhl(3))[0]))
        store.db.close()

    def test_digest_moscow_time_no_games_and_once_per_day(self):
        store = bot.Store(':memory:')
        before = dt.datetime(2026, 10, 17, 5, 59, tzinfo=dt.timezone.utc)
        after = before + dt.timedelta(minutes=2)
        with patch.dict('os.environ', {'DIGEST_HOUR_MSK': '9'}), patch.object(bot, 'request', return_value={'games': []}) as api, patch.object(bot, 'deliver') as send:
            bot.morning_digest(store, '1', 'token', before)
            api.assert_not_called()
            bot.morning_digest(store, '1', 'token', after)
            bot.morning_digest(store, '1', 'token', after)
            self.assertEqual(send.call_count, 1)
            self.assertIn('2026-10-16', api.call_args.args[0])
        store.db.close()

    def test_digest_failure_is_retried(self):
        store = bot.Store(':memory:')
        now = dt.datetime(2026, 10, 17, 6, 5, tzinfo=dt.timezone.utc)
        with patch.object(bot, 'request', return_value={'games': []}), patch.object(bot, 'deliver', side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):
                bot.morning_digest(store, '1', 'token', now)
        self.assertFalse(store.seen('1', {'league': 'digest', 'game': '2026-10-16', 'player': 'complete'}))
        store.db.close()

    def test_digest_long_text_split_under_telegram_limit(self):
        parts = bot.digest_parts(['строка ' * 30] * 80)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 3500 for p in parts))

    def test_digest_results_hat_trick_and_live_status(self):
        game = dict(self.nhl(3), gameState='LIVE')
        store = bot.Store(':memory:')
        now = dt.datetime(2026, 10, 17, 6, 5, tzinfo=dt.timezone.utc)
        def api(url, data=None):
            if '/score/' in url:
                return {'games': [game]}
            if '/boxscore' in url:
                return game
            return {'firstName': {'default': 'Alex'}, 'lastName': {'default': 'Ovechkin'}}
        with patch.object(bot, 'request', side_effect=api), patch.object(bot, 'deliver') as send:
            bot.morning_digest(store, '1', 'token', now)
            text = send.call_args.args[2]
            self.assertIn('4:2 (идёт)', text)
            self.assertIn('Alex Ovechkin', text)
            self.assertIn('Washington Capitals', text)
        store.db.close()


if __name__ == '__main__':
    unittest.main()
