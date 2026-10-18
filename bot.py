"""Personal KHL/NHL Telegram alerts. Python 3.11+, standard library only."""
import argparse
import datetime as dt
import hashlib
from html import escape
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
            goals, assists = int(player.get('goals', 0)), int(player.get('assists', 0))
            if goals >= 3 or goals + assists >= 5:
                found.append({'league': 'НХЛ', 'game': str(game['id']),
                              'player': str(player['playerId']), 'name': local_name(player['name']),
                              'team': nhl_team(game[side]), 'goals': int(player['goals']),
                              'assists': assists, 'points': goals + assists,
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
            # KHL's player match_stats does not always contain assists.
            # Count match goal assists, never the cumulative season field `aps`.
            assists = next((int(s['val']) for s in player.get('match_stats', [])
                            if s['id'] == 'assists'), None)
            if assists is None:
                assists = sum(1 for goal in game.get('goals', [])
                              if goal.get('author', {}).get('team_id') == team.get('id')
                              and str(goal.get('period', '')).lower() not in ('so', 'bullitt')
                              for assistant in goal.get('assistants', [])
                              if player.get('shirt_number') is not None
                              and assistant.get('shirt_number') == player['shirt_number'])
            if goals >= 3 or goals + assists >= 5:
                found.append({'league': 'КХЛ', 'game': str(game['khl_id']),
                              'player': str(player['id']), 'name': player['name'],
                              'team': team['name'], 'goals': goals,
                              'assists': assists, 'points': goals + assists,
                              'match': f"{game['team_a']['name']} — {game['team_b']['name']}",
                              'score': game['score'],
                              'date': dt.datetime.fromtimestamp(game['start_at'] / 1000,
                                                               dt.timezone.utc).date().isoformat(),
                              'url': f"https://www.khl.ru/game/{game['season']}/{game['khl_id']}/"})
    return found


def message(hat):
    title = hat.get('title', '🎩 Хет-трик!')
    return (f"<b>{escape(title.rstrip('!'))}</b> · {escape(hat['league'])}\n"
            f"{display_date(hat['date'])}\n\n"
            f"<b>{escape(hat['name'])}</b>\n{escape(hat['team'])}\n\n"
            f"Голы <b>{hat['goals']}</b>  ·  Передачи <b>{hat.get('assists', 0)}</b>  ·  "
            f"Очки <b>{hat.get('points', hat['goals'])}</b>\n\n"
            f"{escape(hat['match'])}\nСчёт сейчас: <b>{escape(str(hat['score']))}</b>\n\n"
            f"<a href=\"{escape(hat['url'], quote=True)}\">Открыть матч →</a>")


def display_date(value):
    months = ('января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
              'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря')
    date = dt.date.fromisoformat(value)
    return f'{date.day} {months[date.month - 1]} {date.year}'


def milestones(hat):
    """Keep original hat-trick keys so upgrading preserves existing history."""
    goals = hat['goals']
    if goals >= 3:
        level = min(goals, 5)
        event = dict(hat)
        event['player'] = hat['player'] if level == 3 else f"{hat['player']}:goals{level}"
        event['title'] = {3: '🎩 Хет-трик!', 4: '🔥 Покер!', 5: '🚀 Пять голов!'}[level]
        yield event
    if hat.get('points', goals) >= 5:
        yield dict(hat, player=f"{hat['player']}:points5", title='⭐ Пять и более очков!')


def deliver(token, chat, text):
    request(f'https://api.telegram.org/bot{token}/sendMessage',
            {'chat_id': chat, 'text': text, 'parse_mode': 'HTML',
             'link_preview_options': {'is_disabled': True}})


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
                        events = [event for event in milestones(hat) if not store.seen(chat, event)]
                        if not events:
                            continue
                        if hat['league'] == 'НХЛ':
                            profile = request(f"{NHL}/player/{hat['player']}/landing")
                            hat['name'] = local_name(profile['firstName']) + ' ' + local_name(profile['lastName'])
                        for event in events:
                            event['name'] = hat['name']
                            if dry:
                                print(message(event), '\n')
                            else:
                                deliver(token, chat, message(event))
                                store.mark(chat, event)
                                # Skip stale lower goal milestones if first seen at 4/5 goals.
                                if ':points5' not in event['player']:
                                    for lower in range(3, min(hat['goals'], 5)):
                                        key = hat['player'] if lower == 3 else f"{hat['player']}:goals{lower}"
                                        store.mark(chat, dict(hat, player=key))
                                LOG.info('Notification delivered: %s %s', hat['league'], hat['game'])
                except Exception as exc:
                    failures += 1
                    LOG.error('Game %s failed (%s)', gid, type(exc).__name__)
        except Exception as exc:
            failures += 1
            LOG.error('%s failed (%s)', source.__name__, type(exc).__name__)
    return failures


def digest_parts(lines, limit=3500):
    parts, current = [], ''
    for line in lines:
        if len(current) + len(line) + 1 > limit:
            parts.append(current.rstrip())
            current = ''
        current += line + '\n'
    if current:
        parts.append(current.rstrip())
    return parts


def khl_day_games(day):
    """Fetch the entire Moscow calendar day, including games not yet started."""
    zone = dt.timezone(dt.timedelta(hours=3))
    start = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(), zone)
    end = start + dt.timedelta(days=1)
    games = {}
    for page in range(1, 101):
        query = urllib.parse.urlencode({
            'q[start_at_gt_time_from_unixtime]': int(start.timestamp()) - 1,
            'q[start_at_lt_time_from_unixtime]': int(end.timestamp()),
            'order_direction': 'asc', 'page': page, 'locale': 'ru'})
        rows = request(f'{KHL}/events_v2.json?{query}')
        if not isinstance(rows, list):
            raise ValueError('Unexpected KHL schedule schema')
        if not rows:
            return list(games.values())
        fresh = 0
        for row in rows:
            game = row['event']
            if not (game.get('team_a') and game.get('team_b') and game.get('khl_id')):
                continue
            if game.get('type_id') not in (18, 24):
                continue
            when = dt.datetime.fromtimestamp(game['start_at'] / 1000, zone)
            if when.date().isoformat() != day:
                continue
            key = str(game['khl_id'])
            if key not in games:
                fresh += 1
            games[key] = game
        if not fresh:
            return list(games.values())
    raise RuntimeError('KHL day pagination limit exceeded')


def digest_game_status(league, game):
    if league == 'НХЛ':
        if game.get('gameScheduleState') in ('PPD', 'CNCL') or game.get('gameState') in ('PPD', 'CNCL'):
            return 'excluded'
        return 'finished' if game.get('gameState') in ('FINAL', 'OFF') else 'pending'
    state = game.get('game_state_key')
    if state in ('postponed', 'cancelled', 'canceled'):
        return 'excluded'
    return 'finished' if state == 'finished' else 'pending'


def completed_day_digest(store, chat, token, league, day, dry=False):
    # A new namespace deliberately allows final NHL results after an old morning summary.
    key = {'league': f'final-digest:{league}', 'game': day, 'player': 'complete'}
    if store.seen(chat, key):
        return
    games = (request(f'{NHL}/score/{day}')['games'] if league == 'НХЛ'
             else khl_day_games(day))
    if not games:
        return
    statuses = [digest_game_status(league, game) for game in games]
    if 'pending' in statuses or 'finished' not in statuses:
        return
    count = statuses.count('finished')
    lines = [f'🏒 <b>{escape(league)} · Итоги дня</b>',
             display_date(day), '',
             f'Матчей завершено: <b>{count}</b>', '', '<b>Результаты</b>', '']
    hats = []
    # Build every result before sending: a source failure must not send a partial summary.
    for game, status in zip(games, statuses):
        if league == 'НХЛ':
            match = f"{nhl_team(game['homeTeam'])} — {nhl_team(game['awayTeam'])}"
        else:
            match = f"{game['team_a']['name']} — {game['team_b']['name']}"
        if status == 'excluded':
            lines.append(f'{escape(match)}\n<i>Перенесён / отменён</i>\n')
            continue
        if league == 'НХЛ':
            score = f"{game['homeTeam']['score']}:{game['awayTeam']['score']}"
            box = request(f"{NHL}/gamecenter/{game['id']}/boxscore")
            match = f"{nhl_team(box['homeTeam'])} — {nhl_team(box['awayTeam'])}"
            players = nhl_hats(box)
        else:
            score = game['score']
            box = request(f"{KHL}/event_v2.json?id={game['id']}&locale=ru")['event']
            players = khl_hats(box)
        lines.append(f'{escape(match)}\n<b>{escape(str(score))}</b>\n')
        for hat in players:
            if hat['goals'] < 3:
                continue
            name = hat['name']
            if league == 'НХЛ':
                profile = request(f"{NHL}/player/{hat['player']}/landing")
                name = local_name(profile['firstName']) + ' ' + local_name(profile['lastName'])
            hats.append(f"<b>{escape(name)}</b> · голы: <b>{hat['goals']}</b>\n"
                        f"{escape(hat['team'])}\n{escape(match)}\n")
    lines += ['🎩 <b>Хет-трики</b>', '', *(hats or ['В этот игровой день хет-триков не было.'])]
    lines += ['', '<i>Все состоявшиеся матчи завершены.</i>']
    header = f'🏒 <b>{escape(league)} · Итоги дня</b>\n{display_date(day)}'
    for index, text in enumerate(digest_parts(lines, limit=3200)):
        if index:
            text = f'{header}\n<i>Продолжение · часть {index + 1}</i>\n\n{text}'
        part_key = dict(key, player=f'part{index}')
        if store.seen(chat, part_key):
            continue
        if dry:
            print(text, '\n')
        else:
            deliver(token, chat, text)
            store.mark(chat, part_key)
    if not dry:
        store.mark(chat, key)


def completed_digests(store, chat, token, now=None, dry=False):
    now = now or dt.datetime.now(dt.timezone.utc)
    failures = 0
    for league in ('НХЛ', 'КХЛ'):
        today = (now.date() if league == 'НХЛ' else
                 now.astimezone(dt.timezone(dt.timedelta(hours=3))).date())
        # Include recent dates so a game crossing midnight still completes its original day.
        for offset in (-2, -1, 0):
            day = (today + dt.timedelta(days=offset)).isoformat()
            try:
                completed_day_digest(store, chat, token, league, day, dry)
            except Exception as exc:
                failures += 1
                LOG.error('Final digest %s %s failed (%s)', league, day, type(exc).__name__)
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
        failures += completed_digests(store, chat, token, dry=args.dry_run)
        if args.once or args.dry_run:
            return 1 if failures else 0
        time.sleep(interval)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
