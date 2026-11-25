#!/usr/bin/env python3
"""Read-only Volleys Cup ESPN roster snapshot, separate from the standings importer.

Run from the repository root:
    python scripts/update_rosters.py --probe      # validate only, print sample
    python scripts/update_rosters.py              # save data/rosters.json if changed

The public GitHub repo will contain player roster/lineup/injury data, never
ESPN cookies or private member identifiers. The ESPN v3 API is unofficial.
"""

import argparse
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

LEAGUE_ID = 822887247
SEASON = 2026
TEAM_COUNT = 12
STANDINGS_PATH = Path('data/current.json')
ROSTERS_PATH = Path('data/rosters.json')
ENDPOINTS = (
    'https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}/segments/0/leagues/{league}',
    'https://fantasy.espn.com/apis/v3/games/ffl/seasons/{year}/segments/0/leagues/{league}',
)
POSITIONS = {1: 'QB', 2: 'RB', 3: 'WR', 4: 'TE', 5: 'K', 16: 'D/ST'}
LINEUP_SLOTS = {
    0: 'QB', 1: 'TQB', 2: 'RB', 3: 'RB/WR', 4: 'WR', 5: 'WR/TE', 6: 'TE',
    7: 'OP', 16: 'D/ST', 17: 'K', 20: 'Bench', 21: 'IR', 23: 'FLEX',
}


def clean(value):
    value = unicodedata.normalize('NFKC', html.unescape(str(value or '')))
    value = ''.join(ch if ch.isalnum() or ch.isspace() else ' ' for ch in value)
    return ' '.join(value.casefold().split())


def team_names(team):
    names = [team.get('name'), ' '.join(filter(None, [team.get('location'), team.get('nickname')]))]
    return {clean(n) for n in names if n and n.strip()}


def fetch():
    # Only mTeam, mRoster, mStatus and mSettings; no ESPN write endpoints.
    query = urllib.parse.urlencode([('view', view) for view in ('mTeam', 'mRoster', 'mStatus', 'mSettings')])
    cookies = []
    for var, cookie in (('ESPN_S2', 'espn_s2'), ('ESPN_SWID', 'SWID')):
        val = os.environ.get(var, '').strip()
        if val:
            cookies.append(f'{cookie}={val}')
    headers = {'User-Agent': 'Mozilla/5.0 (Volleys Cup roster reader)', 'Accept': 'application/json'}
    if cookies:
        headers['Cookie'] = '; '.join(cookies)
    errors = []
    for template in ENDPOINTS:
        url = template.format(year=SEASON, league=LEAGUE_ID) + '?' + query
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as response:
                payload = json.load(response)
            if isinstance(payload, list):
                payload = payload[0] if len(payload) == 1 else None
            if not isinstance(payload, dict) or not isinstance(payload.get('teams'), list):
                raise ValueError('Response lacks teams array')
            return payload
        except (urllib.error.URLError, ValueError, TimeoutError, KeyError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403):
                raise RuntimeError('ESPN denied access. Confirm the league is publicly viewable, or supply the existing ESPN_S2 and ESPN_SWID GitHub Actions secrets.') from None
            errors.append(f'{type(exc).__name__}: {getattr(exc, "code", str(exc))}')
    raise RuntimeError('Could not retrieve ESPN rosters; ' + '; '.join(errors))


def roster_player(entry):
    if not isinstance(entry, dict):
        raise ValueError('Invalid ESPN roster entry')
    pool = entry.get('playerPoolEntry') or {}
    player = pool.get('player') or {}
    player_id = entry.get('playerId') or pool.get('id') or player.get('id')
    if not player_id or not str(player_id).isdigit():
        raise ValueError('Roster entry is missing a player ID')
    player_id = int(player_id)
    name = player.get('fullName') or pool.get('fullName') or entry.get('fullName')
    if not name:
        name = ' '.join(str(player.get(key) or '').strip() for key in ('firstName', 'lastName')).strip()
    if not name:
        raise ValueError(f'Player {player_id} missing full name (ESPN response changed?)')
    raw_position = player.get('defaultPositionId')
    position_id = int(raw_position) if raw_position is not None else -1
    raw_slot = entry.get('lineupSlotId')
    if raw_slot is None:
        raise ValueError(f'{name} missing lineupSlotId')
    slot = int(raw_slot)
    if slot == 20:
        role = 'bench'
    elif slot == 21:
        role = 'ir'
    else:
        role = 'starter'
    raw_pro_team = player.get('proTeamId')
    raw_eligible = player.get('eligibleSlots') or []
    return {
        'player_id': player_id,
        'name': str(name),
        'position': POSITIONS.get(position_id, f'POSITION_{position_id}'),
        'position_id': position_id,
        'pro_team_id': int(raw_pro_team) if raw_pro_team is not None else None,
        'lineup_slot_id': slot,
        'lineup_slot': LINEUP_SLOTS.get(slot, f'SLOT_{slot}'),
        'role': role,
        'eligible_slot_ids': sorted(set(int(v) for v in raw_eligible)),
        'injury_status': str(player.get('injuryStatus') or entry.get('injuryStatus') or 'UNKNOWN'),
    }


def build_snapshot(payload, standings, now=None):
    teams = payload.get('teams')
    known = standings.get('standings') or []
    if len(known) != TEAM_COUNT or not isinstance(teams, list) or len(teams) != TEAM_COUNT:
        raise ValueError(f'Expected {TEAM_COUNT} mapped teams, got ESPN={len(teams) if isinstance(teams,list) else "missing"}, standings={len(known)}')
    if int(payload.get('seasonId', SEASON)) != SEASON:
        raise ValueError('ESPN season ID mismatch')
    if int(payload.get('id', LEAGUE_ID)) != LEAGUE_ID:
        raise ValueError('ESPN league ID mismatch')
    known_by_name = {}
    for item in known:
        key = clean(item['team'])
        if not key or key in known_by_name:
            raise ValueError('Duplicate or blank team name in current standings')
        known_by_name[key] = item
    player_ids = set()
    gm_ids = set()
    espn_ids = set()
    result_teams = []
    for team in teams:
        matches = {item['gm_id']: item for key in team_names(team) if (item := known_by_name.get(key))}
        if len(matches) != 1:
            raise ValueError(f'Cannot uniquely map ESPN team {team.get("id")} {team.get("name")!r} to an existing GM; no data will be written')
        old = next(iter(matches.values()))
        if old['gm_id'] in gm_ids:
            raise ValueError('Two ESPN teams mapped to one GM')
        gm_ids.add(old['gm_id'])
        espn_id = int(team['id'])
        if espn_id in espn_ids:
            raise ValueError('Duplicate ESPN team ID')
        espn_ids.add(espn_id)
        roster = team.get('roster') or {}
        entries = roster.get('entries')
        if not isinstance(entries, list) or not 8 <= len(entries) <= 35:
            raise ValueError(f'{old["team"]}: expected 8–35 roster entries, received {len(entries) if isinstance(entries,list) else "none"}')
        players = [roster_player(entry) for entry in entries]
        for player in players:
            player_id = player['player_id']
            if player_id in player_ids:
                raise ValueError(f'Player {player_id} appears on multiple teams or twice on one roster')
            player_ids.add(player_id)
        # Stable ordering keeps diffs small when ESPN changes its entry order.
        players.sort(key=lambda p: (p['role'] == 'ir', p['role'] == 'bench', p['lineup_slot_id'], p['position'], p['name']))
        result_teams.append({
            'gm_id': old['gm_id'],
            'team': old['team'],
            'espn_team_id': espn_id,
            'players': players,
        })
    result_teams.sort(key=lambda t: t['team'].casefold())
    settings = (payload.get('settings') or {}).get('rosterSettings') or {}
    raw_counts = settings.get('lineupSlotCounts') or {}
    slot_counts = {LINEUP_SLOTS.get(int(k), f'SLOT_{k}'): int(v)
                   for k, v in raw_counts.items() if int(v) > 0}
    current_period = (payload.get('status') or {}).get('currentMatchupPeriod') or payload.get('scoringPeriodId')
    return {
        'schema_version': 1,
        'league_id': LEAGUE_ID,
        'season': SEASON,
        'source': 'ESPN Fantasy API (read-only mRoster snapshot)',
        'captured_at_utc': (now or datetime.now(timezone.utc)).isoformat(timespec='seconds').replace('+00:00', 'Z'),
        'espn_matchup_period': int(current_period) if current_period is not None else None,
        'team_count': len(result_teams),
        'player_count': len(player_ids),
        'starting_lineup_slots': dict(sorted(slot_counts.items())),
        'teams': result_teams,
    }


def publish_if_changed(snapshot, path):
    if path.exists():
        old = json.loads(path.read_text(encoding='utf-8'))
        def comparable(doc):
            return {k: v for k, v in doc.items() if k != 'captured_at_utc'}
        if comparable(old) == comparable(snapshot):
            print('No roster, lineup, injury or scoring-period changes; file unchanged.')
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Wrote {path}: {snapshot["team_count"]} teams / {snapshot["player_count"]} players.')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe', action='store_true', help='Validate and preview ESPN rosters without writing data')
    args = parser.parse_args()
    standings = json.loads(STANDINGS_PATH.read_text(encoding='utf-8'))
    snapshot = build_snapshot(fetch(), standings)
    print(f'Validated season {SEASON} ESPN rosters: {snapshot["team_count"]} teams, {snapshot["player_count"]} distinct players; ESPN matchup period {snapshot["espn_matchup_period"]}.')
    for team in sorted(snapshot['teams'], key=lambda t: t['gm_id']):
        p = team['players']
        examples = ', '.join(f'{x["name"]} ({x["position"]})' for x in p[:2])
        print(f'  ESPN #{team["espn_team_id"]:>2} -> {team["team"]}: {len(p)} players; sample: {examples}')
    if args.probe:
        print('PROBE ONLY: no data file written and no GitHub commit needed.')
    else:
        publish_if_changed(snapshot, ROSTERS_PATH)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Roster sync aborted safely: {exc}', file=sys.stderr)
        sys.exit(1)
