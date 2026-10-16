import tempfile
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


if __name__ == '__main__':
    unittest.main()
