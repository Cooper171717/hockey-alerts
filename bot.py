"""Personal KHL/NHL Telegram alerts. Python 3.11+, standard library only."""
import argparse
import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
KHL = 'https://khl.api.webcaster.pro/api/khl_mobile'
NHL = 'https://api-web.nhle.com/v1'
LOG = logging.getLogger('hattrick')


def load_env():
    path = ROOT / '.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def request(url, data=None):
    payload = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, payload, {'User-Agent': 'HattrickNotifier/1.0',
                                               'Content-Type': 'application/json'})
    # Do not log request URLs: Telegram URLs contain the secret token.
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'HTTP {exc.code}') from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError('Network request failed') from None
    if isinstance(result, dict) and (result.get('ok') is False or 'error' in result):
        raise RuntimeError('API returned an error')
    return result


def local_name(value):
    return value.get('ru', value.get('default', '')) if isinstance(value, dict) else str(value)


def nhl_team(team):
    return (local_name(team.get('placeName', '')) + ' ' +
            local_name(team.get('commonName', team.get('abbrev', '')))).strip()


def nhl_hats(game):
    found = []
    for side in ('homeTeam', 'awayTeam'):
        stats = game['playerByGameStats'][side]
        for player in stats.get('forwards', []) + stats.get('defense', []):
            if int(player.get('goals', 0)) >= 3:
                found.append({'league': 'НХЛ', 'game': str(game['id']),
                              'player': str(player['playerId']), 'name': local_name(player['name']),
                              'team': nhl_team(game[side]), 'goals': int(player['goals']),
                              'match': f"{nhl_team(game['homeTeam'])} — {nhl_team(game['awayTeam'])}",
                              'score': f"{game['homeTeam'].get('score', 0)}:{game['awayTeam'].get('score', 0)}",
                              'date': game['gameDate'],
                              'url': f"https://www.nhl.com/gamecenter/{game['id']}"})
    return found


def khl_hats(game):
    found = []
    for side in ('team_a', 'team_b'):
        team = game[side]
        for player in team['players']:
            goals = next((int(s['val']) for s in player.get('match_stats', [])
                          if s['id'] == 'goals'), 0)
            if goals >= 3:
                found.append({'league': 'КХЛ', 'game': str(game['khl_id']),
                              'player': str(player['id']), 'name': player['name'],
                              'team': team['name'], 'goals': goals,
                              'match': f"{game['team_a']['name']} — {game['team_b']['name']}",
                              'score': game['score'],
                              'date': dt.datetime.fromtimestamp(game['start_at'] / 1000,
                                                               dt.timezone.utc).date().isoformat(),
                              'url': f"https://www.khl.ru/game/{game['season']}/{game['khl_id']}/"})
    return found


def message(hat):
    return (f"🎩 Хет-трик! {hat['league']}\n\n"
            f"Игрок: {hat['name']}\nКоманда: {hat['team']}\n"
            f"Матч: {hat['match']}\nСчёт на момент проверки: {hat['score']}\n"
            f"Дата матча: {hat['date']}\nГолы: {hat['goals']}\n\n{hat['url']}")


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS sent (chat TEXT, league TEXT, game TEXT, player TEXT, PRIMARY KEY(chat,league,game,player))')

    def seen(self, chat, hat):
        return self.db.execute('SELECT 1 FROM sent WHERE chat=? AND league=? AND game=? AND player=?',
                               (chat, hat['league'], hat['game'], hat['player'])).fetchone() is not None

    def mark(self, chat, hat):
        self.db.execute('INSERT OR IGNORE INTO sent VALUES (?,?,?,?)',
                        (chat, hat['league'], hat['game'], hat['player']))
        self.db.commit()


class CloudStore:
    """Only opaque notification hashes are committed to the public repository."""
    def __init__(self, path):
        self.path = Path(path)
        self.sent = set(json.loads(self.path.read_text()) if self.path.exists() else [])

    @staticmethod
    def key(chat, hat):
        value = [chat, hat['league'], hat['game'], hat['player']]
        return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()

    def seen(self, chat, hat):
        return self.key(chat, hat) in self.sent

    def mark(self, chat, hat):
        self.sent.add(self.key(chat, hat))
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(sorted(self.sent), indent=2) + '\n', encoding='utf-8')
        temporary.replace(self.path)


def nhl_games(now):
    games = {}
    for offset in (-1, 0):
        date = (now.date() + dt.timedelta(days=offset)).isoformat()
        for game in request(f'{NHL}/score/{date}')['games']:
            if game['gameState'] in ('LIVE', 'CRIT', 'FINAL', 'OFF'):
                games[game['id']] = game
    for game in games.values():
        yield game['id'], lambda gid=game['id']: request(f'{NHL}/gamecenter/{gid}/boxscore')


def khl_games(now):
    start = int((now - dt.timedelta(hours=36)).timestamp())
    end = int((now + dt.timedelta(hours=1)).timestamp())
    seen = set()
    for page in range(1, 101):
        query = urllib.parse.urlencode({'q[start_at_gt_time_from_unixtime]': start,
                                       'q[start_at_lt_time_from_unixtime]': end,
                                       'order_direction': 'asc', 'page': page, 'locale': 'ru'})
        rows = request(f'{KHL}/events_v2.json?{query}')
        if not isinstance(rows, list):
            raise ValueError('Unexpected KHL schedule schema')
        if not rows:
            return
        fresh = 0
        for row in rows:
            game = row['event']
            if game['id'] in seen:
                continue
            seen.add(game['id'])
            fresh += 1
            if game.get('team_a') and game.get('team_b') and game['game_state_key'] != 'not_yet_started':
                yield game['id'], lambda gid=game['id']: request(f'{KHL}/event_v2.json?id={gid}&locale=ru')['event']
        if not fresh:
            return
    raise RuntimeError('KHL pagination limit exceeded')


def scan(store, chat, token, dry=False):
    failures = 0
    now = dt.datetime.now(dt.timezone.utc)
    for source, parser in ((nhl_games, nhl_hats), (khl_games, khl_hats)):
        try:
            for gid, fetch in source(now):
                try:
                    for hat in parser(fetch()):
                        if store.seen(chat, hat):
                            continue
                        if hat['league'] == 'НХЛ':
                            profile = request(f"{NHL}/player/{hat['player']}/landing")
                            hat['name'] = local_name(profile['firstName']) + ' ' + local_name(profile['lastName'])
                        if dry:
                            print(message(hat), '\n')
                        else:
                            request(f'https://api.telegram.org/bot{token}/sendMessage',
                                    {'chat_id': chat, 'text': message(hat), 'link_preview_options': {'is_disabled': True}})
                            store.mark(chat, hat)
                            LOG.info('Notification delivered: %s %s', hat['league'], hat['game'])
                except Exception as exc:
                    failures += 1
                    LOG.error('Game %s failed (%s)', gid, type(exc).__name__)
        except Exception as exc:
            failures += 1
            LOG.error('%s failed (%s)', source.__name__, type(exc).__name__)
    return failures


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    load_env()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    ap = argparse.ArgumentParser()
    ap.add_argument('--chat-id', action='store_true', help='Show private chats after /start')
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--cloud', action='store_true', help='Persist opaque notification hashes in cloud-state.json')
    ap.add_argument('--test-message', action='store_true', help='Send a connection test and exit')
    ap.add_argument('--dry-run', action='store_true', help='Print alerts without sending or saving')
    args = ap.parse_args()
    token = os.getenv('TELEGRAM_BOT_TOKEN', '')
    chat = os.getenv('TELEGRAM_CHAT_ID', '')
    if args.chat_id:
        if not token:
            ap.error('Set TELEGRAM_BOT_TOKEN in .env')
        updates = request(f'https://api.telegram.org/bot{token}/getUpdates')
        chats = {str(u['message']['chat']['id']) for u in updates['result']
                 if u.get('message', {}).get('chat', {}).get('type') == 'private'}
        print('Private chat IDs:', ', '.join(sorted(chats)) or 'None. Send /start to your bot first.')
        return
    if not args.dry_run and (not token or not chat):
        ap.error('Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env; see README.md')
    if args.test_message:
        request(f'https://api.telegram.org/bot{token}/sendMessage',
                {'chat_id': chat, 'text': '✅ Проверка подключения: бот может присылать вам уведомления.'})
        return
    interval = int(os.getenv('POLL_SECONDS', '60'))
    if interval < 30:
        ap.error('POLL_SECONDS must be >= 30')
    store = CloudStore(ROOT / 'cloud-state.json') if args.cloud else Store(ROOT / 'state.sqlite3')
    LOG.info('Monitoring KHL and NHL; interval %s seconds', interval)
    while True:
        failures = scan(store, chat, token, args.dry_run)
        if args.once or args.dry_run:
            return 1 if failures else 0
        time.sleep(interval)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
